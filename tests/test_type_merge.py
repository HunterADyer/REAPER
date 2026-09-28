"""Tests for the type-recovery redesign: merge by SHARED DATA TYPE, unions,
and consistent BNDB type binding (the "apply consistently through the bndb"
contract in run-audit.md).

Covers:
  - StructAccessDetector merges partial structs across functions via
    call-context evidence (a field-accessed base passed as an argument into a
    callee whose parameter is also field-accessed, or returned and stored)
    into ONE candidate carrying the union of all offsets — never 3 partial
    structs.
  - The merged candidate carries base_names (function -> bound bases) so the
    recovered pointer type can be retagged onto every involved variable.
  - Overlapping/aliased offsets at the same byte range fire the union hint.
  - GraphRebuilder binds ``struct <name> *`` / ``union <name> *`` to every
    involved base via BNDBWriter.set_variable_type, and emits ``union {...}``
    for kind="union".
  - for_ratify routes recovered struct/field layouts into the context as
    evidence for the naming passes.
"""

from __future__ import annotations

import pytest
from pydantic import BaseModel

from reaper.harness.context import ContextAssembler
from reaper.harness.submission import StructDefinition, StructField
from reaper.tools.graph_rebuild import GraphRebuilder
from reaper.tools.struct_detector import StructAccessDetector

from fake_binja import (
    FakeExtractor,
    FakeFunction,
    FakeVariable,
    add,
    assign,
    call,
    const,
    deref,
    hlil_ret,
    struct_field,
    var,
)


def _handler(address, offset, param_name):
    """A "handler" that field-accesses ONE offset on a struct pointer arg.

    Its caller passes the SAME struct pointer in (call-context evidence); by
    itself this is only a PARTIAL view of the shared data type.
    """
    return FakeFunction(
        address, f"handler_{address:x}",
        instructions=[assign(var("tmp"), struct_field(
            var(param_name), member=f"f", offset=offset,
            address=address + 1),
            address=address + 1)],
        parameters=[FakeVariable(param_name, type_str="$unknown")],
    )


def _dispatcher_that_passes_same_pointer():
    """A dispatcher receives a struct pointer and passes it to 3 handlers.

    Each handler dereferences a DIFFERENT offset; together the offsets are the
    complete struct, but no single function sees more than one field.
    """
    func = FakeFunction(
        0x1000, "dispatch",
        instructions=[
            # offset 0 read directly in the dispatcher too
            assign(var("e0"), deref(add(var("cfg"), const(0)),
                                    address=0x1010), address=0x1010),
            call(dest=None, params=[var("cfg")], address=0x1020,
                 target_func={"name": "handler_2000", "address": 0x2000},
                 target_addr=0x2000),
            call(dest=None, params=[var("cfg")], address=0x1030,
                 target_func={"name": "handler_3000", "address": 0x3000},
                 target_addr=0x3000),
            call(dest=None, params=[var("cfg")], address=0x1040,
                 target_func={"name": "handler_4000", "address": 0x4000},
                 target_addr=0x4000),
        ],
        variables=[FakeVariable("cfg", type_str="$unknown")],
    )
    handlers = [
        _handler(0x2000, 8, "item"),
        _handler(0x3000, 16, "item"),
        _handler(0x4000, 24, "item"),
    ]
    return FakeExtractor.with_functions([func, *handlers])


def test_call_context_merges_partial_structs_into_one_candidate():
    detector = StructAccessDetector(_dispatcher_that_passes_same_pointer())
    candidates = detector.find_struct_accesses()
    # ONE merged candidate, not 4 partial ones.
    assert len(candidates) == 1, [c.candidate_id for c in candidates]
    cand = candidates[0]
    # the union of ALL observed offsets is the complete struct
    offsets = sorted({a.offset for a in cand.accesses})
    assert offsets == [0, 8, 16, 24], offsets
    # every function involved (dispatcher + 3 handlers) is listed
    fns = {int(a, 16) for a in cand.functions_involved}
    assert fns == {0x1000, 0x2000, 0x3000, 0x4000}, fns
    # base_names map each function to the base that must be retagged
    assert cand.base_names.get("0x1000") == ["cfg"]
    for h in ("0x2000", "0x3000", "0x4000"):
        assert cand.base_names.get(h) == ["item"], cand.base_names


