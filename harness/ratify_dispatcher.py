"""Ratify dispatcher — deterministic Pass-1 xhigh naming decision sweep.

Drives the RatifyNamesAgent over every RENAMABLE function (in traversal order,
level-gated like Pass 1 so callees' approved names inform caller context),
and for each returned decision:

  * ``approve`` -> publish the decision (no write) — the current name stands.
  * ``rename``  -> merge the new name onto the graph (stable node id) + BNDB,
                   then publish the decision with its evidence.

The rename is applied EXACTLY ONCE. There is no critic loop, no retry, no
shadow-merge, no conflict resolution — the ratifier's word is final, which is
deliberate for a deterministic 2-pass RE (the VR stage refines later).

Atomicity follows the existing discipline: graph MERGE first, ledger second;
any write failure is logged, never rolled back.
"""

from __future__ import annotations

import asyncio
import logging

from reaper.agents.ratify_names import RatifyNamesAgent

log = logging.getLogger(__name__)

_LEVELS_KEY = "ratify"  # thinking budget for the deep ratification pass


class RatifyDispatcher:
    """Sweeps all renamable functions, applies ratify decisions once (Pass 1)."""

    def __init__(
        self,
        llm_client,
        neo4j_driver,
        context_asm,
        bndb_writer,
        ledger,
        tracer,
        config: dict,
    ):
        self.llm = llm_client
        self.neo4j_driver = neo4j_driver
        self.context_asm = context_asm
        self.bndb_writer = bndb_writer
        self.ledger = ledger
        self.tracer = tracer
        self.config = config or {}
        limits = self.config.get("limits") or {}
        self._semaphore = asyncio.Semaphore(
            int(limits.get("max_concurrent_agents", 8))
        )
        self.ratify_agent = RatifyNamesAgent(llm_client, context_asm, tracer, config)
        # (node_id) -> name Binja currently holds (mirror BNDBWriter discipline).
        self._seen_renames: set[str] = set()

    async def run(self) -> None:
        functions = await self._functions_ordered()
        if not functions:
            await self._log("ratify_no_functions", {})
            return
        max_level = max(level for _, level in functions)
        n_decisions = 0
        n_renames = 0
        for level in range(max_level + 1):
            addrs = [a for a, lvl in functions if lvl == level]
            if not addrs:
                continue
            results = await asyncio.gather(
                *(self._process_function(a) for a in addrs),
                return_exceptions=True,
            )
            for res in results:
                if isinstance(res, Exception):
                    log.exception("ratify: function failed", exc_info=res)
                elif res:
                    n_decisions += res[0]
                    n_renames += res[1]
            await self._log("ratify_level_done", {"level": level, "n": len(addrs)})

        if self.bndb_writer is not None:
            try:
                self.bndb_writer.save()
            except Exception:
                log.exception("ratify: bndb save failed")
        await self._log("ratify_done", {
            "functions": len(functions),
            "decisions": n_decisions,
            "renames": n_renames,
        })

    async def _process_function(self, func_addr: str) -> tuple[int, int]:
        async with self._semaphore:
            output = await self.ratify_agent.run(func_addr)
        decisions = list(output.decisions or [])
        renames = 0
        for d in decisions:
            self._normalize(func_addr, d)
            await self._publish(func_addr, d)
            if d.decision == "rename" and d.node_id:
                await self._apply_rename(func_addr, d)
                renames += 1
        return len(decisions), renames

    def _normalize(self, func_addr: str, decision) -> None:
        """Scope a bare entity suffix to the function (same defense as
        RenameVariableAgent) BEFORE any publish/merge, so the ledger row and
        the graph write share the same stable id."""
        node_id = getattr(decision, "node_id", None)
        if not node_id:
            return
        node_id = str(node_id)
        if ":" not in node_id:
            decision.node_id = f"{func_addr}:{node_id}"

    async def _publish(self, func_addr: str, decision) -> None:
        """Record the decision to the ledger (approve AND rename)."""
        try:
            await self.ledger.record_name_decision({
                "function_address": func_addr,
                "entity": decision.entity,
                "node_id": decision.node_id,
                "decision": decision.decision,
                "current_name": decision.current_name,
                "llm_name": decision.llm_name,
                "canon_name": decision.canon_name,
                "justification": decision.justification,
                "confidence": decision.confidence,
                "evidence": [
                    e.model_dump() if hasattr(e, "model_dump") else dict(e)
                    for e in (decision.evidence or [])
                ],
                "submitted_by": "ratify",
            })
        except Exception:
            log.exception("ratify: record_name_decision failed for %s",
                          getattr(decision, "node_id", "?"))

    async def _apply_rename(self, func_addr: str, decision) -> None:
        """Merge the rename into the graph + BNDB, exactly once.

        ``node_id`` is the stable id: variable/argument ids carry a ':' (the
        function prefix is already included); function ids are bare addresses.
        The ratifier is instructed to return the id UNCHANGED — barring that,
        we normalize a bare entity suffix to the function-scoped id (same
        defense as RenameVariableAgent).
        """
        node_id = str(decision.node_id)
        llm_name = decision.llm_name or decision.canon_name
        canon_name = decision.canon_name or decision.llm_name
        if not node_id or not llm_name:
            return
        if node_id in self._seen_renames:
            return  # never rename twice
        if ":" not in node_id:
            # guard for any caller that skips _normalize
            node_id = f"{func_addr}:{node_id}"
            decision.node_id = node_id
        self._seen_renames.add(node_id)

        await self._merge_graph(node_id, llm_name, canon_name)
        if self.bndb_writer is not None:
            try:
                if ":" in node_id:
                    addr = int(node_id.split(":")[0], 16)
                    self.bndb_writer.rename_variable(
                        addr, node_id, llm_name, canon_name)
                else:
                    self.bndb_writer.rename_function(
                        int(node_id, 16), llm_name, canon_name)
            except Exception:
                log.exception("ratify: bndb apply failed for %s", node_id)

    async def _merge_graph(self, node_id: str, llm_name: str, canon_name: str) -> None:
        try:
            async with self.neo4j_driver.session() as session:
                if ":" in node_id:
                    await session.run(
                        "MERGE (n {id: $id}) "
                        "SET n.llm_name = $llm, n.canon_name = $canon",
                        {"id": node_id, "llm": llm_name, "canon": canon_name},
                    )
                else:
                    await session.run(
                        "MERGE (n:Function {address: $id}) "
                        "SET n.llm_name = $llm, n.canon_name = $canon",
                        {"id": node_id, "llm": llm_name, "canon": canon_name},
                    )
        except Exception:
            log.exception("ratify: graph merge failed for %s", node_id)

    async def _functions_ordered(self) -> list[tuple[str, int]]:
        rows = []
        try:
            async with self.neo4j_driver.session() as session:
                res = await session.run(
                    "MATCH (f:Function) "
                    "WHERE (f.pinned IS NULL OR f.pinned = false) "
                    "RETURN f.address AS address, f.traversal_order AS traversal_order "
                    "ORDER BY f.traversal_order ASC"
                )
                rows = [r async for r in res]
        except Exception:
            log.exception("ratify: _functions_ordered query failed")
        out = []
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
            await self.tracer.log(event_type, "ratify", data)
        except Exception:
            log.debug("ratify: tracer.log(%s) failed", event_type)


__all__ = ["RatifyDispatcher"]
