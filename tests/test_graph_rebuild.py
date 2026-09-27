"""Tests for Deliverable 4.3 — GraphRebuilder (reaper/tools/graph_rebuild.py).

Applies a StructDefinition to a graph whose functions contain struct-access
HLIL patterns: verifies the :Struct node, explicit per-field :Variable nodes,
:FIELD_OF edges, the obsolete placeholder-walker prune query, and that
validate_and_order is re-invoked. Also covers the fallback/no-affected paths.
"""

from __future__ import annotations

import pytest

from reaper.harness.submission import StructDefinition, StructField
from reaper.tools.graph_rebuild import GraphRebuilder

from conftest import RecordingNeo4jDriver
from fake_binja import (
    FakeExtractor,
    FakeFunction,
    FakeVariable,
    add,
    assign,
    const,
    deref,
    struct_field,
    var,
)


class RecorderWriter:
    """BNDBWriter double that records set_struct_type calls."""

    def __init__(self):
        self.calls = []

    def set_struct_type(self, struct_name, struct_str):
        self.calls.append((struct_name, struct_str))
        return False


def _struct_def():
    return StructDefinition(
        struct_name="cjson_node",
        fields=[
            StructField(offset=0, name="next", type_str="struct cjson_node*",
                        size=8, confidence="high_confidence"),
            StructField(offset=8, name="obj_type", type_str="int32_t",
                        size=4, confidence="mid_confidence"),
        ],
    )


def _extractor_with_accesses():
    func_a = FakeFunction(
        0x1000, "parse",
        instructions=[
            assign(var("item"), deref(add(var("node"), const(0)), address=0x1010),
                   address=0x1010),
            assign(var("tmp"), struct_field(var("node"), member="type", offset=8,
                                            address=0x1020), address=0x1020),
        ],
        variables=[FakeVariable("node", type_str="struct cJSON*")],
    )
    return FakeExtractor.with_functions([func_a])


def _recording_driver():
    def respond(query, params):
        if "RETURN f.address AS address" in query:
            return [{"address": "0x1000", "pinned": False}]
        return []

    return RecordingNeo4jDriver(respond)


@pytest.mark.asyncio
async def test_apply_struct_builds_field_graph_and_revalidates():
    driver = _recording_driver()
    writer = RecorderWriter()
    rebuilder = GraphRebuilder(_extractor_with_accesses(), writer, driver)

    affected = await rebuilder.apply_struct(_struct_def(), ["0x1000"])

    assert affected == ["0x1000"]
    assert writer.calls and writer.calls[0][0] == "cjson_node"
    assert "struct cjson_node {" in writer.calls[0][1]

    # :Struct node registered with the C definition.
    struct_merges = [q for q in driver.queries_matching("MERGE (s:Struct {id: $id})")]
    assert struct_merges and struct_merges[0][1]["id"] == "struct:cjson_node"
    assert "struct cjson_node {" in struct_merges[0][1]["cdef"]

    # Explicit per-field Variable nodes created for the detected accesses
    # (filter to the node-MERGE query — CONTAINS/FIELD_OF also carry ids).
    field_ids = [q[1]["id"] for q in driver.queries
                 if "SET v.field_offset = $off" in q[0]
                 and str(q[1].get("id", "")).startswith("0x1000:cjson_node.")]
    assert set(field_ids) == {"0x1000:cjson_node.next", "0x1000:cjson_node.obj_type"}

    # :FIELD_OF edges to the struct node exist.
    field_of = driver.queries_matching("FIELD_OF]->(s)")
    assert field_of and all(q[1].get("sid") == "struct:cjson_node" for q in field_of)

    # :CONTAINS from the function to each field node.
    contains = driver.queries_matching("MERGE (f)-[:CONTAINS]->(v)")
    assert len(contains) == 2

    # Obsolete placeholder prune: keep list excludes nothing we created.
    prune = driver.queries_matching("DETACH DELETE v")
    assert prune
    assert prune[0][1]["pattern"] == ".*@0x[0-9a-f]+$"
    assert set(prune[0][1]["keep"]) == {
        "0x1000:cjson_node.next", "0x1000:cjson_node.obj_type"
    }
    assert prune[0][1]["addrs"] == ["0x1000"]

    # validate_and_order was re-invoked (traversal_order writes emitted).
    assert driver.queries_matching("f.traversal_order = $order")


@pytest.mark.asyncio
async def test_apply_struct_infers_functions_from_extractor_when_omitted():
    driver = _recording_driver()
    rebuilder = GraphRebuilder(_extractor_with_accesses(), None, driver)
    affected = await rebuilder.apply_struct(_struct_def())
    assert affected == ["0x1000"]
    assert driver.queries_matching("FIELD_OF]->(s)")


@pytest.mark.asyncio
async def test_apply_struct_no_affected_functions_is_noop():
    driver = _recording_driver()
    rebuilder = GraphRebuilder(FakeExtractor(), RecorderWriter(), driver)
    affected = await rebuilder.apply_struct(_struct_def(), [])
    assert affected == []
    # no field node, no prune, no validate writes
    assert not driver.queries_matching("FIELD_OF]->(s)")
    assert not driver.queries_matching("DETACH DELETE v")
    assert not driver.queries_matching("traversal_order")


@pytest.mark.asyncio
async def test_apply_struct_skips_accesses_without_matching_field():
    driver = _recording_driver()
    # struct only defines offset 8; the detector also reports offset 0 which
    # must NOT create a node.
    struct_def = StructDefinition(
        struct_name="partial", fields=[
            StructField(offset=8, name="obj_type", type_str="int32_t",
                        size=4, confidence="mid_confidence"),
        ],
    )
    rebuilder = GraphRebuilder(_extractor_with_accesses(), None, driver)
    affected = await rebuilder.apply_struct(struct_def, ["0x1000"])
    assert affected == ["0x1000"]
    field_ids = [q[1]["id"] for q in driver.queries
                 if "SET v.field_offset = $off" in q[0]
                 and str(q[1].get("id", "")).startswith("0x1000:partial.")]
    assert field_ids == ["0x1000:partial.obj_type"]
    assert "0x1000:partial.next" not in field_ids
