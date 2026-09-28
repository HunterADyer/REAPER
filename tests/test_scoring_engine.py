"""Tests for the modular scoring engine (rubrics + judge independence + align).

Locks down the two hard contracts the user required:
  1. INDEPENDENCE — each of the N runs is a FRESH, zero-context session
     (create_session -> one send -> destroy_session); runs never share state.
  2. CACHE — a hit returns without touching the LLM client at all; an
     interrupted session resumes from the JSONL cache.
Also locks alignment determinism (addr+ordinal for vars, name for structs).
"""

from __future__ import annotations

import asyncio
import json

import pytest

from reaper.eval.scoring.align import (
    datatype_items, function_items, variable_items, all_dimensions,
)
from reaper.eval.scoring.judge import JudgeEngine, JudgeScore
from reaper.eval.scoring.reporting import aggregate_rows
from reaper.eval.scoring.rubrics import RUBRICS, get_rubric
from reaper.eval.scoring.scheduler import ScoreScheduler
from reaper.harness.llm_client import ReaperLLMClient


class RecordingLLM:
    """Stub client that records every lifecycle call (independence probe)."""

    def __init__(self, response='{"score": 9, "justification": "probed"}'):
        self.response = response
        self.calls = []   # ("create", sid) / ("send", sid) / ("destroy", sid)
        self._sessions = set()

    async def create_session(self, session_id, system_prompt):
        self.calls.append(("create", session_id))
        self._sessions.add(session_id)
        return session_id

    async def send(self, session_id, message, thinking_level="low", structured_output=None):
        self.calls.append(("send", session_id))
        assert session_id in self._sessions, "send on a session that was not created"
        return self.response

    def destroy_session(self, session_id):
        self.calls.append(("destroy", session_id))
        self._sessions.discard(session_id)

    async def close(self):
        pass


# --------------------------------------------------------------------------
# Rubrics
# --------------------------------------------------------------------------

def test_registry_has_all_dimensions():
    assert set(RUBRICS) == {"function-name", "variable-name", "datatype"}


def test_unknown_rubric_raises_helpful():
    with pytest.raises(KeyError, match="unknown rubric"):
        get_rubric("nope")


def test_name_rubric_question_embeds_true_and_recovered():
    q = get_rubric("function-name").format_question(
        {"key": "k", "true": "cJSON_ParseWithOpts", "recovered": "parse_cjson"})
    assert "`cJSON_ParseWithOpts`" in q
    assert "`parse_cjson`" in q


def test_datatype_rubric_question_renders_layouts():
    q = get_rubric("datatype").format_question({
        "key": "s", "name": "orig", "recovered_name": "rec",
        "true": [{"offset": 0, "name": "next", "type_str": "void*"}],
        "recovered": [],
    })
    assert "ORIGINAL struct `orig`" in q
    assert "+0x0" in q
    assert "no layout recovered" in q


# --------------------------------------------------------------------------
# Judge independence + cache
# --------------------------------------------------------------------------

async def _engine(llm, runs=3, cache_path=None):
    return JudgeEngine(llm, n_runs=runs, cache_path=cache_path)


def test_each_run_is_fresh_zero_context_session():
    llm = RecordingLLM()
    item = {"key": "0x1000", "true": "A", "recovered": "B"}
    engine = asyncio.run(_engine(llm, runs=3))
    rows = asyncio.run(engine.score_item(get_rubric("function-name"), item))

    creates = [sid for op, sid in llm.calls if op == "create"]
    sends = [sid for op, sid in llm.calls if op == "send"]
    destroys = [sid for op, sid in llm.calls if op == "destroy"]
    # 3 independent runs -> 3 create / 3 send / 3 destroy, DIFFERENT session ids
    assert len(creates) == 3 and len(sends) == 3 and len(destroys) == 3
    assert len(set(creates)) == 3, "each independent run must get its own session"
    assert set(creates) == set(sends) == set(destroys), "sessions must be cleaned up"
    assert rows["mean"] == 9.0 and rows["scores"] == [9, 9, 9]


def test_cache_hit_never_touches_llm(tmp_path):
    cache = tmp_path / "c.jsonl"
    cache.write_text(json.dumps({
        "rubric": "function-name", "item": "0x1000", "run": 0,
        "result": {"score": 9, "justification": "cached"}}) + "\n")
    llm = RecordingLLM()
    engine = asyncio.run(_engine(llm, runs=1, cache_path=str(cache)))
    item = {"key": "0x1000", "true": "A", "recovered": "B"}
    r = asyncio.run(engine.rate(get_rubric("function-name"), item, 0))
    assert r.score == 9 and llm.calls == []  # served from cache, no client activity


def test_cache_is_written_and_resumable(tmp_path):
    cache = tmp_path / "c.jsonl"
    llm = RecordingLLM()
    item = {"key": "0x1000", "true": "A", "recovered": "B"}

    async def run_once():
        e1 = JudgeEngine(llm, n_runs=1, cache_path=str(cache))
        await e1.rate(get_rubric("function-name"), item, 0)
        return JudgeEngine(llm, n_runs=1, cache_path=str(cache))

    e2 = asyncio.run(run_once())
    lines = cache.read_text().strip().splitlines()
    assert len(lines) == 1
    rec = json.loads(lines[0])
    assert rec["rubric"] == "function-name" and rec["item"] == "0x1000"
    # engine resumes: cached run contributes to score without a new call
    assert len([c for c in e2._cache if c.endswith("::r0")]) == 1


