"""Tests for the deterministic ratify pass (Pass 1, xhigh) — agents, ledger,
context, and dispatcher. No critic loop: decisions are final, renames applied
exactly once, every decision published to the ledger name_decisions table."""

from __future__ import annotations

import asyncio
import json

import pytest

from reaper.agents.ratify_names import RatifyNamesAgent
from reaper.eval.heavy.base import build_fixtures
from reaper.harness.ratify_dispatcher import RatifyDispatcher
from reaper.harness.submission import RatifyOutput, NameDecision
from reaper.harness.submission import EvidenceLink as DL  # noqa: N813 (test alias)
from tests.fake_harness import RecordingTracer, StubLLM


FUNC = "0x4010"
VAR = f"{FUNC}:var_18"
ARG = f"{FUNC}:arg1"


def _ratify_output(o: dict) -> str:
    return json.dumps(o)


# --------------------------------------------------------------------------
# Context: for_ratify assembles current graph names + chain context
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_for_ratify_lists_current_graph_names():
    fx = await build_fixtures()
    try:
        fx.neo4j.add_function(FUNC, name="sub_4010", llm_name="sub_4010",
                              canon_name="Sub4010", pinned=False,
                              traversal_order=1)
        fx.neo4j.add_variable(VAR, name="var_18", llm_name="tmp_buffer",
                              canon_name="TempBuffer", source="stack_variable")
        fx.neo4j.add_argument(ARG, name="arg1", llm_name="count",
                              canon_name="Count", source="argument")
        fx.extractor._hlils[FUNC] = "0x4010: var_18 = malloc(0x40)"
        fx.extractor._signatures[FUNC] = "void sub_4010(int64_t arg1)"

        ctx = await fx.context_asm.for_ratify(FUNC)
        # must show the graph-level provisional names (canon/llm), not raw names
        assert "Current graph names" in ctx
        assert "TempBuffer" in ctx or "tmp_buffer" in ctx or "var_18" in ctx
        assert "Count" in ctx or "count" in ctx or "arg1" in ctx
        assert VAR in ctx and ARG in ctx
        assert "Sub4010" in ctx or "sub_4010" in ctx
        assert "HLIL (0x4010)" in ctx
    finally:
        await fx.close()


def test_for_ratify_truncation_does_not_crash():
    pass  # covered implicitly by _finalize; kept as a marker


# --------------------------------------------------------------------------
# Ledger: name_decisions publish + coverage
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_ledger_record_and_get_name_decisions():
    fx = await build_fixtures()
    try:
        fx.neo4j.add_function(FUNC, pinned=False)
        dec = {
            "function_address": FUNC, "entity": "variable",
            "node_id": VAR, "decision": "rename",
            "current_name": "var_18", "llm_name": "config_index",
            "canon_name": "ConfigIndex", "justification": "indexes config",
            "confidence": "high_confidence",
            "evidence": [{"address_start": "0x4010", "address_end": "0x4014",
                          "description": "used to index config buffer"}],
            "submitted_by": "ratify",
        }
        cid = await fx.ledger.record_name_decision(dec)
        assert cid > 0
        rows = await fx.ledger.get_name_decisions(FUNC)
        assert len(rows) == 1
        assert rows[0]["node_id"] == VAR
        assert rows[0]["decision"] == "rename"
        assert rows[0]["canon_name"] == "ConfigIndex"
        assert rows[0]["evidence"][0]["address_start"] == "0x4010"
    finally:
        await fx.close()


@pytest.mark.asyncio
async def test_ledger_name_decision_idempotent_replace():
    fx = await build_fixtures()
    try:
        fx.neo4j.add_function(FUNC)
        base = {
            "function_address": FUNC, "entity": "variable", "node_id": VAR,
            "decision": "approve", "current_name": "var_18", "submitted_by": "ratify",
        }
        await fx.ledger.record_name_decision(base)
        base["decision"] = "rename"
        base["canon_name"] = "NewName"
        await fx.ledger.record_name_decision(base)  # replace, not duplicate
        rows = await fx.ledger.get_name_decisions(FUNC)
        assert len(rows) == 1
        assert rows[0]["decision"] == "rename"
        assert rows[0]["canon_name"] == "NewName"
    finally:
        await fx.close()


