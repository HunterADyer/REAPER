"""Bottom-up traversal dispatcher — Deliverable 5.2 (Pass 1).

Walks functions in traversal_order (ascending). For each function on the
current level (parallelized across functions, sequential WITHIN a function):

  a. non-pinned :Variable nodes → rename (minimal thinking) → apply to
     Neo4j + BNDB immediately (no critic in Pass 1 — speed).
  d. non-pinned :Argument nodes → rename with callee-rename context.
  f. function summary via the FunctionSummary schema.
  g. apply: bndb rename + ledger.set_function_names/set_function_summary.

Levels are AWAITED before the next one (upper levels need lower-level renames
as context). Then an SCC second pass re-runs only argument renames + function
summaries for members of each SCC (SCC peers were only partially grounded in
the main pass). Finally bndb_writer.save().
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from reaper.agents.rename_variable import RenameVariableAgent
from reaper.harness.submission import (
    FunctionSummary,
    Rename,
    get_schema,
    parse_response,
)

log = logging.getLogger(__name__)

_LEVELS_KEY = "pass1_rename"  # summary renames use the same minimal budget


class Pass1Dispatcher:
    """Processes every function bottom-up: variables → arguments → summary."""

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
        self.rename_agent = RenameVariableAgent(
            llm_client, context_asm, tracer, config
        )
        with open(self._summary_prompt_path(), encoding="utf-8") as fh:
            self.summary_prompt = fh.read()

    @staticmethod
    def _summary_prompt_path() -> str:
        root = Path(__file__).resolve().parent.parent / "agents" / "prompts"
        return str(root / "function_summary.txt")

    async def run(self) -> None:
        """Execute the full Pass 1 sweep (design § 5.2)."""
        functions = await self._functions_ordered()
        if not functions:
            await self._log("pass1_no_functions", {})
            return

        max_level = max(level for _, level in functions)
        for level in range(max_level + 1):
            addrs = [a for a, lvl in functions if lvl == level]
            if not addrs:
                continue
            await asyncio.gather(*(self._process_function(a) for a in addrs))
            await self._log("pass1_level_done",
                            {"level": level, "functions": addrs})

        # 4. SCC second pass — only arguments + function summaries.
        await self._scc_second_pass()

        # 5. Persist the BinaryView.
        if self.bndb_writer is not None:
            try:
                self.bndb_writer.save()
            except Exception:
                log.exception("Pass1Dispatcher: bndb save() failed")

    async def _process_function(self, func_addr: str) -> None:
        async with self._semaphore:
            try:
                # a. Variables (non-pinned), SEQUENTIAL within the function.
                for var_id in await self._query_nodes(func_addr, "Variable"):
                    submission = await self.rename_agent.run(func_addr, var_id)
                    await self._apply_submission(submission)

                # d. Arguments (non-pinned) with callee rename context.
                for arg_id in await self._query_nodes(func_addr, "Argument"):
                    submission = await self.rename_agent.run(
                        func_addr, arg_id, include_callee_renames=True
                    )
                    await self._apply_submission(submission)

                # f. Function summary.
                summary = await self._function_summary(func_addr)
                if summary is not None:
                    await self._apply_summary(func_addr, summary)
            except Exception:
                log.exception("Pass1Dispatcher: function %s failed", func_addr)

    async def _scc_second_pass(self) -> None:
        rows: list[dict] = []
        async with self.neo4j_driver.session() as session:
            res = await session.run(
                "MATCH (f:Function) WHERE f.scc_id IS NOT NULL "
                "RETURN f.address AS address, f.scc_id AS sid "
                "ORDER BY f.scc_id, f.address"
            )
            rows = [r async for r in res]
        groups: dict[str, list[str]] = {}
        for r in rows:
            addr = r.get("address")
            if addr:
                groups.setdefault(r.get("sid"), []).append(addr)
        for addrs in groups.values():
            for addr in addrs:
                try:
                    await self._scc_function_pass(addr)
                except Exception:
                    log.exception("Pass1Dispatcher: SCC pass failed for %s", addr)
            await self._log("pass1_scc_done", {"group": addrs})

    async def _scc_function_pass(self, func_addr: str) -> None:
        async with self._semaphore:
            for arg_id in await self._query_nodes(func_addr, "Argument"):
                submission = await self.rename_agent.run(
                    func_addr, arg_id, include_callee_renames=True
                )
                await self._apply_submission(submission)
            summary = await self._function_summary(func_addr)
            if summary is not None:
                await self._apply_summary(func_addr, summary)

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

    async def _query_nodes(self, func_addr: str, label: str) -> list[str]:
        rows: list[dict] = []
        query = (
            "MATCH (f:Function {address: $fa})-[:CONTAINS]->(n:" + label + ") "
            "WHERE NOT coalesce(n.pinned, false) RETURN n.id AS id"
        )
        async with self.neo4j_driver.session() as session:
            res = await session.run(query, fa=func_addr)
            rows = [r async for r in res]
        return [str(r["id"]) for r in rows if r.get("id")]

    async def _function_summary(self, func_addr: str) -> FunctionSummary | None:
        context = await self.context_asm.for_function(func_addr, include_callees=True)
        session_id = f"summary_{func_addr}"
        try:
            await self.llm.create_session(session_id, self.summary_prompt)
            response = await self.llm.send(
                session_id,
                context,
                thinking_level=self.config.get("thinking_levels", {}).get(
                    _LEVELS_KEY, "minimal"
                ),
                structured_output=get_schema(FunctionSummary),
            )
            return parse_response(FunctionSummary, response)
        finally:
            self.llm.destroy_session(session_id)

    async def _apply_summary(self, func_addr: str, summary: FunctionSummary) -> None:
        await self._merge_rename(
            Rename(node_id=func_addr, llm_name=summary.llm_name,
                   canon_name=summary.canon_name, justification="pass1 summary")
        )
        try:
            if self.bndb_writer is not None:
                self.bndb_writer.rename_function(
                    int(func_addr, 16), summary.llm_name, summary.canon_name
                )
        except Exception:
            log.exception("Pass1Dispatcher: bndb rename_function failed for %s", func_addr)
        if self.ledger is not None:
            try:
                await self.ledger.set_function_names(
                    func_addr, summary.llm_name, summary.canon_name
                )
                await self.ledger.set_function_summary(func_addr, summary.summary)
            except Exception:
                log.exception("Pass1Dispatcher: ledger summary failed for %s", func_addr)

    async def _apply_submission(self, submission) -> None:
        for rn in submission.renames:
            if not rn.node_id or not rn.llm_name:
                continue
            await self._merge_rename(rn)
            try:
                if self.bndb_writer is None:
                    continue
                if ":" in rn.node_id:
                    addr = int(rn.node_id.split(":")[0], 16)
                    self.bndb_writer.rename_variable(
                        addr, rn.node_id, rn.llm_name, rn.canon_name
                    )
                else:
                    self.bndb_writer.rename_function(
                        int(rn.node_id, 16), rn.llm_name, rn.canon_name
                    )
            except Exception:
                log.exception("Pass1Dispatcher: bndb apply failed for %s", rn.node_id)

    async def _merge_rename(self, rn: Rename) -> None:
        async with self.neo4j_driver.session() as session:
            if ":" in rn.node_id:
                await session.run(
                    "MERGE (n {id: $id}) "
                    "SET n.llm_name = $llm, n.canon_name = $canon",
                    {"id": rn.node_id, "llm": rn.llm_name, "canon": rn.canon_name},
                )
            else:
                await session.run(
                    "MERGE (n:Function {address: $id}) "
                    "SET n.llm_name = $llm, n.canon_name = $canon",
                    {"id": rn.node_id, "llm": rn.llm_name, "canon": rn.canon_name},
                )

    async def _log(self, event_type: str, data: dict) -> None:
        try:
            if self.tracer is not None:
                await self.tracer.log(event_type, "pass1", data)
        except Exception:
            log.debug("Pass1Dispatcher: tracer.log(%s) failed", event_type)


__all__ = ["Pass1Dispatcher"]
