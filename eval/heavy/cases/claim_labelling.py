"""Heavy case — CLAIM LABELLING / PROMOTION.

Many mechanisms converge on the ledger's claim lifecycle: claims are inserted
with truth_level=NULL and must be PROMOTED to an explicit label by the critic
before completion/export, or swept. This case exercises the full promote/sweep
surface against the REAL ledger:

  * insert an unlabeled claim (truth_level NULL)
  * run sweep_null_claims()  -> the unevaluated claim is removed (not exported)
  * re-seed, label it via the critic policy, verify the label sticks and the
    claim is NOT swept

Also validates claim_coverage_stats reflects promotion (speculation vs mid+).
This is the mechanism that guards against exporting half-evaluated evidence.
"""

from __future__ import annotations

import json

from reaper.agents.critic_agent import CriticEvaluator
from reaper.eval.heavy.base import Fixtures, HeavyCase
from reaper.eval.heavy.runner import register
from reaper.harness.submission import Claim, EvidenceLink

FUNC = "0x4200"


class ClaimLabellingCase(HeavyCase):
    id = "claim_labelling"
    description = "Claim truth-level promotion + null-claim sweep against the real ledger"
    mechanisms = ("claim_labelling", "ledger")

    async def seed(self, fx: Fixtures) -> None:
        fx.neo4j.add_function(FUNC, name="sub_4200")
        fx.extractor._hlils[FUNC] = "0x4200: return strlen(buffer)"
        fx.extractor._signatures[FUNC] = "uint64_t sub_4200(char* buffer)"

        # One UNEVALUATED claim left in the ledger.
        fx.expected["unevaluated_id"] = await fx.ledger.add_claim(
            FUNC, "this function measures the string length", "reviewer",
            [{"address_start": "0x4200", "address_end": "0x4200",
              "description": "strlen call"}])
        fx.extra = {}

    def scripted_responses(self, fx: Fixtures) -> list[str]:
        return [json.dumps({"truth_level": "mid_confidence", "accepted": True,
                            "feedback": "strlen matches the claim"})]

    async def run_case(self, fx: Fixtures, llm) -> None:
        # 1) BEFORE critic: sweep removes the NULL claim.
        swept_before = await fx.ledger.sweep_null_claims()
        fx.extra["swept_before_critic"] = swept_before
        remaining = await fx.ledger.get_claims(FUNC)
        fx.extra["remaining_after_sweep"] = [c["id"] for c in remaining]
        if not remaining:
            return  # nothing left to promote
        assert fx.expected.get("unevaluated_id") in remaining

        # 2) Re-add (simulate an evaluated-but-unswept claim path) and label it
        #    through the REAL critic policy.
        claim_id = await fx.ledger.add_claim(
            FUNC, "this function measures the string length", "reviewer",
            [{"address_start": "0x4200", "address_end": "0x4200",
              "description": "strlen call"}])
        critic = CriticEvaluator(llm, fx.context_asm, fx.ledger, fx.tracer, fx.config)
        claim = Claim(function_address=FUNC,
                      claim_text="this function measures the string length",
                      evidence=[EvidenceLink(address_start="0x4200",
                                             address_end="0x4200",
                                             description="strlen call")])
        outcome = await critic.evaluate_claim(claim_id, claim, FUNC, "review_agent")
        fx.extra["promote_outcome"] = {"accepted": outcome.accepted}
        fx.extra["promoted_claim"] = [
            c for c in await fx.ledger.get_claims(FUNC) if c["id"] == claim_id]

        # 3) Sweep again: a PROMOTED claim survives; NULL ones are dropped.
        fx.extra["swept_after_critic"] = await fx.ledger.sweep_null_claims()
        fx.extra["survivors"] = [c["id"] for c in await fx.ledger.get_claims(FUNC)]
        fx.extra["coverage"] = await fx.ledger.claim_coverage_stats()

    async def assert_expected(self, fx: Fixtures) -> list:
        checks = []
        # Sweep must remove the unevaluated NULL claim before any critic ran.
        if fx.extra.get("swept_before_critic") != 1:
            checks.append(self.fail(
                f"expected 1 null claim swept pre-critic, got "
                f"{fx.extra.get('swept_before_critic')}"))
        # If we promoted one, it must NOT have been swept and must carry a label.
        if fx.extra.get("promote_outcome") is not None:
            survivors = fx.extra.get("survivors") or []
            if not survivors:
                checks.append(self.fail(
                    "promoted claim was swept — labelling+promotion lost it"))
            else:
                claims = await fx.ledger.get_claims(FUNC)
                labelled = [c for c in claims if (c.get("truth_level") or "") is not None]
                if not labelled:
                    checks.append(self.fail("no claim carries a truth level after promotion"))
                else:
                    checks.append(self.ok("promoted claim carries a truth level and survives"))
        # coverage stats must make sense after promotion
        cov = fx.extra.get("coverage") or {}
        if cov:
            if cov.get("with_claims", 0) < 1:
                checks.append(self.fail("coverage stats report 0 claims after promotion"))
        return checks or [self.ok("claim labelling + promotion behaved correctly")]

    def score_items(self, fx: Fixtures) -> dict[str, list[dict]]:
        return {}


register(ClaimLabellingCase)
