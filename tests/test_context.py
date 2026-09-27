"""Tests for Deliverable 3.1 — ContextAssembler (reaper/harness/context.py).

Drives the assembler against a ScriptedExtractor (string/text contract that
mirrors the real 2.1 HLILExtractor), a RecordingNeo4jDriver responder for the
graph-backed sections, and a stub ledger. Verifies every public method and the
for_subgraph truncation ladder (low-confidence claims dropped under budget).
"""

from __future__ import annotations

import pytest

from reaper.harness.context import ContextAssembler
from reaper.tools.struct_detector import FieldAccess, StructCandidate

from conftest import RecordingNeo4jDriver
from fake_harness import ScriptedExtractor


def _norm(addr):
    if isinstance(addr, int):
        return f"0x{addr:x}"
    return str(addr)


class StubLedger:
    def __init__(self, claims=None):
        self._claims = claims or {}

    async def get_claims(self, addr):
        return list(self._claims.get(_norm(addr), []))


def _extractor():
    return ScriptedExtractor(
        hlils={
            "0x1000": (
                "0x1000: int64_t parse(char* arg1)\n"
                "0x1010: var_18 = *arg1\n"
                "0x1020: call worker_beat\n"
                "0x1030: return var_18\n"
            ),
            "0x2000": "0x2000: void worker(void)\n0x2010: return\n",
        },
        signatures={
            "0x1000": "int64_t parse(char* arg1)",
            "0x2000": "void worker(void)",
        },
        refs={"0x1000": [{"address": "0x5000", "value": "token"}]},
        params={"0x1000": [{"index": 0, "name": "arg1", "type": "char*"}]},
        variables={"0x1000": [{"name": "var_18", "type": "int64_t"}]},
    )


def _driver():
    def respond(query, params):
        if "RETURN c.address AS id" in query:
            return [{"id": "0x2000", "llm_name": "worker_beat", "canon_name": "WorkerBeat"}]
        if "RETURN p.address AS id" in query:
            return [{"id": "0x9000", "llm_name": None, "canon_name": "CallerThing"}]
        if "f.pinned = true" in query:
            return [{"id": "0x3000", "llm_name": "printf", "canon_name": "printf"}]
        if "RETURN count(c) AS n" in query:
            return [{"n": 1}]
        return []

    return RecordingNeo4jDriver(respond)


def _assembler(claims=None, max_tokens=32768):
    ledger = StubLedger(claims)
    config = {"limits": {"max_context_tokens": max_tokens}}
    return ContextAssembler(_extractor(), _driver(), ledger, config), ledger


@pytest.mark.asyncio
async def test_for_function_optional_sections():
    claims = {"0x1000": [
        {"id": 1, "claim_text": "This function parses JSON.",
         "truth_level": "high_confidence"}
    ]}
    ca, _ = _assembler(claims=claims)
    text = await ca.for_function(
        "0x1000", include_callees=True, include_callers=True, include_claims=True
    )
    assert "Callees:" in text and "WorkerBeat" in text
    assert "Callers:" in text and "CallerThing" in text
    assert "[claim_id=1] This function parses JSON. (high_confidence)" in text


@pytest.mark.asyncio
async def test_for_variable_highlights_target():
    ca, _ = _assembler()
    text = await ca.for_variable("0x1000", "0x1000:var_18")
    assert ">>> var_18 <<<" in text
    assert "TARGET: Rename the variable `var_18`" in text
    assert "token" in text


@pytest.mark.asyncio
async def test_for_variable_highlight_is_word_boundary_aware():
    """A short variable name must not corrupt bigger identifiers: renaming `c`
    must highlight only standalone `c` tokens, never the `c` inside `char`,
    `calc`, etc. (regression for the old hl.replace bug)."""
    extractor = ScriptedExtractor(
        hlils={"0x3000": (
            "0x3000: c = char(0x41)\n"
            "0x3010: calc = c + 1\n"
            "0x3020: if (c == 0) return c\n"
        )},
        variables={"0x3000": [{"name": "c", "type": "int"}]},
    )
    ca = ContextAssembler(extractor, _driver(), StubLedger(),
                          {"limits": {"max_context_tokens": 32768}})
    text = await ca.for_variable("0x3000", "0x3000:c")
    assert text.count(">>> c <<<") == 4      # only the standalone tokens
    assert "char(0x41)" in text              # not ">>> char <<<"
    assert "calc = >>> c <<< + 1" in text       # calc identifier untouched


@pytest.mark.asyncio
async def test_for_struct_candidate_groups_by_function():
    ca, _ = _assembler()
    candidate = StructCandidate(
        candidate_id="struct_cand_0",
        base_type_hint="struct cJSON*",
        accesses=[
            FieldAccess(offset=0, size=8, access_type="read",
                        function_address="0x1000", instruction_address="0x1010"),
            FieldAccess(offset=8, size=8, access_type="read",
                        function_address="0x1000", instruction_address="0x1020"),
        ],
        functions_involved=["0x1000"],
    )
    text = await ca.for_struct_candidate(candidate)
    assert "Struct candidate: struct_cand_0" in text
    assert "Function 0x1000:" in text
    assert "+0x0 [read, 8B] 0x1010: var_18 = *arg1" in text
    assert "+0x8 [read, 8B] 0x1020: call worker_beat" in text


@pytest.mark.asyncio
async def test_for_task_decomposes_context_spec():
    ca, _ = _assembler()
    task = {
        "description": "figure out what parse does",
        "goal": "determine return contract",
        "start_position": "0x1000",
        "context_spec": {"functions": ["0x1000"], "include_claims": True},
    }
    text = await ca.for_task(task)
    assert "TASK: figure out what parse does" in text
    assert "GOAL: determine return contract" in text
    assert "var_18 = *arg1" in text


@pytest.mark.asyncio
async def test_for_subgraph_includes_claim_ids():
    claims = {"0x1000": [
        {"id": 1, "claim_text": "This function parses JSON.",
         "truth_level": "high_confidence"}
    ]}
    ca, _ = _assembler(claims=claims)
    text = await ca.for_subgraph(["0x1000", "0x2000"])
    assert "[claim_id=1] This function parses JSON. (high_confidence)" in text
    assert "var_18 = *arg1" in text
    assert "0x2000: void worker(void)" in text


@pytest.mark.asyncio
async def test_for_subgraph_truncation_drops_low_confidence_claims():
    """Under a tight token budget the ladder must drop sub-mid_confidence
    claims (step 1) and never re-add them, while keeping high-confidence ones."""
    claims = {"0x1000": [
        {"id": 1, "claim_text": "high claim", "truth_level": "high_confidence"},
        {"id": 2, "claim_text": "low claim", "truth_level": "speculation"},
    ]}
    ca, _ = _assembler(claims=claims, max_tokens=40)
    text = await ca.for_subgraph(["0x1000"])
    assert "high claim" in text
    assert "low claim" not in text
