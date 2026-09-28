"""Tests for Deliverable 2.5/7.4 — graph_analysis (validate_and_order,
compute_resynthesis_groups).

Validates the three validate_and_order steps against a canned graph served by
the RecordingNeo4jDriver responder: leaf validation (raises on bad leaves,
marks isolated functions), Tarjan SCC detection with :DEFERRED_BACK
relabelling, and bottom-up traversal order with NO nulls. Also covers the
7.4 export compute_resynthesis_groups and union-find dedup.
"""

from __future__ import annotations

import pytest

from reaper.tools.graph_analysis import (
    _TarjanSCC,
    compute_resynthesis_groups,
    validate_and_order,
)

from conftest import RecordingNeo4jDriver


def analysis_responder(*, leaves=(), functions=(), call_edges=(), contains=()):
    """Build a responder serving canned reads for graph_analysis queries."""

    def respond(query, params):
        if "DEFERRED_BACK" in query:
            return []
        if "RETURN owner.address AS src, target.address AS dst" in query:
            return [{"src": s, "dst": d} for s, d in call_edges]
        if "MATCH (n)" in query and "labels(n) AS labels" in query:
            return list(leaves)
        if "RETURN f.address AS address" in query:
            return [{"address": a, "pinned": p} for a, p in functions]
        if "f:Function)-[:CONTAINS]->(n)" in query:
            return [{"owner": o, "labels": list(ls)} for o, ls in contains]
        return []

    return respond


def _set_params(driver, fragment, key):
    return [q[1][key] for q in driver.queries_matching(fragment) if key in q[1]]


# A clean, acyclic graph: main(0x1000) -> worker(0x2000) -> malloc(0x3000,pinned)
_CLEAN_LEAVES = [
    {"labels": ["Variable"], "address": "0x1000", "ambiguous": False,
     "pinned": False, "id": "0x1000:var_18"},
    {"labels": ["Argument"], "address": "0x1000", "ambiguous": False,
     "pinned": False, "id": "0x1000:arg0"},
    {"labels": ["StringRef"], "address": "0x5000", "ambiguous": False,
     "pinned": False, "id": "str:0x5000"},
    {"labels": ["Function"], "address": "0x1000", "ambiguous": False,
     "pinned": False, "id": "0x1000"},  # main: entry point, no caller
]
_CLEAN_FUNCTIONS = [("0x1000", False), ("0x2000", False), ("0x3000", True)]
_CLEAN_CALL_EDGES = [("0x1000", "0x2000"), ("0x2000", "0x3000")]
_CLEAN_CONTAINS = [
    ("0x1000", ["Variable", "Call"]),
    ("0x2000", ["Variable", "Call"]),
]


@pytest.mark.asyncio
async def test_leaf_validation_passes_and_entry_function_allowed():
    driver = RecordingNeo4jDriver(analysis_responder(
        leaves=_CLEAN_LEAVES, functions=_CLEAN_FUNCTIONS,
        call_edges=_CLEAN_CALL_EDGES, contains=_CLEAN_CONTAINS))
    await validate_and_order(driver)  # must not raise


@pytest.mark.asyncio
async def test_resolved_call_leaf_allowed():
    """A resolved (ambiguous=false) Call leaf is legitimate: Call nodes carry
    outgoing :CALL + incoming :CONTAINS but never an incoming dataflow edge,
    so they are always leaves and must not fail validation."""
    call_leaf = [{"labels": ["Call"], "address": "0x1010", "ambiguous": False,
                  "pinned": False, "id": "0x1000:call_0x1010"}]
    driver = RecordingNeo4jDriver(analysis_responder(
        leaves=[*_CLEAN_LEAVES, *call_leaf], functions=_CLEAN_FUNCTIONS,
        call_edges=_CLEAN_CALL_EDGES, contains=_CLEAN_CONTAINS))
    await validate_and_order(driver)  # must not raise


@pytest.mark.asyncio
async def test_ambiguous_call_leaf_allowed():
    amb = [{"labels": ["Call"], "address": "0x1010", "ambiguous": True,
            "pinned": False, "id": "0x1000:call_0x1010"}]
    driver = RecordingNeo4jDriver(analysis_responder(
        leaves=[*_CLEAN_LEAVES, *amb], functions=_CLEAN_FUNCTIONS,
        call_edges=_CLEAN_CALL_EDGES, contains=_CLEAN_CONTAINS))
    await validate_and_order(driver)  # ambiguous calls legitimately lack CALL edges


@pytest.mark.asyncio
async def test_isolated_function_marked_not_failed():
    leaves = _CLEAN_LEAVES + [
        {"labels": ["Function"], "address": "0x4000", "ambiguous": False,
         "pinned": False, "id": "0x4000"}]
    functions = [*_CLEAN_FUNCTIONS, ("0x4000", False)]
    driver = RecordingNeo4jDriver(analysis_responder(
        leaves=leaves, functions=functions,
        call_edges=_CLEAN_CALL_EDGES, contains=_CLEAN_CONTAINS))
    await validate_and_order(driver)  # dead/empty function -> isolated, no raise
    iso = driver.queries_matching("SET f.isolated = true")
    assert [q[1]["a"] for q in iso] == ["0x4000"]


