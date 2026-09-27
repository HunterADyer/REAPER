"""Investigation loop runner — Deliverable 7.3.

Runs the scheduler + investigation until the TODO ledger drains or the loop
gets stuck. Creates its OWN CriticEvaluator (design § 6.1 — one evaluator per
harness component, never shared).

Completion flow (design § 7.2, implemented deadlock-free):
  1. claims → ledger → critic; count accepted claims.
  2. subtasks → todo; each created independently so it can run immediately
     (a mutual depends_on cycle would deadlock get_ready_tasks).
  3. if subtasks were created → requeue the current task (pending) so it is
     retried once its subtasks complete.
  4. else, if at least one claim was accepted (or no claims at all) →
     complete the task; otherwise requeue with the critic's feedback.

Stuck detection (design § 7.3): an iteration that assigns nothing, with
nothing in_progress and a non-empty ledger, logs ``investigation_stuck`` and
returns False. Returns True only when the ledger is fully drained.
"""

from __future__ import annotations

import asyncio
import logging

from reaper.agents.critic_agent import CriticEvaluator

log = logging.getLogger(__name__)


class InvestigationLoop:
    """Scheduler + investigation until drain or stuck (7.3)."""

    def __init__(
        self,
        scheduler,
        inv_agent,
        llm_client,
        context_asm,
        todo,
        ledger,
        tracer,
        config: dict,
    ):
        self.scheduler = scheduler
        self.inv_agent = inv_agent
        self.context_asm = context_asm
        self.todo = todo
        self.ledger = ledger
        self.tracer = tracer
        self.config = config or {}
        limits = self.config.get("limits") or {}
        self._max_concurrent = int(limits.get("max_concurrent_agents", 8))
        self.critic = CriticEvaluator(llm_client, context_asm, ledger, tracer, config)

    async def run(self) -> bool:
        """Drain the TODO ledger; returns True iff all tasks completed."""
        while True:
            try:
                await self.scheduler.review_pending_tasks()
            except Exception:
                log.exception("investigation loop: review_pending_tasks failed")
            try:
                tasks = await self.scheduler.run_once()
            except Exception:
                log.exception("investigation loop: run_once failed")
                tasks = []

            if tasks:
                sem = asyncio.Semaphore(self._max_concurrent)

                async def execute(task):
                    async with sem:
                        result = await self.inv_agent.run(task)
                        await self._handle_result(task, result)

                await asyncio.gather(
                    *(execute(t) for t in tasks), return_exceptions=True
                )

            if await self.todo.is_empty():
                await self._log("investigation_complete", {})
                return True
            if tasks or await self.todo.has_in_progress():
                continue  # progress is being made
            # Nothing dispatched AND nothing in progress → stuck.
            await self._log("investigation_stuck", {})
            return False

    async def _handle_result(self, task: dict, result) -> None:
        task_id = task.get("id")
        func_addr = task.get("start_position") or ""
        accepted_claims = 0

        for claim in result.claims:
            try:
                claim_id = await self.ledger.add_claim(
                    claim.function_address, claim.claim_text,
                    f"investigation_{task_id}",
                    [e.model_dump() for e in claim.evidence],
                )
            except Exception:
                log.exception("investigation loop: add_claim failed")
                continue
            try:
                outcome = await self.critic.evaluate_claim(
                    claim_id, claim, func_addr or claim.function_address,
                    "investigation_agent",
                )
                if outcome.accepted:
                    accepted_claims += 1
            except Exception:
                log.exception("investigation loop: critic failed for claim %s", claim_id)

        if result.subtasks:
            for tspec in result.subtasks:
                try:
                    spec = tspec.context_spec
                    context_spec = (spec.model_dump() if hasattr(spec, "model_dump")
                                    else dict(spec or {}))
                    await self.todo.create_task(
                        description=tspec.description,
                        context_spec=context_spec,
                        start_position=tspec.start_position,
                        goal=tspec.goal,
                        graph_refs=list(tspec.graph_refs or []),
                    )
                except Exception:
                    log.exception("investigation loop: create subtask failed")
            await self._requeue(task_id, "subtasks spawned — will retry later")
            return

        if accepted_claims == 0 and result.claims:
            await self._requeue(task_id, "all claims rejected by the critic — retry")
            return

        try:
            await self.todo.complete_task(task_id)
        except Exception:
            log.exception("investigation loop: complete_task(%s) failed", task_id)

    async def _requeue(self, task_id, feedback: str) -> None:
        if task_id is None:
            return
        try:
            await self.todo.requeue_task(task_id, feedback)
        except Exception:
            log.exception("investigation loop: requeue_task(%s) failed", task_id)

    async def _log(self, event_type: str, data: dict) -> None:
        if self.tracer is None:
            return
        try:
            await self.tracer.log(event_type, "inv_loop", data)
        except Exception:
            log.debug("investigation loop: tracer.log(%s) failed", event_type)


__all__ = ["InvestigationLoop"]