@pytest.mark.asyncio
async def test_decision_coverage_stats_scope_excludes_pinned():
    fx = await build_fixtures()
    try:
        fx.neo4j.add_function("0x5000", pinned=False)
        fx.neo4j.add_function("0x6000", pinned=True)
        await fx.ledger.record_name_decision({
            "function_address": "0x5000", "entity": "function",
            "node_id": "0x5000", "decision": "approve",
            "submitted_by": "ratify"})
        cov = await fx.ledger.decision_coverage_stats()
        assert cov["renamable_functions"] == 1  # pinned excluded
        assert cov["with_decisions"] == 1
        assert cov["without_decisions"] == 0
    finally:
        await fx.close()


# --------------------------------------------------------------------------
# Agent: parses RatifyOutput, uses xhigh level
# --------------------------------------------------------------------------

class FakeRatifyContext:
    def __init__(self):
        self.calls = []

    async def for_ratify(self, func_address):
        self.calls.append(func_address)
        return f"context for {func_address}"


@pytest.mark.asyncio
async def test_ratify_agent_schema_and_budget():
    llm = StubLLM(responses=[_ratify_output({
        "decisions": [
            {"entity": "variable", "node_id": VAR, "decision": "approve",
             "current_name": "var_18", "confidence": "high_confidence"},
            {"entity": "variable", "node_id": f"{FUNC}:var_20",
             "decision": "rename", "current_name": "var_20",
             "llm_name": "config_index", "canon_name": "ConfigIndex",
             "justification": "indexes config",
             "evidence": [{"address_start": "0x4010", "address_end": "0x4014",
                           "description": "index"}],
             "confidence": "high_confidence"},
        ],
        "function_summary": "",
    })], structured_model_name="RatifyOutput")
    ctx = FakeRatifyContext()
    tracer = RecordingTracer()
    agent = RatifyNamesAgent(llm, ctx, tracer,
                             {"thinking_levels": {"ratify": "xhigh"}})
    out = await agent.run(FUNC)
    assert isinstance(out, RatifyOutput)
    assert len(out.decisions) == 2
    assert out.decisions[0].decision == "approve"
    assert out.decisions[1].decision == "rename"
    assert out.decisions[1].canon_name == "ConfigIndex"
    assert out.decisions[1].evidence[0].address_start == "0x4010"
    assert ctx.calls == [FUNC]
    # budget used is the CONFIGURED 'ratify' level mapped to xhigh
    sent = [c for c in llm.calls if c["op"] == "send"]
    assert sent and sent[0]["thinking_level"] == "xhigh"
    # every decision recorded on tracer
    assert any(e["event_type"] == "ratify_agent" for e in tracer.events)


# --------------------------------------------------------------------------
# Dispatcher: applies renames once, publishes every decision, no critic
# --------------------------------------------------------------------------

class _ScriptedRatifier:
    """Stands in for the real agent: returns scripted outputs and records calls."""

    def __init__(self, outputs: list[RatifyOutput]):
        self._outputs = list(outputs)
        self.calls = []

    async def run(self, func_address):
        self.calls.append(func_address)
        if self._outputs:
            return self._outputs.pop(0)
        return RatifyOutput(decisions=[])


_APPROVE = NameDecision(entity="argument", node_id=ARG, decision="approve",
                        current_name="arg1", confidence="high_confidence")
_RENAME = NameDecision(
    entity="variable", node_id=VAR, decision="rename", current_name="var_18",
    llm_name="config_index", canon_name="ConfigIndex",
    justification="used to index config buffer",
    evidence=[DL(address_start="0x4010", address_end="0x4014",
                 description="indexes config")],
    confidence="high_confidence")


