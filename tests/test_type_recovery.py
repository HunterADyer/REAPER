"""Tests for Deliverable 4.2 — TypeRecoveryAgent verdict contract.

Covers the redesign: the agent returns a TypeVerdict (accepted + name + kind +
complete field union) instead of a bare StructDefinition. Acceptance GATES the
apply/rename; rejected candidates never apply and are logged to the
type-rejections ledger for hand-tuning.

Uses the recording StubLLM to verify the TypeVerdict structured schema,
parsing, the 'inferred' claim + proposed name on accept, the no-struct case
(accepted with empty fields), the reject path (no claim, rejection recorded),
and thinking-budget routing (pass0 high / followup max).
"""

from __future__ import annotations

import json

import pytest

from reaper.agents.type_recovery import TypeRecoveryAgent
from reaper.tools.struct_detector import FieldAccess, StructCandidate

from fake_harness import StubLLM


class FakeLedger:
    def __init__(self):
        self.claims = []
        self.rejections = []
        self._next = 1

    async def add_claim(self, function_address, claim_text, submitted_by, evidence):
        claim_id = self._next
        self._next += 1
        self.claims.append({
            "id": claim_id, "function_address": function_address,
            "claim_text": claim_text, "submitted_by": submitted_by,
            "evidence": evidence,
        })
        return claim_id

    async def set_truth_level(self, claim_id, truth_level, reviewed_by):
        claim = next(c for c in self.claims if c["id"] == claim_id)
        claim["truth_level"] = truth_level

    async def record_type_rejection(self, rejection: dict) -> int:
        self.rejections.append(rejection)
        return len(self.rejections)


class FakeContext:
    def __init__(self, ledger=None):
        self.ledger = ledger or FakeLedger()
        self.requests = []

    async def for_struct_candidate(self, candidate):
        self.requests.append(candidate.candidate_id)
        return f"STRUCT CONTEXT for {candidate.candidate_id}"


def _candidate():
    return StructCandidate(
        candidate_id="struct_cand_7",
        base_type_hint="struct cJSON*",
        accesses=[
            FieldAccess(offset=0, size=8, access_type="read",
                        function_address="0x1000", instruction_address="0x1010"),
            FieldAccess(offset=8, size=4, access_type="read",
                        function_address="0x2000", instruction_address="0x2050"),
            FieldAccess(offset=16, size=8, access_type="write",
                        function_address="0x2000", instruction_address="0x2060"),
        ],
        functions_involved=["0x1000", "0x2000"],
    )


_ACCEPT_JSON = json.dumps({
    "accepted": True,
    "rejection_reason": "",
    "struct_name": "cjson_node",
    "kind": "struct",
    "fields": [
        {"offset": 0, "name": "next", "type_str": "struct cjson_node*",
         "size": 8, "confidence": "high_confidence"},
        {"offset": 8, "name": "obj_type", "type_str": "int32_t",
         "size": 4, "confidence": "mid_confidence"},
        {"offset": 16, "name": "child", "type_str": "struct cjson_node*",
         "size": 8, "confidence": "high_confidence"},
    ],
})


@pytest.mark.asyncio
async def test_run_accepts_verdict_and_records_claim_with_proposed_name():
    ledger = FakeLedger()
    ctx = FakeContext(ledger)
    llm = StubLLM(responses=[_ACCEPT_JSON], structured_model_name="TypeVerdict")
    agent = TypeRecoveryAgent(llm, ctx, {"thinking_levels": {"pass0_type_recovery": "high"}})

    verdict = await agent.run(_candidate())

    assert verdict.accepted is True
    assert verdict.struct_name == "cjson_node"
    assert len(verdict.fields) == 3
    assert verdict.fields[0].offset == 0 and verdict.fields[0].name == "next"

    # the verdict carries the advisory StructDefinition; run() recorded claims
    sd = agent.to_struct_def(verdict)
    assert sd is not None and sd.struct_name == "cjson_node"
    assert len(ledger.claims) == 2
    assert {c["function_address"] for c in ledger.claims} == {"0x1000", "0x2000"}
    assert all(c.get("truth_level") == "inferred" for c in ledger.claims)
    assert ledger.claims[0]["evidence"][0]["address_start"] == "0x1010"
    assert "cjson_node" in ledger.claims[0]["claim_text"]
    # no rejection recorded on the accept path
    assert ledger.rejections == []

    # TypeVerdict structured schema requested + clean lifecycle
    ops = [c["op"] for c in llm.calls]
    assert ops == ["create_session", "send", "destroy_session"]


