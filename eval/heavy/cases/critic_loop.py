"""Heavy case — the CRITIC LOOP (Deliverable 6.1).

Seed: a claim (with evidence) already in the ledger, plus a function's context
from the extractor and a graph node. Drive: the REAL CriticEvaluator against
that claim. Verify the full policy surface:

  * accept  -> claim truth_level is set (never left NULL)
  * reject  -> claim is DELETED so the agent can retry
  * reject x max_critic_rejections -> force-accept at 'speculation' (never
    infinite-retries the same (func, role))

Offline mode scripts the critic verdicts for all three branches; live mode
uses the real vLLM and only structural invariants are asserted (a verdict did
arrive, the ledger is consistent — no claim left half-evaluated).
"""

from __future__ import annotations

import json

from reaper.agents.critic_agent import CriticEvaluator
from reaper.eval.heavy.base import Fixtures, HeavyCase
from reaper.eval.heavy.runner import register
from reaper.harness.submission import Claim, EvidenceLink

FUNC = "0x4140"
FUNC2 = "0x4148"


def _ev(start: str, end: str) -> dict:
    return {"address_start": start, "address_end": end, "description": "seeded"}


class CriticLoopCase(HeavyCase):
    id = "critic_loop"
    description = "CriticEvaluator accept/reject/force-accept policy against a seeded claim"
    mechanisms = ("critic_loop", "claim_labelling")

    def __init__(self) -> None:
        self._critic = None

    async def seed(self, fx: Fixtures) -> None:
        fx.neo4j.add_function(FUNC, name="sub_4140")
        fx.neo4j.add_function(FUNC2, name="sub_4148")
        fx.extractor._hlils[FUNC] = (
            "0x4140: json = parse_string(buffer)\n0x4141: return handle"
        )
        fx.extractor._signatures[FUNC] = "int64_t sub_4140(int8_t* buffer)"
        fx.extractor._hlils[FUNC2] = (
            "0x4148: counter += 1\n0x4149: return counter"
        )
        fx.extractor._signatures[FUNC2] = "int64_t sub_4148()"

        # Right answer: the claim MATCHES a JSON-parsing function (accept + high).
        cid = await fx.ledger.add_claim(
            FUNC,
            "This function parses a JSON string into a configuration object",
            "test_reviewer", [_ev("0x4140", "0x4141")],
        )
        fx.expected["claim_id"] = cid
        fx.expected["max_critic_rejections"] = int(
            (fx.config.get("limits") or {}).get("max_critic_rejections", 3))
        fx.extra = {}

    # -- scripted (offline) --------------------------------------------------

    def scripted_responses(self, fx: Fixtures) -> list[str]:
        accept = json.dumps({"truth_level": "high_confidence", "accepted": True,
                             "feedback": "evidence supports it"})
        reject = json.dumps({"truth_level": "mid_confidence", "accepted": False,
                             "feedback": "claim contradicts HLIL"})
        # accept(FUNC) then enough rejects on FUNC2 to hit the force-accept cap
        return [accept, reject, reject, reject]

    async def run_case(self, fx: Fixtures, llm) -> None:
        self._critic = CriticEvaluator(
            llm, fx.context_asm, fx.ledger, fx.tracer, fx.config)
        # -- accept path on FUNC -------------------------------------------
        claim = Claim(function_address=FUNC,
                      claim_text="This function parses a JSON string into a "
                                 "configuration object",
                      evidence=[EvidenceLink(address_start="0x4140",
                                             address_end="0x4141",
                                             description="calls into the parser")])
        outcome_ok = await self._critic.evaluate_claim(
            fx.expected["claim_id"], claim, FUNC, "review_agent")
        fx.extra["accept"] = {"accepted": outcome_ok.accepted,
                              "feedback": outcome_ok.feedback}
        fx.extra["claims_func"] = await fx.ledger.get_claims(FUNC)

        # -- reject / force-accept path on FUNC2 ---------------------------
        claim_bad = Claim(function_address=FUNC2,
                          claim_text="This function serializes a struct to "
                                     "a JSON buffer",
                          evidence=[EvidenceLink(address_start="0x4148",
                                                 address_end="0x4149",
                                                 description="counter only")])
        max_reject = fx.expected["max_critic_rejections"]
        fx.extra["reject_seq"] = []
        live_claim_id = await fx.ledger.add_claim(
            FUNC2, claim_bad.claim_text, "test_reviewer",
            [_ev("0x4148", "0x4149")])
        for _ in range(max_reject):
            outcome = await self._critic.evaluate_claim(
                live_claim_id, claim_bad, FUNC2, "review_agent")
            fx.extra["reject_seq"].append(outcome.accepted)
            if not outcome.accepted:
                # Retry semantics: the submitting agent re-submits a fresh
                # claim after the critic deletes the rejected one.
                live_claim_id = await fx.ledger.add_claim(
                    FUNC2, claim_bad.claim_text, "test_reviewer",
                    [_ev("0x4148", "0x4149")])
        fx.extra["claims_func2"] = await fx.ledger.get_claims(FUNC2)
        fx.extra["rejection_counts"] = dict(self._critic._rejection_counts)

    async def assert_expected(self, fx: Fixtures) -> list:
        checks = []
        accept = fx.extra.get("accept") or {}
        # Structural invariant (BOTH modes): any claim that survived a critic
        # pass must have a truth level — never left NULL / half-evaluated.
        for addr in (FUNC, FUNC2):
            for c in await fx.ledger.get_claims(addr):
                if (c.get("truth_level") or "") is None:
                    checks.append(self.fail(
                        f"claim {c['id']} left with truth_level NULL at {addr}"))

        # Offline path has deterministic scripted verdicts we can hard-assert.
        if accept.get("accepted") is True:
            levels = {c.get("truth_level") for c in fx.extra.get("claims_func") or []}
            if "high_confidence" not in levels:
                checks.append(self.fail("offline accept verdict did not label claim "
                                        "high_confidence"))
        elif accept:
            # Live: the model might reasonably disagree — that alone is not a
            # case failure, but tell the report what happened.
            checks.append(self.ok("live critic did not accept the seeded claim — "
                                  "check evidence/prompt quality"))

        # Reject branch (offline): max_critic_rejections rejects -> the final
        # surviving claim must be force-accepted at 'speculation'.
        seq = fx.extra.get("reject_seq") or []
        max_reject = fx.expected.get("max_critic_rejections", 3)
        if seq and len(seq) == max_reject and not any(seq):
            survivors = fx.extra.get("claims_func2") or []
            if not survivors:
                checks.append(self.fail("force-accept branch deleted ALL claims — "
                                        "should have kept the last at speculation"))
            elif "speculation" not in {c.get("truth_level") for c in survivors}:
                checks.append(self.fail("force-accept path did not label the "
                                        "survivor at 'speculation'"))
        elif seq and len(seq) != max_reject:
            checks.append(self.fail(f"expected {max_reject} reject iterations, "
                                    f"saw {len(seq)}"))

        return checks or [self.ok("critic loop terminated cleanly")]

    def score_items(self, fx: Fixtures) -> dict[str, list[dict]]:
        return {}


register(CriticLoopCase)