@pytest.mark.asyncio
async def test_traversal_order_assigned_to_every_function():
    driver = RecordingNeo4jDriver(analysis_responder(
        leaves=_CLEAN_LEAVES, functions=_CLEAN_FUNCTIONS,
        call_edges=_CLEAN_CALL_EDGES, contains=_CLEAN_CONTAINS))
    await validate_and_order(driver)

    ordered = {q[1]["a"]: q[1]["order"] for q in driver.queries
               if "f.traversal_order" in q[0]}
    # Every Function node gets an order — mirrors "no NULL traversal_order".
    assert set(ordered) == {"0x1000", "0x2000", "0x3000"}
    # main -> worker -> (pinned malloc): worker is a leaf (calls only a pinned
    # function), so worker=0, main=1; pinned functions are always order 0.
    assert ordered["0x1000"] == 1
    assert ordered["0x2000"] == 0
    assert ordered["0x3000"] == 0


# Design § 2.5 test scenario: void a() { b(); } void b() { a(); }
_CYCLE_LEAVES = [
    {"labels": ["Variable"], "address": "0x1000", "ambiguous": False,
     "pinned": False, "id": "0x1000:var_a"},
    {"labels": ["Argument"], "address": "0x2000", "ambiguous": False,
     "pinned": False, "id": "0x2000:arg_b"},
]
_CYCLE_FUNCTIONS = [("0x1000", False), ("0x2000", False)]
_CYCLE_CALL_EDGES = [("0x1000", "0x2000"), ("0x2000", "0x1000")]
_CYCLE_CONTAINS = [("0x1000", ["Call"]), ("0x2000", ["Call"])]


@pytest.mark.asyncio
async def test_scc_detected_and_backedge_relabelled():
    driver = RecordingNeo4jDriver(analysis_responder(
        leaves=_CYCLE_LEAVES, functions=_CYCLE_FUNCTIONS,
        call_edges=_CYCLE_CALL_EDGES, contains=_CYCLE_CONTAINS))
    await validate_and_order(driver)

    # Both members get the SAME scc_id.
    scc_ids = {q[1]["a"]: q[1]["id"] for q in driver.queries
               if "f.scc_id" in q[0]}
    assert set(scc_ids) == {"0x1000", "0x2000"}
    assert scc_ids["0x1000"] == scc_ids["0x2000"] == "scc:0x1000"

    # Exactly one back-edge is relabelled as :DEFERRED_BACK (highest->lowest).
    deferred = driver.queries_matching("DEFERRED_BACK")
    assert len(deferred) == 1
    assert deferred[0][1] == {"src": "0x2000", "dst": "0x1000"}


def test_tarjan_scc_finds_cycle():
    adj = {"0x1000": ["0x2000"], "0x2000": ["0x1000"], "0x3000": []}
    comps = _TarjanSCC(adj).run()
    scc = {tuple(sorted(c)) for c in comps}
    assert ("0x1000", "0x2000") in scc
    assert ("0x3000",) in scc


@pytest.mark.asyncio
async def test_compute_resynthesis_groups_dedupes_overlapping_scopes():
    def respond(query, params):
        if "f.scc_id IS NOT NULL" in query:
            return [{"sid": "scc:0x1000", "addrs": ["0x1000", "0x2000"]}]
        # NOTE: the real Cypher is "[:FIELD_OF]->(s)" — the ']' between
        # FIELD_OF and -> is mandatory Cypher syntax. The fragment must include
        # it or this responder never serves the struct-consumer group.
        if "FIELD_OF]->(s)" in query:
            return [{"sid": "struct:cjson", "addrs": ["0x2000", "0x3000"]}]
        if "RETURN caller.address AS src" in query:
            return [{"src": "0x1000", "dsts": ["0x4000"]}]
        return []

    driver = RecordingNeo4jDriver(respond)
    groups = await compute_resynthesis_groups(driver)
    # union-find merges every overlapping scope into one group
    assert groups == [["0x1000", "0x2000", "0x3000", "0x4000"]]


@pytest.mark.asyncio
async def test_compute_resynthesis_groups_subset_membership():
    """The design's a()<->b() cycle yields one resynthesis group from its SCC."""

    def respond(query, params):
        if "f.scc_id IS NOT NULL" in query:
            return [{"sid": "scc:0x1000", "addrs": ["0x1000", "0x2000"]}]
        return []

    driver = RecordingNeo4jDriver(respond)
    groups = await compute_resynthesis_groups(driver)
    assert groups == [["0x1000", "0x2000"]]
