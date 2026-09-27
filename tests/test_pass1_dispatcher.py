"""Tests for Deliverable 5.2 — Pass1Dispatcher (reaper/harness/pass1_dispatcher.py).

Exercises the bottom-up sweep end-to-end against fake services: variable +
argument renames (applied to Neo4j + BNDB immediately), the function-summary
step (FunctionSummary schema + ledger writeback), and the SCC second pass.
bndb_writer.save() is monkeypatched because Binja is not installed.
"""

from __future__ import annotations

import json

import pytest

from reaper.harness.pass1_dispatcher import Pass1Dispatcher
from reaper.tools.bndb_writer import BNDBWriter

from conftest import RecordingNeo4jDriver
from fake_binja import FakeFunction, FakeVariable
from fake_harness import RecordingTracer, StubLLM, make_extractor_with_tags


class FakeContext:
    async def for_variable(self, func_address, var_id, include_callee_renames=False):
        return f"HLIL of {func_address} with >>> {var_id} <<<"

    async def for_function(self, func_address, include_callees=True, **kwargs):
        return f"Function {func_address} (callees shown) full HLIL..."


class StubLedger:
    def __init__(self):
        self.names = []
        self.summaries = []

    async def set_function_names(self, address, llm_name, canon_name):
        self.names.append((address, llm_name, canon_name))

    async def set_function_summary(self, address, summary):
        self.summaries.append((address, summary))


def _rename_json(node_id, llm_name, canon_name):
    return json.dumps({
        "renames": [{
            "node_id": node_id, "llm_name": llm_name,
            "canon_name": canon_name, "justification": "test",
        }],
        "claims": [],
    })


def _summary_json(llm_name, canon_name):
    return json.dumps({
        "llm_name": llm_name, "canon_name": canon_name,
        "summary": "parses the incoming buffer",
    })


def _driver(variables=None, arguments=None, scc=None):
    variables = variables or {"0x1000": ["0x1000:var_18"],
                              "0x2000": ["0x2000:var_20"]}
    arguments = arguments or {"0x1000": ["0x1000:arg0"], "0x2000": ["0x2000:arg0"]}
    scc = scc or []

    def respond(query, params):
        if "n:Variable" in query:
            return [{"id": i} for i in variables.get(params.get("fa"), [])]
        if "n:Argument" in query:
            return [{"id": i} for i in arguments.get(params.get("fa"), [])]
        if "f.scc_id IS NOT NULL" in query:
            return scc
        if "f.traversal_order AS traversal_order" in query:
            return [
                {"address": "0x1000", "traversal_order": 0},
                {"address": "0x2000", "traversal_order": 0},
            ]
        return []

    return RecordingNeo4jDriver(respond)


def _bndb_writer(funcs):
    return BNDBWriter(make_extractor_with_tags(functions=list(funcs)))


def _make_funcs():
    func1 = FakeFunction.simple(
        0x1000, "sub_1000",
        variables=[FakeVariable("var_18"), FakeVariable("arg0")],
        parameters=[FakeVariable("arg0")],
    )
    func2 = FakeFunction.simple(
        0x2000, "sub_2000",
        variables=[FakeVariable("var_20"), FakeVariable("arg0")],
        parameters=[FakeVariable("arg0")],
    )
    return func1, func2


