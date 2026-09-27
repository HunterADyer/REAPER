"""Tests for Deliverable 4.2 — TypeRecoveryAgent.

Uses the recording StubLLM to verify the agent requests the StructDefinition
structured schema, parses the LLM output, records an 'inferred' claim per
involved function, and returns None (without claims) when the model emits an
empty fields list.
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


_STRUCT_JSON = json.dumps({
    "struct_name": "cjson_node",
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
async def test_run_requests_struct_schema_and_records_inferred_claim():
    ledger = FakeLedger()
    ctx = FakeContext(ledger)
    llm = StubLLM(responses=[_STRUCT_JSON], structured_model_name="StructDefinition")
    agent = TypeRecoveryAgent(llm, ctx, {"thinking_levels": {"pass0_type_recovery": "high"}})

    result = await agent.run(_candidate())

    assert result is not None
    assert result.struct_name == "cjson_node"
    assert len(result.fields) == 3
    assert result.fields[0].offset == 0 and result.fields[0].name == "next"

    # one claim per involved function, all at 'inferred'
    assert len(ledger.claims) == 2
    assert {c["function_address"] for c in ledger.claims} == {"0x1000", "0x2000"}
    assert all(c.get("truth_level") == "inferred" for c in ledger.claims)
    # evidence references the observed access sites
    assert ledger.claims[0]["evidence"][0]["address_start"] == "0x1010"
    # one create_session + one send + one destroy for this run
    ops = [c["op"] for c in llm.calls]
    assert ops == ["create_session", "send", "destroy_session"]


@pytest.mark.asyncio
async def test_run_empty_fields_returns_none_without_claims():
    ledger = FakeLedger()
    ctx = FakeContext(ledger)
    llm = StubLLM(responses=[json.dumps({"struct_name": "none", "fields": []})],
                  structured_model_name="StructDefinition")
    agent = TypeRecoveryAgent(llm, ctx, {"thinking_levels": {}})

    result = await agent.run(_candidate())

    assert result is None
    assert ledger.claims == []


@pytest.mark.asyncio
async def test_run_without_ledger_still_returns_struct():
    llm = StubLLM(responses=[_STRUCT_JSON])
    agent = TypeRecoveryAgent(llm, FakeContext(ledger=None), {"thinking_levels": {}})
    result = await agent.run(_candidate())
    assert result is not None and result.struct_name == "cjson_node"


def _sent_level(llm) -> str:
    return next(c for c in reversed(llm.calls) if c["op"] == "send")["thinking_level"]


@pytest.mark.asyncio
async def test_run_follow_up_selects_max_budget_pass0_high():
    """Regression guard (audit 2026-09-27): pass-0 recovery requests
    ``pass0_type_recovery`` ("high"); any FOLLOW-UP recovery round requests
    ``recovery_followup`` ("max" = xhigh). Both defaults and explicit config
    must route to the correct thinking levels."""
    llm0 = StubLLM(responses=[_STRUCT_JSON], structured_model_name="StructDefinition")
    await TypeRecoveryAgent(llm0, FakeContext(ledger=None),
                            {"thinking_levels": {"pass0_type_recovery": "high"}}
                            ).run(_candidate())
    assert _sent_level(llm0) == "high"

    llm1 = StubLLM(responses=[_STRUCT_JSON], structured_model_name="StructDefinition")
    await TypeRecoveryAgent(llm1, FakeContext(ledger=None),
                            {"thinking_levels": {"recovery_followup": "max"}}
                            ).run(_candidate(), follow_up=True)
    assert _sent_level(llm1) == "max"

    # defaults when the config omits the keys: pass 0 -> "high", follow-up -> "max"
    llm2 = StubLLM(responses=[_STRUCT_JSON])
    await TypeRecoveryAgent(llm2, FakeContext(ledger=None),
                            {"thinking_levels": {}}).run(_candidate())
    assert _sent_level(llm2) == "high"

    llm3 = StubLLM(responses=[_STRUCT_JSON])
    await TypeRecoveryAgent(llm3, FakeContext(ledger=None),
                            {"thinking_levels": {}}).run(_candidate(), follow_up=True)
    assert _sent_level(llm3) == "max"
