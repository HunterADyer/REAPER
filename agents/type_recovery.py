"""Type recovery agent — Deliverable 4.2.

Takes StructCandidate groups (4.1), infers a struct layout via the LLM, and
records a claim in the ledger. Runs as part of pass 0 (type recovery), BEFORE
Pass 1 / Pass 2 / 3.x shadow & merge.

Output: ``StructDefinition`` (from reaper.harness.submission, shared protocol).
``run()`` returns ``None`` when the LLM concludes no struct exists (empty
``fields``), so the caller can skip the rebuild for that candidate.

Claim discipline: one claim per involved function, truth_level="inferred" for
v1 (the design says the critic may revisit it later; for the first
implementation we set it directly).
"""

from __future__ import annotations

import logging
from pathlib import Path

from reaper.harness.submission import StructDefinition, get_schema, parse_response

log = logging.getLogger(__name__)


class TypeRecoveryAgent:
    """Infer a C struct layout from a StructCandidate via the LLM (4.2)."""

    def __init__(self, llm_client, context_asm, config: dict):
        self.llm = llm_client
        self.context_asm = context_asm
        self.config = config or {}
        with open(self._prompt_path(), encoding="utf-8") as fh:
            self.prompt = fh.read()

    @staticmethod
    def _prompt_path() -> str:
        return str(Path(__file__).resolve().parent / "prompts" / "type_recovery.txt")

    def _ledger(self):
        return getattr(self.context_asm, "ledger", None)

    async def run(self, candidate) -> StructDefinition | None:
        """Assemble context, call the LLM, return a parsed StructDefinition.

        Returns ``None`` (no struct inferred) only when the model emits an
        empty fields list. A claim is recorded per involved function with
        truth_level "inferred" for v1.
        """
        session_id = f"type_recovery_{candidate.candidate_id}"
        try:
            await self.llm.create_session(session_id, self.prompt)
            context = await self.context_asm.for_struct_candidate(candidate)
            response = await self.llm.send(
                session_id,
                context,
                thinking_level=self.config.get("thinking_levels", {}).get(
                    "pass0_type_recovery", "high"
                ),
                structured_output=get_schema(StructDefinition),
            )
            result = parse_response(StructDefinition, response)
        finally:
            self.llm.destroy_session(session_id)

        if not result.fields:
            return None

        await self._record_claim(candidate, result, session_id)
        return result

    async def _record_claim(self, candidate, struct_def: StructDefinition,
                            submitted_by: str) -> None:
        ledger = self._ledger()
        if ledger is None:
            return
        functions = [a for a in (candidate.functions_involved or []) if a]
        if not functions:
            return
        evidence = [
            {
                "address_start": a.instruction_address,
                "address_end": a.instruction_address,
                "description": (
                    f"field access +0x{a.offset:x} ({a.access_type}, "
                    f"{a.size}B) in function {a.function_address}"
                ),
            }
            for a in candidate.accesses
        ]
        claim_text = (
            f"Type recovery ({candidate.base_type_hint or candidate.candidate_id}): "
            f"these functions access a shared struct '{struct_def.struct_name}' "
            f"with {len(struct_def.fields)} fields"
        )
        for func_addr in functions:
            try:
                claim_id = await ledger.add_claim(
                    func_addr, claim_text, submitted_by, evidence
                )
                await ledger.set_truth_level(claim_id, "inferred", submitted_by)
            except Exception:
                log.exception("type recovery: ledger claim failed for %s", func_addr)


__all__ = ["TypeRecoveryAgent"]
