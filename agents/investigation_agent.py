"""Investigation agent — Deliverable 7.2.

Executes a single TODO task and returns an ``InvestigationResult`` (answer +
claims + rich subtask TaskSpecs). The harness-side completion flow (claims →
critic, subtasks → todo with dependency, complete/requeue) lives in the
InvestigationLoop (7.3), NOT here.
"""

from __future__ import annotations

import logging
from pathlib import Path

from reaper.harness.submission import InvestigationResult, get_schema, parse_response

log = logging.getLogger(__name__)


class InvestigationAgent:
    """Executes one TODO task (design § 7.2)."""

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
        return str(Path(__file__).resolve().parent / "prompts" / "investigation_agent.txt")

    async def run(self, task: dict) -> InvestigationResult:
        """Assemble task context, call the LLM, return InvestigationResult."""
        context = await self.context_asm.for_task(task)
        session_id = f"investigation_{task.get('id', '?')}"
        try:
            await self.llm.create_session(session_id, self.prompt)
            response = await self.llm.send(
                session_id,
                context,
                thinking_level=self.config.get("thinking_levels", {}).get(
                    "investigation", "high"
                ),
                structured_output=get_schema(InvestigationResult),
            )
            result = parse_response(InvestigationResult, response)
        finally:
            self.llm.destroy_session(session_id)

        try:
            if self.tracer is not None:
                await self.tracer.log(
                    "investigation_agent", session_id,
                    {"task_id": task.get("id"), "answer": result.answer[:200],
                     "claims": len(result.claims), "subtasks": len(result.subtasks)},
                )
        except Exception:
            log.debug("investigation agent: tracer.log failed")
        return result


__all__ = ["InvestigationAgent"]
