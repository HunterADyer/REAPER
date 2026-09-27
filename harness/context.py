"""Context assembler — Deliverable 3.1.

Takes graph/ledger state and produces formatted text for agent prompts.
Constructed by EVERY agent and the dispatchers (pipeline runner § 8.2), so the
constructor signature is fixed: ``ContextAssembler(extractor, neo4j_driver,
ledger, config)``. All data access is async (Neo4j reads, ledger reads); the
only synchronous calls are CPU-only extractor lookups.

Truncation: every method routes its assembled text through ``_finalize()``
which enforces ``config['limits']['max_context_tokens']`` (default 32768).
``for_subgraph`` additionally implements the three-step truncation ladder from
the design (§ 3.1): (1) drop claims below mid_confidence, (2) summarize HLIL
to signature + first/last 10 lines, (3) drop the least-connected functions.

Deliberately degrades gracefully: any Neo4j or ledger read failure is logged
and the missing section is simply omitted — context assembly must never crash
the pipeline.
"""

from __future__ import annotations

import logging
import re

from reaper.tools.struct_detector import FieldAccess  # noqa: F401
from reaper.tools.struct_detector import StructCandidate  # noqa: F401

log = logging.getLogger(__name__)

# Truth levels at or above "mid_confidence" survive truncation step 1.
_MID_OR_ABOVE = {"mid_confidence", "high_confidence"}

# Number of leading/trailing HLIL lines kept when summarizing (truncation step 2).
_SUMMARY_LINES = 10


