"""Resynthesis agent — Deliverable 7.4.

Reviews the evidence landscape for a resynthesis GROUP (SCC members, struct
consumers, or direct call neighborhoods — see compute_resynthesis_groups in
reaper/tools/graph_analysis.py) and returns a ResynthesisResult: contradictions
to investigate, duplicate claims to merge, and new atomic investigation tasks.
"""

from __future__ import annotations

import logging
from pathlib import Path

from reaper.harness.submission import ResynthesisResult, get_schema, parse_response

log = logging.getLogger(__name__)


class ResynthesisAgent:
    """Analyzes one resynthesis group for contradictions/duplicates (7.4)."""

    def __init__(self, llm_client, context_asm, ledger, todo, tracer, config: dict):
        self.llm = llm_client
        self.context_asm = context_asm
        self.ledger = ledger
        self.todo = todo
        self.tracer = tracer
        self.config = config or {}
        with open(self._prompt_path(), encoding="utf-8") as fh:
            self.prompt = fh.read()

    @staticmethod
    def _prompt_path() -> str:
        return str(Path(__file__).resolve().parent / "prompts" / "resynthesis_agent.txt")

    async def run(self, context: str) -> ResynthesisResult:
        """Single LLM call with for_subgraph context → ResynthesisResult."""
        session_id = f"resynthesis"
        try:
            await self.llm.create_session(session_id, self.prompt)
            response = await self.llm.send(
                session_id,
                context,
                thinking_level=self.config.get("thinking_levels", {}).get(
                    "resynthesis", "high"
                ),
                structured_output=get_schema(ResynthesisResult),
            )
            result = parse_response(ResynthesisResult, response)
        finally:
            self.llm.destroy_session(session_id)

        try:
            if self.tracer is not None:
                await self.tracer.log(
                    "resynthesis_agent", session_id,
                    {"contradictions": len(result.contradictions),
                     "merged_claims": len(result.merged_claims),
                     "new_tasks": len(result.new_tasks)},
                )
        except Exception:
            log.debug("resynthesis agent: tracer.log failed")
        return result


__all__ = ["ResynthesisAgent"]