def test_return_flow_merges_across_functions():
    """Caller stores the callee's returned struct-typed value and the callee
    returns a field-accessed base — same data type flows back."""
    producer = FakeFunction(
        0x2200, "make_config",
        instructions=[
            assign(var("tmp"), struct_field(
                var("item"), member="ver", offset=40, address=0x2210),
                address=0x2210),
            hlil_ret(var("item"), address=0x2220),
        ],
        variables=[FakeVariable("item", type_str="$unknown")],
    )
    consumer = FakeFunction(
        0x1100, "consume_config",
        instructions=[
            assign(var("got"), call(dest=var("got"), params=[], address=0x1110,
                                    target_func={"name": "make_config",
                                                 "address": 0x2200},
                                    target_addr=0x2200),
                   address=0x1110),
            assign(var("tmp"), struct_field(
                var("got"), member="f", offset=32,
                address=0x1120), address=0x1120),
        ],
        variables=[FakeVariable("got", type_str="$unknown")],
    )
    extractor = FakeExtractor.with_functions([producer, consumer])
    candidates = StructAccessDetector(extractor).find_struct_accesses()
    merged = [c for c in candidates if 32 in {a.offset for a in c.accesses}
              and 40 in {a.offset for a in c.accesses}]
    assert len(merged) == 1, [c.candidate_id for c in candidates]
    fns = {int(a, 16) for a in merged[0].functions_involved}
    assert {0x1100, 0x2200} <= fns


def test_overlapping_offsets_fire_union_hint():
    """Two accesses to the SAME byte range with different sizes -> union hint."""
    read_deref = deref(add(var("buf"), const(0)), address=0x3010)
    read_deref.type_ref = FakeVariable("as_int", type_str="int32_t")
    tag_field = struct_field(var("buf"), member="tag", offset=0,
                             address=0x3020)
    tag_field.type_ref = FakeVariable("tag", type_str="void*")
    func = FakeFunction(
        0x3000, "ambiguous",
        instructions=[
            assign(var("a"), read_deref, address=0x3010),
            assign(var("b"), tag_field, address=0x3020),
        ],
        variables=[FakeVariable("buf", type_str="$unknown")],
    )
    detector = StructAccessDetector(FakeExtractor.with_functions([func]))
    candidates = detector.find_struct_accesses()
    assert candidates, "expected a candidate"
    assert all(c.overlap_hint for c in candidates)


# ---------------------------------------------------------------------------
# BNDB type binding (apply consistently through the bndb)
# ---------------------------------------------------------------------------


class RecorderBindWriter:
    def __init__(self):
        self.struct_calls = []
        self.type_calls = []

    def set_struct_type(self, struct_name, struct_str):
        self.struct_calls.append((struct_name, struct_str))
        return False

    def set_variable_type(self, func_address, node_id, type_str):
        self.type_calls.append((func_address, node_id, type_str))


def _recording_driver():
    from conftest import RecordingNeo4jDriver

    def respond(query, params):
        if "RETURN f.address AS address" in query:
            return [{"address": "0x1000", "pinned": False}]
        return []

    return RecordingNeo4jDriver(respond)


def _extractor_with_bases():
    node_field = struct_field(var("node"), member="next", offset=0, address=0x1010)
    node_field.type_ref = FakeVariable("next", type_str="void*")
    func = FakeFunction(
        0x1000, "parse",
        instructions=[assign(var("tmp"), node_field, address=0x1010)],
        variables=[FakeVariable("node", type_str="struct cJSON*")],
    )
    return FakeExtractor.with_functions([func])


@pytest.mark.asyncio
async def test_apply_struct_binds_merged_type_to_all_bases():
    driver = _recording_driver()
    writer = RecorderBindWriter()
    rebuilder = GraphRebuilder(_extractor_with_bases(), writer, driver)
    sd = StructDefinition(
        struct_name="config_entry", kind="struct",
        fields=[StructField(offset=0, name="next", type_str="void*",
                            size=8, confidence="mid_confidence")],
    )
    await rebuilder.apply_struct(
        sd, ["0x1000"], base_names={"0x1000": ["node"], "0x2000": ["item"]},
    )
    # struct defined + every involved base retagged with the pointer type
    assert writer.struct_calls and writer.struct_calls[0][0] == "config_entry"
    binding = {b[1]: b[2] for b in writer.type_calls}
    assert binding.get("0x1000:node") == "struct config_entry *"
    assert binding.get("0x2000:item") == "struct config_entry *"


