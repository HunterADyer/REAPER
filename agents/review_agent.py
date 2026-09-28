"""Review agent — Deliverable 6.2.

Evaluates Pass 1 labels and produces corrections, evidenced claims, and
concrete TODO tasks in ONE structured ReviewOutput response. Read-only by
design — the Pass 2 dispatcher applies the corrections through the critic plus
merge pipeline.
"""

from __future__ import annotations

import logging
from pathlib import Path

from reaper.harness.submission import (
    ReviewOutput,
    get_schema,  # noqa: F401  (kept for API parity)
    parse_response,  # noqa: F401  (kept for API parity)
    send_structured,
)

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

    async def run(self, func_address: str, prior_feedback: str = "") -> ReviewOutput:
        """Review one function and return renames + claims + tasks.

        ``prior_feedback`` is the critic's aggregated rejection feedback from a
        previous attempt (design § 6.1 retry-with-feedback). When provided it
        is appended to the context so the model can correct its submission.
        """
        context = await self.context_asm.for_function(
            func_address, include_callees=True, include_claims=True
        )
        if prior_feedback:
            context += (
                "\n\nPREVIOUS ATTEMPT WAS REJECTED BY THE CRITIC. "
                "Fix the submission accordingly:\n" + prior_feedback
            )
        session_id = f"review_{func_address}"
        try:
            await self.llm.create_session(session_id, self.prompt)
            result = await send_structured(
                self.llm,
                session_id,
                context,
                ReviewOutput,
                thinking_level=self.config.get("thinking_levels", {}).get(
                    "pass2_review", "max"
                ),
            )
        finally:
            self.llm.destroy_session(session_id)

        # Normalize bare rename ids to the STABLE "<func>:<name>" node id
        # (same defense as RenameVariableAgent). Review is scoped to one
        # function; any rename whose id has no ':' must belong to it.
        for rename in result.renames:
            if rename.node_id and ":" not in str(rename.node_id):
                rename.node_id = f"{func_address}:{rename.node_id}"

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
