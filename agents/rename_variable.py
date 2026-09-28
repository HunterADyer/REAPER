"""Variable rename agent — Deliverable 5.1 (Pass 1, leaf level).

Renames ONE variable from local HLIL context with minimal thinking budget.
Assembles context through ``context_asm.for_variable()`` (which highlights the
target with ``>>> name <<<``), calls the LLM with the Submission schema, and
returns the parsed Submission (expected to contain exactly one Rename).
"""

from __future__ import annotations

import logging
from pathlib import Path

from reaper.harness.submission import Submission, get_schema, parse_response

log = logging.getLogger(__name__)


class RenameVariableAgent:
    """Single-variable rename agent (design § 5.1)."""

    def __init__(self, llm_client, context_asm, tracer, config: dict):
        self.llm = llm_client
        self.context_asm = context_asm
        self.tracer = tracer
        self.config = config or {}
        with open(self._prompt_path(), encoding="utf-8") as fh:
            self.prompt = fh.read()

    @staticmethod
    def _prompt_path() -> str:
        return str(Path(__file__).resolve().parent / "prompts" / "rename_variable.txt")

    async def run(
        self,
        func_address: str,
        var_id: str,
        include_callee_renames: bool = False,
    ) -> Submission:
        """Return a Submission with one Rename for the highlighted variable."""
        context = await self.context_asm.for_variable(
            func_address, var_id, include_callee_renames=include_callee_renames
        )
        session_id = f"rename_{func_address}_{var_id}"
        try:
            await self.llm.create_session(session_id, self.prompt)
            response = await self.llm.send(
                session_id,
                context,
                thinking_level=self.config.get("thinking_levels", {}).get(
                    "pass1_rename", "minimal"
                ),
                structured_output=get_schema(Submission),
            )
            result = parse_response(Submission, response)
        finally:
            self.llm.destroy_session(session_id)

        # Normalize bare variable names to the STABLE node id. A rename to a
        # variable inside THIS function must carry the full
        # "<func_addr>:<name>" id — the graph/builders address nodes by that
        # stable id. The model is told to echo the id it was given, but live
        # models frequently return just the bare suffix (e.g. "zmm15" instead
        # of "0x1400:zmm15"), which Pass 1's _apply_submission would otherwise
        # mis-parsed as a FUNCTION address (spurious :Function nodes). Because
        # this run() is strictly one-variable-per-call scoped to
        # ``func_address``, prefixing is always correct.
        for rename in result.renames:
            if rename.node_id and ":" not in str(rename.node_id):
                rename.node_id = f"{func_address}:{rename.node_id}"

        try:
            if self.tracer is not None:
                await self.tracer.log(
                    "rename_agent", session_id,
                    {"func_address": func_address, "var_id": var_id,
                     "renames": [r.model_dump() for r in result.renames]},
                )
        except Exception:
            log.exception("rename agent: tracer.log failed")

        return result


__all__ = ["RenameVariableAgent"]