@pytest.mark.asyncio
async def test_apply_union_emits_union_layout_and_binds_union_pointer():
    driver = _recording_driver()
    writer = RecorderBindWriter()
    rebuilder = GraphRebuilder(_extractor_with_bases(), writer, driver)
    sd = StructDefinition(
        struct_name="uq", kind="union",
        fields=[StructField(offset=0, name="as_int", type_str="int32_t",
                            size=4, confidence="mid_confidence"),
                StructField(offset=0, name="as_ptr", type_str="void*",
                            size=8, confidence="mid_confidence")],
    )
    await rebuilder.apply_struct(sd, ["0x1000"],
                                 base_names={"0x1000": ["node"]})
    assert writer.struct_calls and "union uq {" in writer.struct_calls[0][1]
    assert writer.type_calls[0][2] == "union uq *"


def test_c_struct_emits_union_keyword():
    sd = StructDefinition(
        struct_name="uq", kind="union",
        fields=[StructField(offset=0, name="a", type_str="int32_t", size=4,
                            confidence="mid_confidence")],
    )
    text = GraphRebuilder._c_struct(sd)
    assert text.startswith("union uq {")
    sd2 = sd.model_copy(update={"kind": "struct"})
    assert GraphRebuilder._c_struct(sd2).startswith("struct uq {")


# ---------------------------------------------------------------------------
# for_ratify routes recovered types as evidence
# ---------------------------------------------------------------------------


class _FakeRecoveryLedger:
    def __init__(self, recovered):
        self._recovered = recovered

    async def get_recovered_types(self):
        return self._recovered


class _FakeContextExtractor:
    def __init__(self, hlil="", sig=""):
        self._hlil, self._sig = hlil, sig

    def get_string_refs(self, func_address):
        return []

    def get_function_signature(self, func_address):
        return self._sig

    def get_function_hlil(self, func_address):
        return self._hlil


class _RecordingDriver:
    """Minimal driver returning a function node + entities for for_ratify."""

    def __init__(self):
        self.queries = []

    def session(self):
        return _FakeSession(self)

    def _answer(self, query, params):
        self.queries.append(query)
        if "RETURN f.name AS name" in query and "{address: $a}" in query:
            return [{"name": "parse", "llm_name": None, "canon_name": None}]
        if "CONTAINS]->(n:Argument)" in query or "CONTAINS]->(n:Variable)" in query:
            return [{
                "id": "0x1000:node", "name": "node", "llm_name": None,
                "canon_name": None, "type": "struct config_entry *",
                "ordinal": 0,
            }]
        if "pinned = true" in query:
            return []
        return []


class _FakeSession:
    def __init__(self, driver):
        self._driver = driver

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def run(self, query, parameters=None, **kwargs):
        params = {}
        if parameters:
            params.update(parameters)
        params.update(kwargs)
        return _FakeResult(self._driver._answer(query, params))


class _FakeResult:
    def __init__(self, rows):
        self._rows = rows or []

    def __aiter__(self):
        self._it = iter(self._rows)
        return self

    async def __anext__(self):
        try:
            return next(self._it)
        except StopIteration:
            raise StopAsyncIteration


@pytest.mark.asyncio
async def test_for_ratify_includes_recovered_types_as_evidence():
    ledger = _FakeRecoveryLedger([{
        "name": "config_entry", "kind": "struct", "base_type": None,
        "fields": [{"offset": 8, "name": "max_entries", "type_str": "uint64_t"},
                   {"offset": 16, "name": "items", "type_str": "void*"}],
    }])
    ctx = ContextAssembler(_FakeContextExtractor(), _RecordingDriver(), ledger, {})
    text = await ctx.for_ratify("0x1000")
    assert "Recovered types bound to this function" in text
    assert "struct config_entry {"
    assert "+0x8 uint64_t max_entries" in text
    assert "+0x10 void* items" in text
