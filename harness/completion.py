"""Resynthesis loop + completion check — Deliverable 7.5.

Runs resynthesis → investigation cycles with a hard cap
(``config['limits']['max_resynthesis_iterations']``):

  for each structured group (SCC / struct consumers / call neighborhoods, via
  ``compute_resynthesis_groups`` in graph_analysis.py):
     - assemble subgraph context, ask the resynthesis agent,
     - create any new investigation tasks,
     - apply any merged-claim / deleted-contradictory-claim decisions.
  If no new tasks were created in an iteration, the evidence landscape is
  stable → stop. Otherwise run the investigation loop to drain the new tasks
  and try again. Finally re-register function names from the (potentially
  changed) graph and return ``is_re_complete``.
"""

from __future__ import annotations

import logging

from reaper.tools.graph_analysis import compute_resynthesis_groups

log = logging.getLogger(__name__)


async def is_re_complete(ledger, todo) -> bool:
    """Pipeline completion: every RENAMABLE function has claims AND the TODO is
    empty.

    Pinned functions (imports/library/named symbols) are given, not discovered,
    so they are intentionally excluded from the claim requirement — otherwise
    completion would be blocked by functions nobody was asked to investigate.
    Use ``ledger.claim_coverage_stats()`` for a partial-credit breakdown.
    """
    try:
        has_claims = await ledger.all_functions_have_claims(include_pinned=False)
    except Exception:
        log.exception("is_re_complete: all_functions_have_claims failed")
        has_claims = False
    try:
        todo_empty = await todo.is_empty()
    except Exception:
        log.exception("is_re_complete: todo.is_empty failed")
        todo_empty = False
    return has_claims and todo_empty


class ResynthesisLoop:
    """Resynthesis ↔ investigation cycles until stability or the cap (7.5)."""

    def __init__(
        self,
        resynth_agent,
        inv_loop,
        todo,
        ledger,
        neo4j_driver,
        context_asm,
        tracer,
        config: dict,
    ):
        self.resynth_agent = resynth_agent
        self.inv_loop = inv_loop
        self.todo = todo
        self.ledger = ledger
        self.neo4j_driver = neo4j_driver
        self.context_asm = context_asm
        self.tracer = tracer
        self.config = config or {}
        limits = self.config.get("limits") or {}
        self._max_iterations = int(limits.get("max_resynthesis_iterations", 5))

    async def run(self) -> bool:
        """Run resynthesis cycles; returns True when the pipeline is complete."""
        for iteration in range(self._max_iterations):
            try:
                groups = await compute_resynthesis_groups(self.neo4j_driver)
            except Exception:
                log.exception("resynthesis loop: compute_resynthesis_groups failed")
                groups = []
            new_task_count = 0
            # Groups are independent — sequential for v1 simplicity.
            for group_addrs in groups:
                try:
                    context = await self.context_asm.for_subgraph(group_addrs)
                    result = await self.resynth_agent.run(context)
                    for tspec in result.new_tasks:
                        await self._create_task(tspec)
                        new_task_count += 1
                    for mc in result.merged_claims:
                        await self._merge_claim(mc)
                except Exception:
                    log.exception("resynthesis loop: group %s failed", group_addrs)

            await self._log(
                "resynthesis_iteration",
                {"iteration": iteration, "groups": len(groups),
                 "new_tasks": new_task_count},
            )
            if new_task_count == 0:
                break  # evidence landscape stable
            await self.inv_loop.run()

        try:
            await self.ledger.register_functions_from_graph()
        except Exception:
            log.exception("resynthesis loop: register_functions_from_graph failed")
        # Partial-credit visibility: report the claim-coverage breakdown instead
        # of hiding it behind a single boolean (design § 8.3 metric 4).
        try:
            stats = await self.ledger.claim_coverage_stats()  # type: ignore[attr-defined]
        except Exception:
            log.debug("resynthesis loop: claim_coverage_stats unavailable")
            stats = {}
        if stats:
            await self._log("completion_coverage", stats)
        return await is_re_complete(self.ledger, self.todo)

    async def _create_task(self, tspec) -> None:
        try:
            await self.todo.create_task(**tspec.model_dump())
        except Exception:
            log.exception("resynthesis loop: create_task failed for %s", tspec.start_position)

    async def _merge_claim(self, mc) -> None:
        try:
            await self.ledger.update_claim_text(mc.keep_id, mc.merged_text)
            for rid in mc.remove_ids or []:
                await self.ledger.delete_claim(rid)
        except Exception:
            log.exception("resynthesis loop: merge claim failed (keep=%s)", mc.keep_id)

    async def _log(self, event_type: str, data: dict) -> None:
        if self.tracer is None:
            return
        try:
            await self.tracer.log(event_type, "resynthesis_loop", data)
        except Exception:
            log.debug("resynthesis loop: tracer.log(%s) failed", event_type)


__all__ = ["ResynthesisLoop", "is_re_complete"]
