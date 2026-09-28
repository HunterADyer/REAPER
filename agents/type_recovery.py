"""Type recovery agent — Deliverable 4.2.

Takes StructCandidate groups (4.1) — which may span MULTIPLE functions via
call-context sharing — and returns a TypeVerdict: does the LLM ACCEPT that
these functions share one data type, and if so what is its complete layout
and proposed name? Runs as part of phase 4 (type recovery), BEFORE
Pass 0 / ratify.

Verdict vs the old StructDefinition contract: acceptance is the GATE. The
detector may over-merge (union-find over call-flow is aggressive); the verdict
is where a careful reverse engineer decides whether the shared-data-type claim
holds. The verdict commits the name + complete unioned field set ONLY when
``accepted``. Rejected candidates are recorded to the ledger rejections table
for hand-tuning (never applied, never renamed).

Thinking budget by ROUND (audit 2026-09-27): the initial pass-0 recovery runs
at ``pass0_type_recovery`` ("high"). Any FOLLOW-UP recovery round (a struct
applied in an earlier round exposes NEW struct-access patterns) runs at
``recovery_followup`` ("max" = xhigh) via ``run(..., follow_up=True)``. See
run.py Phase 4 for the fixed-point driver that selects the round.

Claim discipline: one claim per involved function, truth_level="inferred" for
v1 (the design says the critic may revisit it later).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from reaper.harness.submission import (
    StructDefinition,
    TypeVerdict,
    get_schema,
    parse_response,
)

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

    async def run(
        self,
        candidate,
        *,
        follow_up: bool = False,
    ) -> TypeVerdict:
        """Assemble context, call the LLM, return a parsed TypeVerdict.

        ``follow_up=False`` (pass 0 / first round) uses the
        ``pass0_type_recovery`` level (default "high"). ``follow_up=True``
        (a refinement round after a prior struct application exposed new
        access patterns) uses ``recovery_followup`` (default "max" = xhigh).

        The verdict carries ``accepted``; when accepted a claim is recorded per
        involved function with truth_level "inferred". When rejected, a row is
        recorded in the ledger's type_rejections table and nothing is applied
        (the caller must check ``verdict.accepted`` before rebinding).
        """
        session_id = f"type_recovery_{candidate.candidate_id}"
        context = None
        try:
            await self.llm.create_session(session_id, self.prompt)
            context = await self.context_asm.for_struct_candidate(candidate)
            levels = self.config.get("thinking_levels", {})
            thinking_level = (
                levels.get("recovery_followup", "max")
                if follow_up else levels.get("pass0_type_recovery", "high")
            )
            response = await self.llm.send(
                session_id,
                context,
                thinking_level=thinking_level,
                structured_output=get_schema(TypeVerdict),
            )
            verdict = parse_response(TypeVerdict, response)
        finally:
            self.llm.destroy_session(session_id)

        if not verdict.accepted:
            await self._record_rejection(candidate, verdict)
            return verdict

        # An ACCEPTED verdict with no fields is the "no struct exists" case.
        if not verdict.fields:
            return verdict

        struct_def = StructDefinition(
            struct_name=verdict.struct_name,
            kind=verdict.kind,
            fields=verdict.fields,
        )
        await self._record_claim(candidate, struct_def, session_id)
        verdict._struct_def = struct_def
        return verdict

    def to_struct_def(self, verdict: TypeVerdict) -> Optional[StructDefinition]:
        """Extract the advisory StructDefinition from an accepted verdict."""
        return getattr(verdict, "_struct_def", None)

    async def _record_rejection(self, candidate, verdict: TypeVerdict) -> None:
        ledger = self._ledger()
        if ledger is None:
            return
        try:
            await ledger.record_type_rejection({
                "candidate_id": candidate.candidate_id,
                "base_type_hint": candidate.base_type_hint,
                "rejection_reason": verdict.rejection_reason,
                "functions_involved": list(candidate.functions_involved or []),
            })
        except Exception:
            log.exception("type recovery: record_type_rejection failed for %s",
                          candidate.candidate_id)

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


__all__ = ["TypeRecoveryAgent"]
