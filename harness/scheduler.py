"""Scheduler agent — Deliverable 7.1.

Reviews the TODO ledger, merges overlapping tasks, decomposes large ones, and
hands ready tasks to the investigation loop. Responsibilities:

  * ``review_pending_tasks()`` — batch pending task descriptions (chunked to
    20 to avoid context overflow) and ask the LLM for merge / decompose /
    keep recommendations, then apply them.
  * ``run_once()`` — reset stale in_progress tasks to pending, take all ready
    tasks (dependencies satisfied), assign each (status → in_progress), and
    return them. Does NOT run agents — the caller (InvestigationLoop, 7.3)
    owns execution.
  * stale check — in_progress tasks older than
    ``config['limits']['task_timeout_seconds']`` are reset to pending.
"""

from __future__ import annotations

import logging

from pydantic import BaseModel, Field

from reaper.harness.submission import get_schema, parse_response, send_structured  # noqa: F401

log = logging.getLogger(__name__)

_BATCH = 20  # max pending task descriptions per LLM call (design § 7.1)

_SYSTEM_PROMPT = """\
You are the REAPER Task Scheduler. Given the list of pending reverse-engineering
TODO tasks (each with an id, description, and start address), recommend how to
organize them:

- MERGE: two or more tasks that clearly ask about the SAME function/behavior.
  Emit one MergedTaskSpec with their task_ids, a single combined description,
  the lowest start address, and a single concrete goal.
- DECOMPOSE: a task that bundles MULTIPLE independent questions. Emit one
  SplitTaskSpec PER resulting atomic subtask, each marked with source_task_id.
- Otherwise leave the task as-is (list it under neither).

Never invent task ids that were not listed. Never propose a merge that would
lose information. Output the SchedulerPlan schema exactly.
"""


class MergedTaskSpec(BaseModel):
    """Merge multiple tasks into one new task."""
    task_ids: list[int] = Field(description="existing task ids to merge")
    description: str
    start_position: str
    goal: str
    graph_refs: list[str] = []
    context_spec: dict = {}


class SplitTaskSpec(BaseModel):
    """One resulting subtask of a decomposed task."""
    source_task_id: int = Field(description="the original task being split")
    description: str
    start_position: str
    goal: str
    graph_refs: list[str] = []
    context_spec: dict = {}


class SchedulerPlan(BaseModel):
    """Raw LLM recommendation output."""
    merges: list[MergedTaskSpec] = []
    decomposes: list[SplitTaskSpec] = []
    keep: list[int] = []


class Scheduler:
    """Reviews/assigns TODO tasks; never runs agents itself (7.1)."""

    def __init__(self, llm_client, todo, context_asm, tracer, config: dict):
        self.llm = llm_client
        self.todo = todo
        self.context_asm = context_asm
        self.tracer = tracer
        self.config = config or {}
        limits = self.config.get("limits") or {}
        self._timeout = int(limits.get("task_timeout_seconds", 600))
        self._thinking = self.config.get("thinking_levels", {}).get(
            "scheduler", "low"
        )

    async def review_pending_tasks(self) -> int:
        """Review pending tasks and apply merge/decompose recommendations.

        Returns the number of LLM recommendation batches processed.
        """
        pending = await self.todo.get_pending_tasks()
        batches = [
            pending[i:i + _BATCH]
            for i in range(0, len(pending), _BATCH)
        ]
        processed = 0
        for chunk in batches:
            plan = await self._review_batch(chunk)
            await self._apply_plan(plan)
            processed += 1
            await self._log("scheduler_reviewed", {"batch_size": len(chunk),
                                                   "merges": len(plan.merges),
                                                   "splits": len(plan.decomposes)})
        return processed

    async def run_once(self) -> list[dict]:
        """Assign ready tasks and return them (does NOT execute agents)."""
        try:
            stale = await self.todo.reset_stale_in_progress(self._timeout)
            if stale:
                await self._log("scheduler_stale_reset", {"count": stale})
        except Exception:
            log.exception("scheduler: stale task check failed")
        ready = await self.todo.get_ready_tasks()
        assigned: list[dict] = []
        for task in ready:
            try:
                await self.todo.assign_task(task["id"], f"sched_{task['id']}")
                assigned.append(task)
            except Exception:
                log.exception("scheduler: assign_task(%s) failed", task.get("id"))
        await self._log("scheduler_assigned", {"count": len(assigned)})
        return assigned

    def _format_batch(self, tasks: list[dict]) -> str:
        lines = ["PENDING TASKS:"]
        for t in tasks:
            lines.append(
                f"  [id={t.get('id')}] {t.get('description')} "
                f"(start={t.get('start_position')}, goal={t.get('goal')})"
            )
        return "\n".join(lines)

    async def _review_batch(self, tasks: list[dict]) -> SchedulerPlan:
        if not tasks or self.llm is None:
            return SchedulerPlan()
        payload = self._format_batch(tasks)
        session_id = "scheduler_batch"
        try:
            await self.llm.create_session(session_id, _SYSTEM_PROMPT)
            return await send_structured(
                self.llm, session_id, payload, SchedulerPlan,
                thinking_level=self._thinking,
            )
        finally:
            self.llm.destroy_session(session_id)

    async def _apply_plan(self, plan: SchedulerPlan) -> None:
        for merge in plan.merges or []:
            try:
                await self.todo.create_task(
                    description=merge.description,
                    context_spec=merge.context_spec or {},
                    start_position=merge.start_position or "",
                    goal=merge.goal,
                    graph_refs=list(merge.graph_refs or []),
                )
                for task_id in merge.task_ids or []:
                    await self.todo.delete_task(task_id)
            except Exception:
                log.exception("scheduler: merge apply failed")

        # Group splits by source task so each source is deleted at most once.
        by_source: dict[int, list[SplitTaskSpec]] = {}
        for split in plan.decomposes or []:
            by_source.setdefault(split.source_task_id, []).append(split)
        for source_id, splits in by_source.items():
            try:
                for split in splits:
                    await self.todo.create_task(
                        description=split.description,
                        context_spec=split.context_spec or {},
                        start_position=split.start_position or "",
                        goal=split.goal,
                        graph_refs=list(split.graph_refs or []),
                    )
                await self.todo.delete_task(source_id)
            except Exception:
                log.exception("scheduler: decompose apply failed")

    async def _log(self, event_type: str, data: dict) -> None:
        if self.tracer is None:
            return
        try:
            await self.tracer.log(event_type, "scheduler", data)
        except Exception:
            log.debug("scheduler: tracer.log(%s) failed", event_type)


__all__ = ["Scheduler", "SchedulerPlan", "MergedTaskSpec", "SplitTaskSpec"]