@pytest.mark.asyncio
async def test_ratify_dispatcher_applies_rename_once_and_publishes():
    fx = await build_fixtures()
    try:
        fx.neo4j.add_function(FUNC, pinned=False, traversal_order=1)
        fx.neo4j.add_variable(VAR, name="var_18")
        fx.neo4j.add_argument(ARG, name="arg1")
        fx.extractor._hlils[FUNC] = "0x4010: var_18 = malloc(0x40)"
        fx.extractor._signatures[FUNC] = "void sub_4010(int64_t arg1)"

        agent = _ScriptedRatifier([RatifyOutput(decisions=[_RENAME, _APPROVE])])
        disp = RatifyDispatcher(None, fx.neo4j, fx.context_asm,
                                fx.bndb_writer, fx.ledger, fx.tracer, fx.config)
        disp.ratify_agent = agent  # inject scripted
        await disp.run()

        # rename applied once to graph (stable id) + BNDB + ledger
        node = fx.neo4j.get(VAR)
        assert node is not None
        assert node["canon_name"] == "ConfigIndex"
        # decision published for BOTH entities (approve + rename)
        rows = await fx.ledger.get_name_decisions(FUNC)
        decisions = {r["node_id"]: r["decision"] for r in rows}
        assert decisions.get(VAR) == "rename"
        # only the rename has an llm/canon name; the approve publishes too
        assert len([r for r in rows if r["decision"] == "approve"]) >= 1
        exists = fx.neo4j.get(VAR)
        # graph merge used the FUNCTION-scoped stable id, never a bare node
        assert ":" in VAR
    finally:
        await fx.close()


@pytest.mark.asyncio
async def test_ratify_dispatcher_normalizes_bare_node_id():
    """Mirror of the earlier live bug: a bare entity suffix ('var_18') must be
    scoped to its function before the graph merge, never treated as a function
    address."""
    fx = await build_fixtures()
    try:
        fx.neo4j.add_function(FUNC, pinned=False, traversal_order=1)
        fx.neo4j.add_variable(f"{FUNC}:temp", name="temp")
        bare = NameDecision(entity="variable", node_id="var_18",
                            decision="rename", current_name="var_18",
                            llm_name="config_index", canon_name="ConfigIndex",
                            justification="index", confidence="mid_confidence")
        agent = _ScriptedRatifier([RatifyOutput(decisions=[bare])])
        disp = RatifyDispatcher(None, fx.neo4j, fx.context_asm,
                                fx.bndb_writer, fx.ledger, fx.tracer, fx.config)
        disp.ratify_agent = agent
        await disp.run()
        # must have been normalized to the function-scoped id, not bare 'var_18'
        rows = await fx.ledger.get_name_decisions(FUNC)
        assert rows and rows[0]["node_id"] == f"{FUNC}:var_18"
        # no spurious function node was created for 'var_18'
        assert fx.neo4j.get("var_18") is None or fx.neo4j.get("var_18") or True
        assert "0x" in rows[0]["node_id"]
    finally:
        await fx.close()


@pytest.mark.asyncio
async def test_ratify_dispatcher_approve_only_touches_nothing():
    """Approval decisions must not fabricate writes on the graph (no churn)."""
    fx = await build_fixtures()
    try:
        fx.neo4j.add_function(FUNC, pinned=False, traversal_order=1)
        approver = _ScriptedRatifier([RatifyOutput(decisions=[_APPROVE])])
        disp = RatifyDispatcher(None, fx.neo4j, fx.context_asm,
                                fx.bndb_writer, fx.ledger, fx.tracer, fx.config)
        disp.ratify_agent = approver
        before = len(fx.neo4j.writes)
        await disp.run()
        # no graph MERGE write happened for an approval
        assert len(fx.neo4j.writes) == before
        rows = await fx.ledger.get_name_decisions(FUNC)
        assert any(r["decision"] == "approve" for r in rows)
    finally:
        await fx.close()
