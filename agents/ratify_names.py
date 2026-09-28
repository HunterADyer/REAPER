"""Ratify names agent — the deterministic Pass-1 xhigh naming decision.

Reviews the chain context of ONE function (HLIL + current graph names of the
function and every variable/argument + callee/caller context + string refs +
pinned symbols) and emits a FINAL approve-or-rename decision per naming entity,
each grounded in cited evidence. This is the deliberate replacement for the
critic retry loop: the decision is deterministic, published once to the
ledger's ``name_decisions`` table, and the rename is applied exactly once.

Design (user, 2026-09-28): RE degrades to a deterministic 2-pass —
  Pass 0 : cheapest sweep of provisional names (existing Pass 1 machinery).
  Pass 1 : ONE heavy xhigh run; the ratifier explores function chains, approves
           or disapproves every function + variable name, and renames once when
           disapproving, citing graph/HLIL evidence. The VR stage will refine
           claims with better tools later.
No critic, no investigation/resynthesis, no retry — decisions are final.
"""

from __future__ import annotations

import logging
from pathlib import Path

from reaper.harness.submission import RatifyOutput, get_schema, parse_response

log = logging.getLogger(__name__)


class RatifyNamesAgent:
    """One xhigh batched approve/rename decision per function (Pass 1)."""

    def __init__(self, llm_client, context_asm, tracer, config: dict):
        self.llm = llm_client
        self.context_asm = context_asm
        self.tracer = tracer
        self.config = config or {}
        with open(self._prompt_path(), encoding="utf-8") as fh:
            self.prompt = fh.read()

    @staticmethod
    def _prompt_path() -> str:
        return str(Path(__file__).resolve().parent / "prompts" / "ratify_names.txt")

    async def run(self, func_address: str) -> RatifyOutput:
        """Review one function's names; return final approve/rename decisions."""
        context = await self.context_asm.for_ratify(func_address)
        session_id = f"ratify_{func_address}"
        try:
            await self.llm.create_session(session_id, self.prompt)
            response = await self.llm.send(
                session_id,
                context,
                thinking_level=self.config.get("thinking_levels", {}).get(
                    "ratify", "xhigh"
                ),
                structured_output=get_schema(RatifyOutput),
            )
            result = parse_response(RatifyOutput, response)
        finally:
            self.llm.destroy_session(session_id)

        try:
            if self.tracer is not None:
                await self.tracer.log(
                    "ratify_agent", session_id,
                    {"func_address": func_address,
                     "decisions": len(result.decisions),
                     "renames": sum(1 for d in result.decisions
                                    if d.decision == "rename")},
                )
        except Exception:
            log.debug("ratify agent: tracer.log failed", exc_info=True)
        return result


__all__ = ["RatifyNamesAgent"]
