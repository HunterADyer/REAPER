"""Critic agent — Deliverable 6.1.

Shared harness class ``CriticEvaluator``. Build this FIRST — both Pass 2 (6.3)
and the investigation loop (7.3) depend on it.

Evaluates a claim (already inserted in the ledger with truth_level=NULL)
against its cited evidence + function context and returns a ``CriticOutcome``.
Rejection tracking is per ``(function_address, agent_role)`` in the in-memory
``_rejection_counts`` dict (design § 6.1) — NOT on the claim row, so a retry
that produces a different claim still counts toward the limit:
  - verdict.accepted -> set_truth_level(claim_id, verdict.truth_level); done.
  - rejected and count < max_critic_rejections -> DELETE the claim so the
    caller can re-invoke the submitting agent with feedback.
  - rejected and count >= max_critic_rejections -> force-accept at
    'speculation' and reset the counter (move on).
"""

from __future__ import annotations

import logging
from pathlib import Path

from reaper.harness.submission import (
    CriticOutcome,
    CriticVerdict,
    get_schema,
    parse_response,
)

log = logging.getLogger(__name__)


def _evidence_dicts(claim) -> list[dict]:
    """Normalize evidence into the list[dict] form for_context.for_evidence."""
    out = []
    for e in (claim.evidence if hasattr(claim, "evidence") else []) or []:
        if hasattr(e, "model_dump"):
            out.append(e.model_dump())
        elif isinstance(e, dict):
            out.append(dict(e))
    return out


class CriticEvaluator:
    """Wraps the LLM-based critic with per-(func, role) rejection tracking."""

    def __init__(self, llm_client, context_asm, ledger, tracer, config: dict):
        self.llm = llm_client
        self.context_asm = context_asm
        self.ledger = ledger
        self.tracer = tracer
        self.config = config or {}
        self._rejection_counts: dict[tuple[str, str], int] = {}
        limits = self.config.get("limits") or {}
        self._max_rejections = int(limits.get("max_critic_rejections", 3))
        with open(self._prompt_path(), encoding="utf-8") as fh:
            self.prompt = fh.read()

    @staticmethod
    def _prompt_path() -> str:
        return str(Path(__file__).resolve().parent / "prompts" / "critic_agent.txt")

    async def evaluate_claim(
        self,
        claim_id: int,
        claim,
        func_addr: str,
        agent_role: str,
    ) -> CriticOutcome:
        """Judge ``claim`` (already in ledger) against evidence + context."""
        evidence_text = ""
        try:
            evidence_text = await self.context_asm.for_evidence(_evidence_dicts(claim))
        except Exception:
            log.exception("critic: for_evidence failed for claim %s", claim_id)

        func_text = ""
        try:
            func_text = await self.context_asm.for_function(func_addr)
        except Exception:
            log.exception("critic: for_function failed for %s", func_addr)

        payload = (
            f"CLAIM:\n{claim.claim_text if hasattr(claim, 'claim_text') else claim}\n\n"
            f"FUNCTION CONTEXT:\n{func_text or '(unavailable)'}\n\n"
            f"EVIDENCE:\n{evidence_text or '(none provided)'}"
        )
        session_id = f"critic_{claim_id}"
        try:
            await self.llm.create_session(session_id, self.prompt)
            response = await self.llm.send(
                session_id,
                payload,
                thinking_level=self.config.get("thinking_levels", {}).get(
                    "critic", "high"
                ),
                structured_output=get_schema(CriticVerdict),
            )
            verdict = parse_response(CriticVerdict, response)
        finally:
            self.llm.destroy_session(session_id)

        return await self._apply_verdict(
            claim_id, verdict, func_addr, agent_role, session_id
        )

    async def _apply_verdict(
        self,
        claim_id: int,
        verdict: CriticVerdict,
        func_addr: str,
        agent_role: str,
        reviewed_by: str,
    ) -> CriticOutcome:
        if verdict.accepted:
            await self._set_level(claim_id, verdict.truth_level, reviewed_by)
            self._rejection_counts.pop((func_addr, agent_role), None)
            await self._log(
                "claim_accepted", claim_id, func_addr, agent_role, reviewed_by,
                {"truth_level": verdict.truth_level},
            )
            return CriticOutcome(accepted=True, feedback=verdict.feedback)

        key = (func_addr, agent_role)
        self._rejection_counts[key] = self._rejection_counts.get(key, 0) + 1
        if self._rejection_counts[key] >= self._max_rejections:
            # Give up on this (func, role) — force-accept at 'speculation'.
            await self._set_level(claim_id, "speculation", reviewed_by)
            self._rejection_counts.pop(key, None)
            await self._log(
                "claim_force_accepted", claim_id, func_addr, agent_role, reviewed_by,
                {"feedback": verdict.feedback, "count": self._rejection_counts.get(key, 0)},
            )
            return CriticOutcome(accepted=True, feedback=verdict.feedback)
        # Delete the rejected claim so the caller can have the agent retry.
        await self._delete(claim_id)
        await self._log(
            "claim_rejected", claim_id, func_addr, agent_role, reviewed_by,
            {"feedback": verdict.feedback, "count": self._rejection_counts[key]},
        )
        return CriticOutcome(accepted=False, feedback=verdict.feedback)

    async def _set_level(self, claim_id, truth_level, reviewed_by) -> None:
        try:
            await self.ledger.set_truth_level(claim_id, truth_level, reviewed_by)
        except Exception:
            log.exception("critic: set_truth_level(%s, %s) failed", claim_id, truth_level)

    async def _delete(self, claim_id) -> None:
        try:
            await self.ledger.delete_claim(claim_id)
        except Exception:
            log.exception("critic: delete_claim(%s) failed", claim_id)

    async def _log(self, event_type, claim_id, func_addr, agent_role, session_id, data) -> None:
        if self.tracer is None:
            return
        try:
            await self.tracer.log(
                event_type, session_id,
                {**data, "claim_id": claim_id, "func_addr": func_addr,
                 "agent_role": agent_role},
            )
        except Exception:
            log.debug("critic: tracer.log(%s) failed", event_type)


__all__ = ["CriticEvaluator"]
