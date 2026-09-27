"""Review agent — Deliverable 6.2.

Evaluates Pass 1 labels and produces corrections, evidenced claims, and
concrete TODO tasks in ONE structured ReviewOutput response. Read-only by
design — the Pass 2 dispatcher applies the corrections through the critic plus
merge pipeline.
"""

from __future__ import annotations

import logging
from pathlib import Path

from reaper.harness.submission import ReviewOutput, get_schema, parse_response

log = logging.getLogger(__name__)


class ReviewAgent:
    """Deep review of one function (design § 6.2)."""

    def __init__(self, llm_client, context_asm, tracer, config: dict):
        self.llm = llm_client
        self.context_asm = context_asm
        self.tracer = tracer
        self.config = config or {}
        with open(self._prompt_path(), encoding="utf-8") as fh:
            self.prompt = fh.read()

    @staticmethod
    def _prompt_path() -> str:
        return str(Path(__file__).resolve().parent / "prompts" / "review_agent.txt")

    async def run(self, func_address: str) -> ReviewOutput:
        """Review one function and return renames + claims + tasks."""
        context = await self.context_asm.for_function(
            func_address, include_callees=True, include_claims=True
        )
        session_id = f"review_{func_address}"
        try:
            await self.llm.create_session(session_id, self.prompt)
            response = await self.llm.send(
                session_id,
                context,
                thinking_level=self.config.get("thinking_levels", {}).get(
                    "pass2_review", "max"
                ),
                structured_output=get_schema(ReviewOutput),
            )
            result = parse_response(ReviewOutput, response)
        finally:
            self.llm.destroy_session(session_id)

        try:
            if self.tracer is not None:
                await self.tracer.log(
                    "review_agent", session_id,
                    {"func_address": func_address,
                     "renames": len(result.renames), "claims": len(result.claims),
                     "tasks": len(result.tasks)},
                )
        except Exception:
            log.debug("review agent: tracer.log failed")
        return result


__all__ = ["ReviewAgent"]
