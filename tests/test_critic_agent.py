"""Tests for Deliverable 6.1 — CriticEvaluator (reaper/agents/critic_agent.py).

Covers the accept path, the reject path (claim deleted + counted), and the
force-accept after max_rejections path (truth_level='speculation'). Verifies
the CriticVerdict schema is requested and that the payload carries the claim,
function context, and evidence.
"""

from __future__ import annotations

import json

import pytest

from reaper.agents.critic_agent import CriticEvaluator
from reaper.harness.submission import Claim, EvidenceLink

from fake_harness import RecordingTracer, StubLLM


class FakeContext:
    async def for_evidence(self, evidence_links):
        return "EVIDENCE TEXT @" + ";".join(
            e.get("address_start", "?") for e in evidence_links
        )

    async def for_function(self, func_addr):
        return f"SIG: int64_t parse(char*) @ {func_addr}\nHLIL..."


class StubLedger:
    def __init__(self):
        self.levels = []
        self.deleted = []

    async def set_truth_level(self, claim_id, truth_level, reviewed_by):
        self.levels.append((claim_id, truth_level, reviewed_by))

    async def delete_claim(self, claim_id):
        self.deleted.append(claim_id)


def _claim():
    return Claim(
        function_address="0x1000",
        claim_text="the second argument is allowed to be NULL",
        evidence=[EvidenceLink(address_start="0x1010", address_end="0x1020",
                               description="shows the NULL check")],
    )


def _verdict(accepted, truth_level="high_confidence", feedback="ok"):
    return json.dumps({"truth_level": truth_level, "accepted": accepted,
                       "feedback": feedback})


def _critic(ledger, llm):
    config = {"limits": {"max_critic_rejections": 3},
              "thinking_levels": {"critic": "high"}}
    return CriticEvaluator(llm, FakeContext(), ledger, RecordingTracer(), config)


@pytest.mark.asyncio
async def test_accept_path_sets_truth_level():
    ledger = StubLedger()
    llm = StubLLM(responses=[_verdict(True, "mid_confidence")],
                  structured_model_name="CriticVerdict")
    critic = _critic(ledger, llm)

    outcome = await critic.evaluate_claim(7, _claim(), "0x1000", "review_agent")

    assert outcome.accepted is True
    assert ledger.levels == [(7, "mid_confidence", "critic_7")]
    assert not ledger.deleted
    assert critic._rejection_counts == {}

    # payload includes claim + function context + evidence
    send = [c for c in llm.calls if c["op"] == "send"][0]
    assert "NULL" in send["message"]
    assert "EVIDENCE TEXT @0x1010" in send["message"]


@pytest.mark.asyncio
async def test_reject_path_deletes_claim_and_counts():
    ledger = StubLedger()
    llm = StubLLM(responses=[_verdict(False, feedback="unsubstantiated")])
    critic = _critic(ledger, llm)

    outcome = await critic.evaluate_claim(7, _claim(), "0x1000", "review_agent")

    assert outcome.accepted is False
    assert outcome.feedback == "unsubstantiated"
    assert ledger.deleted == [7]
    assert critic._rejection_counts == {("0x1000", "review_agent"): 1}


@pytest.mark.asyncio
async def test_force_accept_after_max_rejections():
    ledger = StubLedger()
    llm = StubLLM(responses=[_verdict(False, feedback="no1"),
                             _verdict(False, feedback="no2"),
                             _verdict(False, feedback="no3")])
    critic = _critic(ledger, llm)

    r1 = await critic.evaluate_claim(10, _claim(), "0x1000", "review_agent")
    r2 = await critic.evaluate_claim(11, _claim(), "0x1000", "review_agent")
    r3 = await critic.evaluate_claim(12, _claim(), "0x1000", "review_agent")

    assert r1.accepted is False and r2.accepted is False
    # third rejection reaches max_critic_rejections → force-accepted
    assert r3.accepted is True
    assert ledger.deleted == [10, 11]
    assert ledger.levels == [(12, "speculation", "critic_12")]
    # counter reset after force-accept
    assert critic._rejection_counts == {}
