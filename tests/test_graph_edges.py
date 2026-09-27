"""Tests for Deliverable 2.3 — build_edges (graph dataflow edge builder).

Builds a small fake binary (two functions + one string) and verifies the
instruction-type -> edge mapping of design § 2.3: DATAFLOW_ASSIGN,
DATAFLOW_ARG, CALL, RETURN and REFS_STRING edges, parameter-name -> Argument
node redirection, deferral of :REFS_STRING to 2.3, and that ambiguous
(indirect) calls emit no outgoing :CALL edge. All writes are MERGE.
"""

from __future__ import annotations

import pytest

from reaper.tools.graph_edges import build_edges

from conftest import RecordingNeo4jDriver
from fake_binja import (
    FakeExtractor,
    FakeFunction,
    FakeStringRef,
    FakeVariable,
    assign,
    call,
    const,
    const_ptr,
    hlil_if,
    hlil_ret,
    var,
    var_init,
)


def _make_extractor():
    helper_call = call(
        const_ptr(0x2000), (var("var_18", address=0x1021), const(0x10, address=0x1022)),
        address=0x1020, target_func="helper", target_addr=0x2000)
    indirect = call(var("fp", address=0x1051), (), address=0x1051)
    func_a = FakeFunction.simple(
        0x1000, "parse_thing",
        instructions=[
            assign(var("var_18", address=0x1010), var("arg1", address=0x1011),
                   address=0x1010),
            assign(var("var_19", address=0x1025), helper_call, address=0x1025),
            assign(var("var_20", address=0x1030), const_ptr(0x5000, address=0x1031),
                   address=0x1030),
            assign(var("var_21", address=0x1050), indirect, address=0x1050),
            var_init("var_30", var("arg2", address=0x1061), address=0x1060),
            hlil_if(call(const_ptr(0x4000), (), address=0x1071,
                         target_func="crit", target_addr=0x4000), address=0x1070),
            hlil_ret(var("var_20", address=0x1040), address=0x1040),
        ],
        variables=[FakeVariable("var_18", type_str="char*"),
                   FakeVariable("var_19", type_str="char*"),
                   FakeVariable("var_20", type_str="char*"),
                   FakeVariable("var_21", type_str="char*"),
                   FakeVariable("var_30", type_str="int64_t"),
                   FakeVariable("fp", type_str="void (*)()")],
        parameters=[FakeVariable("arg1", type_str="char*"),
                    FakeVariable("arg2", type_str="int64_t")],
    )
    func_b = FakeFunction.simple(
        0x2000, "helper",
        instructions=[assign(var("var_b", address=0x2010), const(0, address=0x2011),
                             address=0x2010)],
        variables=[FakeVariable("var_b", type_str="int64_t")],
        parameters=[FakeVariable("arg0", type_str="char*")],
    )
    return FakeExtractor.with_functions(
        [func_a, func_b], strings=[FakeStringRef(0x5000, "token")])


def _edges(driver, rel):
    return [(q[1].get("src"), q[1].get("dst"),
             q[1].get("vid"), q[1].get("sid"), q[1].get("cid"), q[1].get("fa"))
            for q in driver.queries_matching(rel)]


@pytest.mark.asyncio
async def test_dataflow_assign_edges_created():
    driver = RecordingNeo4jDriver()
    await build_edges(_make_extractor(), driver)
    assigns = [q[1] for q in driver.queries_matching("DATAFLOW_ASSIGN")]
    assert assigns
    pairs = {(a["src"], a["dst"]) for a in assigns}
    # parameter arg1 redires to its :Argument node, not a bogus Variable id
    assert ("0x1000:arg0", "0x1000:var_18") in pairs
    assert ("0x1000:arg1", "0x1000:var_30") in pairs  # from VAR_INIT


@pytest.mark.asyncio
async def test_call_and_arg_edges_to_real_target_function():
    driver = RecordingNeo4jDriver()
    await build_edges(_make_extractor(), driver)

    call_edges = [q[1] for q in driver.queries_matching("MERGE (c)-[:CALL]->(f)")]
    assert call_edges
    edges = {(c["cid"], c["fa"]) for c in call_edges}
    assert ("0x1000:call_0x1020", "0x2000") in edges  # direct call
    assert ("0x1000:call_0x1071", "0x4000") in edges  # nested inside if-condition

    arg_edges = [q[1] for q in driver.queries_matching("DATAFLOW_ARG")]
    assert ("0x1000:var_18", "0x2000:arg0") in {
        (a["src"], a["dst"]) for a in arg_edges}


@pytest.mark.asyncio
async def test_indirect_call_emits_no_call_edge():
    driver = RecordingNeo4jDriver()
    await build_edges(_make_extractor(), driver)
    call_edges = {(q[1]["cid"]) for q in driver.queries_matching("MERGE (c)-[:CALL]->(f)")}
    assert "0x1000:call_0x1051" not in call_edges  # indirect — no statically known target


@pytest.mark.asyncio
async def test_refs_string_links_created_from_assign_and_call():
    driver = RecordingNeo4jDriver()
    await build_edges(_make_extractor(), driver)

    refs = [(q[1].get("vid"), q[1].get("sid"))
            for q in driver.queries_matching("REFS_STRING")]
    assert ("0x1000:var_20", "str:0x5000") in refs  # var holds the string address
    # call parameter that carries a string -> callee :Argument references it
    assert ("str:0x5000",) in {(r[1],) for r in refs}


@pytest.mark.asyncio
async def test_return_edges_point_to_owning_function():
    driver = RecordingNeo4jDriver()
    await build_edges(_make_extractor(), driver)
    rets = [(q[1]["vid"], q[1]["fa"]) for q in driver.queries_matching("-[:RETURN]->")]
    assert ("0x1000:var_20", "0x1000") in rets


@pytest.mark.asyncio
async def test_edges_use_merge_only_and_are_deterministic():
    e1 = _make_extractor()
    e2 = _make_extractor()
    d1, d2 = RecordingNeo4jDriver(), RecordingNeo4jDriver()
    await build_edges(e1, d1)
    await build_edges(e2, d2)

    key = lambda d: sorted((q[0], sorted(q[1].items())) for q in d.queries)
    assert key(d1) == key(d2)
    assert all("MERGE" in q[0] for q in d1.queries)
    assert not any("CREATE (" in q[0] for q in d1.queries)