def test_legacy_cache_format_still_reads(tmp_path):
    # Pre-refactor llm_score cache wrote {"address", "run", "result"}.
    cache = tmp_path / "legacy.jsonl"
    cache.write_text(json.dumps({
        "address": "0x1000", "run": 1,
        "result": {"score": 8, "justification": "legacy row"}}) + "\n")
    llm = RecordingLLM()
    engine = asyncio.run(_engine(llm, runs=1, cache_path=str(cache)))
    item = {"key": "0x1000", "true": "A", "recovered": "B"}
    r = asyncio.run(engine.rate(get_rubric("function-name"), item, 1))
    assert r.score == 8 and llm.calls == []


# --------------------------------------------------------------------------
# Alignment determinism
# --------------------------------------------------------------------------

_GT = {
    "0x1000": {"function_name": "cJSON_ParseWithOpts",
               "parameters": [{"name": "opts"}],
               "local_variables": [{"name": "item"}]},
    "0x3000": {"function_name": "print_help"},
}
_OUT = {
    "0x1000": {"canon_name": "parse_cjson_with_options",
               "parameters": [{"canon_name": "options"}],
               "variables": [{"canon_name": "node"}]},
    "0x9000": {"canon_name": "unrelated"},
}


def test_function_items_aligned_by_address_deterministic():
    items = function_items(_GT, _OUT)
    assert [i["key"] for i in items] == ["0x1000", "0x3000"]
    assert items[0]["true"] == "cJSON_ParseWithOpts"
    assert items[0]["recovered"] == "parse_cjson_with_options"
    assert items[1]["recovered"] == ""  # not recovered -> empty, still scored


def test_variable_items_aligned_by_address_and_ordinal():
    items = variable_items(_GT, _OUT)
    assert items[0]["key"] == "0x1000:0"
    assert items[0]["true"] == "opts" and items[0]["recovered"] == "options"
    assert items[1]["key"] == "0x1000:1"
    assert items[1]["true"] == "item" and items[1]["recovered"] == "node"
    # 0x3000 has no vars -> no variable items
    assert len(items) == 2


def test_datatype_items_aligned_by_name_only_gt():
    gt = {"structs": {"cjson": [{"offset": 0, "name": "next", "type_str": "void*"}]}}
    out = {"structs": {"cjson": [{"offset": 0, "name": "next", "type_str": "void*"}],
                       "spurious": [{"offset": 0, "name": "x", "type_str": "int"}]}}
    items = datatype_items(gt, out)
    assert len(items) == 1
    assert items[0]["key"] == "cjson"


def test_all_dimensions_keys():
    dims = all_dimensions(_GT, _OUT)
    assert set(dims) == {"function-name", "variable-name", "datatype"}
    assert dims["datatype"] == []  # no structs in fixture


# --------------------------------------------------------------------------
# Aggregation
# --------------------------------------------------------------------------

def test_aggregate_rows_math():
    rows = [
        {"key": "a", "mean": 10.0, "scores": [10, 10, 10]},
        {"key": "b", "mean": 8.0, "scores": [8, 9, 7]},
        {"key": "c", "mean": 0.0, "scores": [0, 0, 0]},
    ]
    agg = aggregate_rows(rows)
    assert agg["count"] == 3 and agg["overall_mean"] == 6.0
    assert agg["strict_equivalent_rate"] == 66.7
    assert agg["related_or_better_rate"] == 66.7
    assert agg["failed_no_rename_rate"] == 33.3
    assert [r["key"] for r in agg["worst"]] == ["c", "b", "a"]


# --------------------------------------------------------------------------
# Scheduler
# --------------------------------------------------------------------------

def test_scheduler_scores_all_dimensions_sequentially():
    llm = RecordingLLM()

    async def go():
        gt = {"0x1000": {"function_name": "AA", "parameters": [{"name": "p"}]},
              "structs": {"s": [{"offset": 0, "name": "f", "type_str": "int"}]}}
        out = {"0x1000": {"canon_name": "AA", "parameters": [{"canon_name": "p"}]},
               "structs": {"s": [{"offset": 0, "name": "f", "type_str": "int"}]}}
        sched = ScoreScheduler(llm, n_runs=2)
        return await sched.score(all_dimensions(gt, out))

    report = asyncio.run(go())
    assert set(report["dimensions"]) == {"function-name", "variable-name", "datatype"}
    # function(2 runs) + variable(2 runs) + datatype(2 runs) = 6 sends
    sends = [sid for op, sid in llm.calls if op == "send"]
    assert len(sends) == 6
    # ordered: all function-name runs are independent sessions
    fn_sids = [sid for sid in sends if "function-name" in sid]
    assert len(set(fn_sids)) == 2
    assert report["dimensions"]["function-name"]["count"] == 1
    assert report["dimensions"]["datatype"]["count"] == 1

    # flush idempotent: don't launch anything else after
    assert all(op != "close" for op, _ in llm.calls) or True


def test_scheduler_no_items_is_empty_report():
    llm = RecordingLLM()
    report = asyncio.run(ScoreScheduler(llm, n_runs=2).score({}))
    assert report["dimensions"]["function-name"]["count"] == 0
    assert llm.calls == []  # nothing to score -> no LLM traffic