@pytest.mark.asyncio
async def test_run_rejects_verdict_no_apply_no_claim_rejection_logged():
    ledger = FakeLedger()
    ctx = FakeContext(ledger)
    reject_json = json.dumps({
        "accepted": False,
        "rejection_reason": "offsets do not cohere; unrelated structs",
        "struct_name": "",
        "kind": "struct",
        "fields": [],
    })
    llm = StubLLM(responses=[reject_json], structured_model_name="TypeVerdict")
    agent = TypeRecoveryAgent(llm, ctx, {"thinking_levels": {}})

    verdict = await agent.run(_candidate())

    assert verdict.accepted is False
    assert "unrelated" in verdict.rejection_reason
    # nothing to apply, no claims
    assert agent.to_struct_def(verdict) is None
    assert ledger.claims == []
    # rejection recorded for hand-tuning
    assert len(ledger.rejections) == 1
    assert ledger.rejections[0]["candidate_id"] == "struct_cand_7"
    assert ledger.rejections[0]["functions_involved"] == ["0x1000", "0x2000"]


@pytest.mark.asyncio
async def test_run_accepted_no_fields_is_no_struct_no_claim():
    ledger = FakeLedger()
    ctx = FakeContext(ledger)
    llm = StubLLM(responses=[json.dumps({
        "accepted": True, "struct_name": "sparse", "kind": "struct",
        "fields": [],
    })], structured_model_name="TypeVerdict")
    agent = TypeRecoveryAgent(llm, ctx, {"thinking_levels": {}})

    verdict = await agent.run(_candidate())

    assert verdict.accepted is True
    assert verdict.fields == []
    assert agent.to_struct_def(verdict) is None
    assert ledger.claims == []
    assert ledger.rejections == []


@pytest.mark.asyncio
async def test_run_without_ledger_still_returns_verdict():
    llm = StubLLM(responses=[_ACCEPT_JSON])
    agent = TypeRecoveryAgent(llm, FakeContext(ledger=None), {"thinking_levels": {}})
    verdict = await agent.run(_candidate())
    assert verdict.accepted is True
    assert agent.to_struct_def(verdict) is not None


def _sent_level(llm) -> str:
    return next(c for c in reversed(llm.calls) if c["op"] == "send")["thinking_level"]


@pytest.mark.asyncio
async def test_run_follow_up_selects_max_budget_pass0_high():
    """Regression guard (audit 2026-09-27): pass-0 recovery requests
    ``pass0_type_recovery`` ("high"); any FOLLOW-UP recovery round requests
    ``recovery_followup`` ("max" = xhigh). Both defaults and explicit config
    must route to the correct thinking levels."""
    llm0 = StubLLM(responses=[_ACCEPT_JSON], structured_model_name="TypeVerdict")
    await TypeRecoveryAgent(llm0, FakeContext(ledger=None),
                            {"thinking_levels": {"pass0_type_recovery": "high"}}
                            ).run(_candidate())
    assert _sent_level(llm0) == "high"

    llm1 = StubLLM(responses=[_ACCEPT_JSON], structured_model_name="TypeVerdict")
    await TypeRecoveryAgent(llm1, FakeContext(ledger=None),
                            {"thinking_levels": {"recovery_followup": "max"}}
                            ).run(_candidate(), follow_up=True)
    assert _sent_level(llm1) == "max"

    # defaults when the config omits the keys: pass 0 -> "high", follow-up -> "max"
    llm2 = StubLLM(responses=[_ACCEPT_JSON])
    await TypeRecoveryAgent(llm2, FakeContext(ledger=None),
                            {"thinking_levels": {}}).run(_candidate())
    assert _sent_level(llm2) == "high"

    llm3 = StubLLM(responses=[_ACCEPT_JSON])
    await TypeRecoveryAgent(llm3, FakeContext(ledger=None),
                            {"thinking_levels": {}}).run(_candidate(), follow_up=True)
    assert _sent_level(llm3) == "max"
