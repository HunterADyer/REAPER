"""Tests for Deliverable 2.2 — build_nodes (graph node builder).

Uses FakeExtractor/FakeFunction from tests/fake_binja.py and the additive
RecordingNeo4jDriver from conftest. Verifies that Function/Variable/Argument/
Call/StringRef nodes are MERGEd (never CREATE) with stable ids, that every
node carries an address property, and that Variable/Argument/Call nodes get a
:CONTAINS edge from their parent Function.
"""

from __future__ import annotations

import pytest

from reaper.tools.graph_nodes import build_nodes

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
    var,
    var_init,
)


def _make_extractor():
    indirect = call(var("fp", address=0x1031), (), address=0x1031)
    func = FakeFunction.simple(
        0x1000, "sub_1000",
        instructions=[
            var_init("var_18",
                     call(const_ptr(0x4000), (const(0x100),), address=0x1011,
                          target_func="malloc", target_addr=0x4000),
                     address=0x1010, type_str="char*"),
            assign(var("var_20", address=0x1020), const_ptr(0x5000, address=0x1021),
                   address=0x1020),
            assign(var("var_21", address=0x1030), indirect, address=0x1030),
            hlil_if(call(const_ptr(0x4100), (), address=0x1051,
                         target_func="helper", target_addr=0x4100), address=0x1050),
        ],
        variables=[FakeVariable("var_18", type_str="char*"),
                   FakeVariable("var_20", type_str="char*"),
                   FakeVariable("var_21", type_str="void*"),
                   FakeVariable("fp", type_str="void (*)()")],
        parameters=[FakeVariable("arg1", type_str="int64_t"),
                    FakeVariable("arg2", type_str="char*")],
    )
    return FakeExtractor.with_functions(
        [func], strings=[FakeStringRef(0x5000, "hello\n")])


def _params(driver, fragment, key):
    out = []
    for _q, p in driver.queries_matching(fragment):
        if key in p:
            out.append(p[key])
    return out


@pytest.mark.asyncio
async def test_build_nodes_merges_function_nodes():
    driver = RecordingNeo4jDriver()
    await build_nodes(_make_extractor(), driver)

    func_addrs = _params(driver, "MERGE (f:Function {address: $address})", "address")
    assert func_addrs == ["0x1000"]
    fq = driver.queries_matching("MERGE (f:Function {address: $address})")[0]
    assert fq[1]["name"] == "sub_1000"
    assert fq[1]["imported"] is False
    # pinned/ambiguous default to false (pin_symbols may flip them later).
    assert "SET f.pinned = false, f.ambiguous = false" in fq[0]


@pytest.mark.asyncio
async def test_build_nodes_merges_variables_with_stable_ids():
    driver = RecordingNeo4jDriver()
    await build_nodes(_make_extractor(), driver)

    ids = set(_params(driver, "MERGE (v:Variable {id: $id})", "id"))
    assert ids == {
        "0x1000:var_18", "0x1000:var_20", "0x1000:var_21", "0x1000:fp",
    }
    # id embeds the function address + ORIGINAL var name (stable on rename).
    assert all(v.startswith("0x1000:") for v in ids)
    # every variable node carries an address property
    addrs = _params(driver, "MERGE (v:Variable {id: $id})", "address")
    assert addrs and all(a == "0x1000" for a in addrs)


@pytest.mark.asyncio
async def test_build_nodes_creates_contains_edges_for_owned_nodes():
    driver = RecordingNeo4jDriver()
    await build_nodes(_make_extractor(), driver)

    var_contains = driver.queries_matching("MERGE (f)-[:CONTAINS]->(v)")
    assert var_contains
    # The MATCH clause pins the target label to :Variable.
    assert all("v:Variable {id: $vid}" in q[0] for q in var_contains)
    assert {q[1]["vid"] for q in var_contains} == {
        "0x1000:var_18", "0x1000:var_20", "0x1000:var_21", "0x1000:fp",
    }
    arg_contains = driver.queries_matching("MERGE (f)-[:CONTAINS]->(a)")
    assert all("a:Argument {id: $aid}" in q[0] for q in arg_contains)
    assert {q[1]["aid"] for q in arg_contains} == {"0x1000:arg0", "0x1000:arg1"}
    call_contains = driver.queries_matching("MERGE (f)-[:CONTAINS]->(c)")
    assert all("c:Call {id: $cid}" in q[0] for q in call_contains)
    assert len(call_contains) == 3  # 2 direct + 1 indirect call found by walk


@pytest.mark.asyncio
async def test_build_nodes_merges_arguments_and_string_refs():
    driver = RecordingNeo4jDriver()
    await build_nodes(_make_extractor(), driver)

    arg_ids = _params(driver, "MERGE (a:Argument {id: $id})", "id")
    assert arg_ids == ["0x1000:arg0", "0x1000:arg1"]

    str_ids = _params(driver, "MERGE (s:StringRef {id: $id})", "id")
    assert str_ids == ["str:0x5000"]
    sq = driver.queries_matching("MERGE (s:StringRef {id: $id})")[0]
    assert sq[1]["value"] == "hello\n"


@pytest.mark.asyncio
async def test_build_nodes_flags_ambiguous_calls():
    driver = RecordingNeo4jDriver()
    await build_nodes(_make_extractor(), driver)

    calls = driver.queries_matching("MERGE (c:Call {id: $id})")
    by_id = {q[1]["id"]: q[1]["ambiguous"] for q in calls}
    assert by_id == {
        "0x1000:call_0x1011": False,  # direct call to malloc @ 0x4000
        "0x1000:call_0x1031": True,   # indirect call through function pointer
        "0x1000:call_0x1051": False,  # direct call hidden inside an if-condition
    }


@pytest.mark.asyncio
async def test_build_nodes_runs_idempotently_with_merge_only():
    extractor = _make_extractor()
    driver1 = RecordingNeo4jDriver()
    driver2 = RecordingNeo4jDriver()
    await build_nodes(extractor, driver1)
    await build_nodes(extractor, driver2)

    mk = lambda d: sorted((q[0], sorted(q[1].items())) for q in d.queries)
    assert mk(driver1) == mk(driver2)  # deterministic
    # MERGE only — never CREATE (idempotency by design).
    assert all("MERGE" in q[0] for q in driver1.queries)
    assert not any("CREATE (" in q[0] for q in driver1.queries)