class ContextAssembler:
    """Formats graph/ledger/extractor state into agent prompt text.

    Not a Pydantic model — a pure assembler.
    """

    def __init__(self, extractor, neo4j_driver, ledger, config: dict):
        self.extractor = extractor
        self.neo4j_driver = neo4j_driver
        self.ledger = ledger
        self.config = config or {}
        limits = self.config.get("limits") or {}
        self._max_tokens = int(limits.get("max_context_tokens", 32768))

    # ------------------------------------------------------------------ #
    # Public context methods (design § 3.1)
    # ------------------------------------------------------------------ #

    async def for_function(
        self,
        func_address: str,
        include_callees: bool = False,
        include_callers: bool = False,
        include_claims: bool = False,
    ) -> str:
        """Assemble context for a function.

        Always includes function signature, full HLIL, parameters, variables
        and string references. Optional sections controlled by the flags.
        """
        parts = []
        parts.append(f"Function: {self._signature_text(func_address)}")

        hl = self._hlil_text(func_address)
        if hl:
            parts.append(f"HLIL:\n{hl}")

        params = self._safe_extractor_list(self.extractor.get_parameters, func_address)
        if params:
            parts.append(
                "Parameters:\n"
                + "\n".join(
                    f"  {p.get('index', '?')}: {p.get('name', '?')} : {p.get('type', '?')}"
                    for p in params
                )
            )

        variables = self._safe_extractor_list(self.extractor.get_variables, func_address)
        if variables:
            parts.append(
                "Variables:\n"
                + "\n".join(f"  {v.get('name', '?')} : {v.get('type', '?')}" for v in variables)
            )

        refs = self._safe_extractor_list(self.extractor.get_string_refs, func_address)
        if refs:
            parts.append(
                "String refs:\n"
                + "\n".join(f'  {r.get("address")}: "{r.get("value")}"' for r in refs)
            )

        if include_claims:
            claims = await self._claims_section(func_address)
            if claims:
                parts.append(claims)
        if include_callees:
            callees = await self._callees_section(func_address)
            if callees:
                parts.append(callees)
        if include_callers:
            callers = await self._callers_section(func_address)
            if callers:
                parts.append(callers)

        return self._finalize("\n\n".join(parts))

    async def for_variable(
        self,
        func_address: str,
        var_id: str,
        include_callee_renames: bool = False,
    ) -> str:
        """Assemble context for renaming a specific variable.

        The HLIL has every occurrence of the target variable wrapped in
        ``>>> name <<<`` (the format the rename agent is trained on). Also
        includes string refs, pinned symbol context, and optionally each
        callee's current llm_name/canon_name from Neo4j.
        """
        var_name = str(var_id).split(":")[-1] if var_id else ""
        hl = self._hlil_text(func_address) or ""
        highlighted = self._highlight(var_name, hl, func_address)
        name = self._function_name(func_address)

        parts = [f"HLIL of function {name}:\n{highlighted}"]

        refs = self._safe_extractor_list(self.extractor.get_string_refs, func_address)
        if refs:
            parts.append(
                "String refs:\n"
                + "\n".join(f'  {r.get("address")}: "{r.get("value")}"' for r in refs)
            )

        pinned = await self._pinned_context()
        if pinned:
            parts.append(pinned)

        if include_callee_renames:
            callee_renames = await self._callee_rename_context(func_address)
            if callee_renames:
                parts.append(callee_renames)

        parts.append(f"TARGET: Rename the variable `{var_name}` highlighted with >>> <<< above.")
        return self._finalize("\n\n".join(parts))

    async def for_evidence(self, evidence_links: list[dict]) -> str:
        """Retrieve HLIL text for evidence address ranges."""
        parts = []
        for i, link in enumerate(evidence_links or []):
            start = link.get("address_start")
            end = link.get("address_end")
            desc = link.get("description", "")
            hl = self._evidence_hlil(start, end)
            parts.append(f"Evidence {i} [{start}..{end}]: {desc}\n{hl}")
        if not parts:
            return ""
        return self._finalize("\n\n".join(parts))

    async def for_struct_candidate(self, candidate) -> str:
        """Assemble context for type recovery from a StructCandidate.

        Groups the candidate's FieldAccess sites by function and retrieves the
        single HLIL instruction at each access site.
        """
        groups: dict[str, list] = {}
        for access in candidate.accesses:
            groups.setdefault(access.function_address, []).append(access)

        parts = [
            f"Struct candidate: {candidate.candidate_id} "
            f"(base type hint: {candidate.base_type_hint})"
        ]
        for func_addr in sorted(groups, key=lambda x: int(str(x), 16)):
            parts.append(f"Function {func_addr}:")
            for access in sorted(groups[func_addr], key=lambda a: a.offset):
                ins = self._instruction_text(func_addr, access.instruction_address)
                parts.append(f"  +0x{access.offset:x} [{access.access_type}, {access.size}B] {ins}")
        return self._finalize("\n".join(parts))

    async def for_task(self, task: dict) -> str:
        """Assemble context for an investigation task.

        Interprets ``task['context_spec']`` (schema documented in design § 1.6
        / submission.py TaskContextSpec) and decomposes into for_function()
        calls.
        """
        spec = task.get("context_spec") or {}
        functions = list(spec.get("functions") or [])
        if not functions and task.get("start_position"):
            functions = [task["start_position"]]
        include_callees = bool(spec.get("include_callees"))
        include_claims = bool(spec.get("include_claims"))
        include_neighborhood = bool(spec.get("include_neighborhood"))

        parts = []
        if task.get("description") or task.get("goal"):
            parts.append(f"TASK: {task.get('description', '')}\nGOAL: {task.get('goal', '')}")
        for f in functions:
            sub = await self.for_function(
                f,
                include_callees=include_callees,
                include_callers=include_neighborhood,
                include_claims=include_claims,
            )
            if sub:
                parts.append(sub)
        return self._finalize("\n\n".join(parts))


    async def for_subgraph(
        self,
        function_addresses: list[str],
        include_claims: bool = True,
    ) -> str:
        """Assemble context for a group of related functions (resynthesis).

        Applies the three-step truncation ladder from the design when the
        assembled text exceeds max_context_tokens.
        """
        funcs = [f for f in function_addresses if f]

        claims_by_func: dict[str, list] = {}
        if include_claims:
            for f in funcs:
                try:
                    claims_by_func[f] = await self.ledger.get_claims(f)
                except Exception:
                    log.exception("for_subgraph: ledger.get_claims(%s) failed", f)
                    claims_by_func[f] = []

        text = self._render_subgraph(funcs, claims_by_func)
        if self._token_count(text) <= self._max_tokens:
            return text

        log.warning(
            "for_subgraph(%d funcs): %d tokens exceeds max_context_tokens=%d — truncating",
            len(funcs), self._token_count(text), self._max_tokens,
        )

        # Step 1: drop claims below mid_confidence.
        text = self._render_subgraph(funcs, claims_by_func, min_truth="mid_confidence")
        if self._token_count(text) <= self._max_tokens:
            log.warning("for_subgraph: truncation step 1 (drop low-confidence claims)")
            return text

        # Step 2: summarize HLIL to signature + first/last 10 lines each.
        text = self._render_subgraph(
            funcs, claims_by_func, min_truth="mid_confidence", summarize_hlil=True
        )
        if self._token_count(text) <= self._max_tokens:
            log.warning("for_subgraph: truncation step 2 (summarize HLIL)")
            return text

        # Step 3: drop least-connected functions until it fits.
        connectivity = await self._connectivity(funcs)
        working = list(funcs)
        while len(working) > 1:
            drop = min(working, key=lambda f: (connectivity.get(f, 0), funcs.index(f)))
            working.remove(drop)
            text = self._render_subgraph(
                working, claims_by_func, min_truth="mid_confidence", summarize_hlil=True
            )
            if self._token_count(text) <= self._max_tokens:
                log.warning(
                    "for_subgraph: truncation step 3 (dropped %d least-connected funcs)",
                    len(funcs) - len(working),
                )
                return text
        return self._render_subgraph(
            working, claims_by_func, min_truth="mid_confidence", summarize_hlil=True
        )

    # ------------------------------------------------------------------ #
    # Rendering helpers
    # ------------------------------------------------------------------ #

    def _render_subgraph(self, funcs, claims_by_func, *,
                         min_truth=None, summarize_hlil=False) -> str:
        blocks = []
        for f in funcs:
            sig = self._signature_text(f) or str(f)
            blocks.append(f"=== Function {sig} ===")
            hl = self._hlil_text(f)
            if summarize_hlil and hl:
                hl = self._summarize_hlil(hl)
            blocks.append(f"HLIL:\n{hl}" if hl else "HLIL: (none)")
            claims = claims_by_func.get(f, [])
            if min_truth is not None:
                claims = [c for c in claims if self._keep_claim(c, min_truth)]
            if claims:
                lines = [
                    f"  [claim_id={c.get('id')}] {c.get('claim_text')} ({c.get('truth_level')})"
                    for c in claims
                ]
                blocks.append("Claims:\n" + "\n".join(lines))
        return "\n\n".join(blocks)

    def _keep_claim(self, claim: dict, min_truth: str) -> bool:
        level = claim.get("truth_level") or "speculation"
        if min_truth == "mid_confidence":
            return level in _MID_OR_ABOVE
        return True

    def _summarize_hlil(self, hl: str, keep: int = _SUMMARY_LINES) -> str:
        lines = hl.splitlines()
        if len(lines) <= 2 * keep:
            return hl
        omitted = len(lines) - 2 * keep
        return "\n".join(
            lines[:keep] + [f"... [{omitted} lines omitted] ..."] + lines[-keep:]
        )

    async def _connectivity(self, funcs: list[str]) -> dict[str, int]:
        """Count CALL edges per function (undirected) for step-3 dropping."""
        connectivity = {}
        for f in funcs:
            count = 0
            try:
                async with self.neo4j_driver.session() as session:
                    res = await session.run(
                        "MATCH (f:Function)-[:CALL]-(c:Function) "
                        "WHERE f.address = $id RETURN count(c) AS n",
                        id=f,
                    )
                    rows = [r async for r in res]
                if rows and "n" in rows[0]:
                    count = int(rows[0]["n"])
            except Exception:
                log.debug("connectivity(%s) query failed — assuming 0", f)
            connectivity[f] = count
        return connectivity

    def _hlil_text(self, addr):
        """Normalized full HLIL text for a function (str is the 2.1 contract)."""
        raw = self.extractor.get_function_hlil(addr)
        if isinstance(raw, str):
            return raw
        # Duck-typed HLIL function object (tests/fake_binja.py): render lines.
        instrs = getattr(raw, "instructions", None)
        if not instrs:
            return "" if raw is None else str(raw)
        lines = []
        for ins in instrs:
            ia = getattr(ins, "address", None)
            prefix = hex(ia) if isinstance(ia, int) else str(ia)
            lines.append(f"{prefix}: {ins}")
        return "\n".join(lines)


    def _signature_text(self, addr) -> str:
        sig = self.extractor.get_function_signature(addr)
        if isinstance(sig, str):
            return sig
        if isinstance(sig, dict):  # test double shape {name, return_type, params}
            params = ", ".join(str(p) for p in (sig.get("params") or []))
            return f"{sig.get('return_type', '?')} {sig.get('name', '?')}({params})"
        return "" if sig is None else str(sig)

    def _function_name(self, addr) -> str:
        try:
            fn = self.extractor.get_function(address=addr)
            name = getattr(fn, "name", None)
            if name:
                return name
        except Exception:
            pass
        sig = self._signature_text(addr) or ""
        if "(" in sig:
            head = sig.split("(")[0].strip().replace("*", " ")
            tokens = [t for t in head.split() if t]
            if tokens:
                return tokens[-1]
        return str(addr)

    @staticmethod
    def _highlight(var_name: str, hl: str, func_address: str) -> str:
        """Wrap every WORD-BOUNDED occurrence of ``var_name`` in >>> <<<.

        A plain ``str.replace`` corrupts short names: renaming ``c`` would also
        highlight the ``c`` inside ``calc``/``char``/``value``. We anchor the
        match on C-identifier boundaries so only the standalone token is marked.
        If the variable cannot be found (renamed in the live view), the HLIL is
        returned untouched rather than producing a misleading highlight.
        """
        if not var_name or not hl:
            return hl
        pattern = rf"(?<![A-Za-z0-9_]){re.escape(var_name)}(?![A-Za-z0-9_])"
        highlighted = re.sub(pattern, f">>> {var_name} <<<", hl)
        if ">>>" in highlighted:
            return highlighted
        # The live Binja variable may already carry its renamed value, in which
        # case the original id-based name has no occurrences. Fall back to the
        # function-level anchor so the agent still has unambiguous direction.
        return hl + f"\n(TARGET variable `{var_name}` for function {func_address})"

    def _safe_extractor_list(self, method, addr) -> list:
        try:
            result = method(addr)
        except Exception:
            log.exception("extractor call %s(%s) failed", getattr(method, "__name__", "?"), addr)
            return []
        return list(result) if result else []

    def _instruction_text(self, func_addr, instr_addr) -> str:
        target = int(str(instr_addr), 16)
        raw = self.extractor.get_function_hlil(func_addr)
        if isinstance(raw, str):
            needle = f"{hex(target)}:"
            for line in raw.splitlines():
                if line.strip().startswith(needle):
                    return line.strip()
            return f"(no instruction at {hex(target)})"
        found = self._find_instruction(getattr(raw, "instructions", []), target)
        return str(found) if found is not None else f"(no instruction at {hex(target)})"

    def _find_instruction(self, instrs, target, _seen=None):
        if _seen is None:
            _seen = set()
        for ins in instrs or []:
            if id(ins) in _seen:
                continue
            _seen.add(id(ins))
            if getattr(ins, "address", None) == target:
                return ins
            found = self._find_instruction(self._child_instructions(ins), target, _seen)
            if found is not None:
                return found
        return None

    @staticmethod
    def _child_instructions(ins) -> list:
        out = []
        for attr in ("src", "dest", "left", "right", "condition", "index", "init", "update"):
            child = getattr(ins, attr, None)
            if child is not None and getattr(child, "operation", None) is not None:
                out.append(child)
        for child in (getattr(ins, "operands", None) or ()):
            if getattr(child, "operation", None) is not None:
                out.append(child)
        return out

    def _evidence_hlil(self, start, end) -> str:
        if hasattr(self.extractor, "get_hlil_range"):
            try:
                return self.extractor.get_hlil_range(str(start), str(end)) or ""
            except Exception:
                log.exception("get_hlil_range(%s, %s) failed — falling back", start, end)
        try:
            funcs = self.extractor.bv.get_functions_containing(int(str(start), 16))
            if funcs:
                faddr = getattr(funcs[0], "start", None)
                if faddr is not None:
                    addr = f"0x{faddr:x}" if isinstance(faddr, int) else str(faddr)
                    return self._hlil_text(addr)
        except Exception:
            log.debug("evidence fallback lookup failed for %s", start)
        return f"[HLIL {start}..{end}]"


    # ------------------------------------------------------------------ #
    # Neo4j / ledger backed sections
    # ------------------------------------------------------------------ #

    async def _claims_section(self, func_address) -> str | None:
        try:
            claims = await self.ledger.get_claims(func_address)
        except Exception:
            log.exception("_claims_section: ledger.get_claims(%s) failed", func_address)
            return None
        if not claims:
            return None
        lines = [
            f"  [claim_id={c.get('id')}] {c.get('claim_text')} ({c.get('truth_level')})"
            for c in claims
        ]
        return "Claims:\n" + "\n".join(lines)

    async def _callees_section(self, func_address) -> str | None:
        rows = await self._query(
            "MATCH (f:Function)-[:CALL]->(c:Function) WHERE f.address = $id "
            "RETURN c.address AS id, c.llm_name AS llm_name, c.canon_name AS canon_name",
            func_address,
        )
        if not rows:
            return None
        lines = [
            f"  {r.get('id')}: {r.get('canon_name') or r.get('llm_name') or r.get('id')}"
            for r in rows
        ]
        return "Callees:\n" + "\n".join(lines)

    async def _callers_section(self, func_address) -> str | None:
        rows = await self._query(
            "MATCH (f:Function)<-[:CALL]-(p:Function) WHERE f.address = $id "
            "RETURN p.address AS id, p.llm_name AS llm_name, p.canon_name AS canon_name",
            func_address,
        )
        if not rows:
            return None
        lines = [
            f"  {r.get('id')}: {r.get('canon_name') or r.get('llm_name') or r.get('id')}"
            for r in rows
        ]
        return "Callers:\n" + "\n".join(lines)

    async def _callee_rename_context(self, func_address) -> str | None:
        rows = await self._query(
            "MATCH (f:Function)-[:CALL]->(c:Function) WHERE f.address = $id "
            "RETURN c.address AS id, c.llm_name AS llm_name, c.canon_name AS canon_name",
            func_address,
        )
        if not rows:
            return None
        lines = [
            f"  {r.get('id')}: llm_name={r.get('llm_name')} canon_name={r.get('canon_name')}"
            for r in rows
        ]
        return "Callee current renames:\n" + "\n".join(lines)

    async def _pinned_context(self) -> str | None:
        rows = await self._query(
            "MATCH (f:Function) WHERE f.pinned = true "
            "RETURN f.address AS id, f.llm_name AS llm_name, f.canon_name AS canon_name "
            "LIMIT 25",
            None,
        )
        if not rows:
            return None
        lines = [
            f"  {r.get('id')}: {r.get('canon_name') or r.get('llm_name') or r.get('id')}"
            for r in rows
        ]
        return "Pinned symbols:\n" + "\n".join(lines)


    async def _query(self, query: str, func_address) -> list[dict]:
        """Run a read query; return records as plain dicts (empty on failure)."""
        try:
            async with self.neo4j_driver.session() as session:
                if func_address is None:
                    res = await session.run(query)
                else:
                    res = await session.run(query, id=func_address)
                return [r async for r in res]
        except Exception:
            log.exception("Neo4j query failed: %s", query)
            return []

    # ------------------------------------------------------------------ #
    # Truncation
    # ------------------------------------------------------------------ #

    @staticmethod
    def _token_count(text: str) -> int:
        """Rough token estimate — chars/4 heuristic for truncation decisions."""
        return max(1, len(text or "") // 4)

    def _finalize(self, text: str) -> str:
        if not text:
            return text
        if self._token_count(text) <= self._max_tokens:
            return text
        log.warning("context exceeded max_context_tokens=%d — truncating", self._max_tokens)
        return self._truncate_lines(text)

    def _truncate_lines(self, text: str) -> str:
        lines = text.split("\n")
        kept = []
        used = 0
        for i, line in enumerate(lines):
            tokens = self._token_count(line)
            if kept and used + tokens > self._max_tokens:
                kept.append(f"... [{len(lines) - i} lines truncated]")
                break
            kept.append(line)
            used += tokens
        return "\n".join(kept)


__all__ = ["ContextAssembler"]