@pytest.mark.asyncio
async def test_pass1_renames_variables_arguments_and_summary(monkeypatch):
    func1, func2 = _make_funcs()
    writer = _bndb_writer([func1, func2])
    monkeypatch.setattr(writer, "save", lambda: "/tmp/fake/target.bndb")

    driver = _driver()
    llm = StubLLM(responses=[
        _rename_json("0x1000:var_18", "input_buffer", "InputBuffer"),
        _rename_json("0x1000:arg0", "raw_json_input", "RawJsonInput"),
        _summary_json("parse_entry_to_struct", "ParseEntry"),
        _rename_json("0x2000:var_20", "item_ptr", "ItemPtr"),
        _rename_json("0x2000:arg0", "callback_arg", "CallbackArg"),
        _summary_json("iterate_items", "IterateItems"),
    ])
    ledger = StubLedger()
    tracer = RecordingTracer()

    dispatcher = Pass1Dispatcher(
        llm, driver, FakeContext(), writer, ledger, tracer,
        {"thinking_levels": {"pass1_rename": "minimal"},
         "limits": {"max_concurrent_agents": 4}},
    )
    await dispatcher.run()

    # Immediate apply into BNDB happened (no critic, no wait).
    names = {v.name for v in func1.vars}
    assert "InputBuffer" in names
    assert "RawJsonInput" in names
    assert func1.name == "ParseEntry"
    assert func2.name == "IterateItems"

    # Ledger writeback recorded.
    assert any(n[0] == "0x1000" and n[1] == "parse_entry_to_struct" for n in ledger.names)
    assert ledger.summaries and ledger.summaries[0][0] == "0x1000"

    # Function-name renames went to Neo4j via MERGE (function address keyed).
    func_merges = [q for q in driver.queries
                   if "MERGE (n:Function {address: $id})" in q[0]
                   and q[1].get("llm") == "parse_entry_to_struct"]
    assert func_merges
    # Variable renames also hit Neo4j.
    var_merges = [q for q in driver.queries if "MERGE (n {id: $id})" in q[0]]
    assert any(q[1].get("id") == "0x1000:var_18" for q in var_merges)

    # Every LLM call requested a structured-output schema.
    sends = [c for c in llm.calls if c["op"] == "send"]
    assert len(sends) == 6 and all(s["structured_output"] for s in sends)

    # Levels awaited + traced.
    assert any(e["event_type"] == "pass1_level_done" for e in tracer.events)


@pytest.mark.asyncio
async def test_pass1_scc_second_pass_redoes_arguments_and_summary(monkeypatch):
    func1, func2 = _make_funcs()
    writer = _bndb_writer([func1, func2])
    monkeypatch.setattr(writer, "save", lambda: "/tmp/fake/target.bndb")

    # scc row: 0x2000 belongs to scc:0x1000 → second pass re-runs its
    # argument rename + function summary ONLY (no variable renames).
    driver = _driver(
        scc=[{"address": "0x2000", "sid": "scc:0x1000"}],
        variables={"0x1000": ["0x1000:var_18"], "0x2000": []},
        arguments={"0x1000": ["0x1000:arg0"], "0x2000": ["0x2000:arg0"]},
    )
    llm = StubLLM(responses=[
        _rename_json("0x1000:var_18", "input_buffer", "InputBuffer"),
        _rename_json("0x1000:arg0", "raw_json_input", "RawJsonInput"),
        _summary_json("parse_entry_to_struct", "ParseEntry"),
        # Main sweep, 0x2000 (no variables) — arguments + summary only.
        _rename_json("0x2000:arg0", "callback_arg", "CallbackArg"),
        _summary_json("iterate_items", "IterateItems"),
        # SCC second pass for 0x2000 — arguments + summary again.
        _rename_json("0x2000:arg0", "scc_arg_new", "SccArgNew"),
        _summary_json("report_scc", "ReportScc"),
    ])
    ledger = StubLedger()

    dispatcher = Pass1Dispatcher(
        llm, driver, FakeContext(), writer, ledger, RecordingTracer(),
        {"thinking_levels": {"pass1_rename": "minimal"},
         "limits": {"max_concurrent_agents": 4}},
    )
    await dispatcher.run()

    assert func1.name == "ParseEntry"
    # SCC second pass applied its summary LAST (overwrites the main-sweep name).
    assert func2.name == "ReportScc"
    assert any(n[0] == "0x2000" for n in ledger.names)
    # 3 (0x1000) + 2 (0x2000 main) + 2 (0x2000 SCC) = 7 sends
    scc_sends = [c for c in llm.calls if c["op"] == "send"]
    assert len(scc_sends) == 7

