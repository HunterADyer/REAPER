"""Pass 2 dispatcher — Deliverable 6.3.

Orchestrates deep review → critic → merge for all functions, walking
traversal_order ascending and grouping by level for parallelism (functions at
the same level run concurrently via a Semaphore; levels are awaited before
advancing — upper levels need lower-level decisions as context).

Per function (parallel):
  a. Review agent reads master graph/ledger directly (read-only).
  b. Review agent → ReviewOutput (renames + claims + tasks).
  c. Claims → ledger (truth_level=NULL) → critic → accept/reject; rejected
     claims are deleted by the critic and never reach merge.
  d. Tasks → light critic review (atomic? goal concrete?) → TodoLedger.
  e. Renames → ONLY when the review "passed" (the function had no claims, or
     at least one claim was accepted) → merge_agent.attempt_merge. Claims are
     intentionally excluded from the merge Submission because they were
     already written to the ledger in step (c) — avoiding double-insertion.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from reaper.agents.critic_agent import CriticEvaluator
from reaper.agents.review_agent import ReviewAgent
from reaper.harness.submission import (
    CriticVerdict,
    Submission,
    get_schema,
    parse_response,
)

log = logging.getLogger(__name__)


class Pass2Dispatcher:
    """Review → critic → merge orchestration (design § 6.3)."""

    def __init__(
        self,
        llm_client,
        neo4j_driver,
        context_asm,
        shadow_mgr,
        merge_agent,
        bndb_writer,
        ledger,
        todo,
        tracer,
        config: dict,
    ):
        self.llm = llm_client
        self.neo4j_driver = neo4j_driver
        self.context_asm = context_asm
        self.shadow_mgr = shadow_mgr
        self.merge_agent = merge_agent
        self.bndb_writer = bndb_writer
        self.ledger = ledger
        self.todo = todo
        self.tracer = tracer
        self.config = config or {}
        limits = self.config.get("limits") or {}
        self._semaphore = asyncio.Semaphore(
            int(limits.get("max_concurrent_agents", 8))
        )
        self.review_agent = ReviewAgent(llm_client, context_asm, tracer, config)
        # One CriticEvaluator per dispatcher (design § 6.1 — never shared).
        self.critic = CriticEvaluator(
            llm_client, context_asm, ledger, tracer, config
        )
        with open(self._critic_prompt_path(), encoding="utf-8") as fh:
            self.task_prompt = fh.read()

    @staticmethod
    def _critic_prompt_path() -> str:
        return str(Path(__file__).resolve().parent.parent
                   / "agents" / "prompts" / "critic_agent.txt")

    async def run(self) -> None:
        """Execute the full Pass 2 review sweep (design § 6.3)."""
        functions = await self._functions_ordered()
        if not functions:
            await self._log("pass2_no_functions", {})
            return

        max_level = max(level for _, level in functions)
        for level in range(max_level + 1):
            addrs = [a for a, lvl in functions if lvl == level]
            if not addrs:
                continue
            await asyncio.gather(*(self._process_function(a) for a in addrs))
            await self._log("pass2_level_done", {"level": level, "functions": addrs})

    async def _process_function(self, func_addr: str) -> None:
        async with self._semaphore:
            try:
                review = await self.review_agent.run(func_addr)

                accepted_claims = []
                for claim in review.claims:
                    try:
                        claim_id = await self.ledger.add_claim(
                            claim.function_address, claim.claim_text,
                            f"pass2:{func_addr}",
                            [e.model_dump() for e in claim.evidence],
                        )
                    except Exception:
                        log.exception("pass2: add_claim failed for %s", func_addr)
                        continue
                    outcome = await self.critic.evaluate_claim(
                        claim_id, claim, func_addr, "review_agent"
                    )
                    if outcome.accepted:
                        accepted_claims.append(claim)

                for tspec in review.tasks:
                    if await self._task_ok(tspec, func_addr):
                        await self._create_task(tspec)

                # Renames are merged only if the review "passed" (no claims, or
                # at least one accepted claim); claims intentionally excluded.
                if review.renames and (not review.claims or accepted_claims):
                    submission = Submission(renames=review.renames, claims=[])
                    await self.merge_agent.attempt_merge(
                        f"pass2_{func_addr}", submission
                    )

                await self._log(
                    "pass2_function_done", {
                        "func_addr": func_addr,
                        "claims": len(review.claims),
                        "accepted_claims": len(accepted_claims),
                        "renames": len(review.renames),
                        "tasks": len(review.tasks),
                    },
                )
            except Exception:
                log.exception("Pass2Dispatcher: function %s failed", func_addr)

    async def _task_ok(self, tspec, func_addr: str) -> bool:
        """Light critic check: is the task atomic with a concrete goal?"""
        payload = (
            "TASK TO EVALUATE (criticize as a TODO for reverse engineering):\n"
            f"description: {tspec.description}\n"
            f"goal: {tspec.goal}\n"
            f"start_position: {tspec.start_position}\n\n"
            "Is it atomic (ONE question) with a CONCRETE goal? "
            "If yes, accepted=true; if vague/compound, accepted=false."
        )
        session_id = f"task_critic_{func_addr}_{tspec.start_position}"
        try:
            await self.llm.create_session(session_id, self.task_prompt)
            response = await self.llm.send(
                session_id,
                payload,
                thinking_level=self.config.get("thinking_levels", {}).get(
                    "pass2_review", "max"
                ),
                structured_output=get_schema(CriticVerdict),
            )
            verdict = parse_response(CriticVerdict, response)
        finally:
            self.llm.destroy_session(session_id)
        return bool(verdict.accepted)

    async def _create_task(self, tspec) -> None:
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
            log.exception("pass2: todo.create_task failed for %s", tspec.start_position)

    async def _functions_ordered(self) -> list[tuple[str, int]]:
        rows: list[dict] = []
        async with self.neo4j_driver.session() as session:
            res = await session.run(
                "MATCH (f:Function) "
                "RETURN f.address AS address, f.traversal_order AS traversal_order "
                "ORDER BY f.traversal_order ASC"
            )
            rows = [r async for r in res]
        out: list[tuple[str, int]] = []
        for r in rows:
            addr = r.get("address")
            level = r.get("traversal_order")
            if addr is not None and level is not None:
                out.append((str(addr), int(level)))
        return out

    async def _log(self, event_type: str, data: dict) -> None:
        if self.tracer is None:
            return
        try:
            await self.tracer.log(event_type, "pass2", data)
        except Exception:
            log.debug("Pass2Dispatcher: tracer.log(%s) failed", event_type)


__all__ = ["Pass2Dispatcher"]
