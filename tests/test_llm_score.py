"""Unit tests for the rubric-based LLM-judge scorer (pure parts only).

The LLM calls themselves are exercised live by eval/llm_score.py; here we lock
down the deterministic logic: pair alignment by address, ordering, rubric
aggregation math, and (optionally) the resume-cache jig.
"""

from __future__ import annotations

import pytest

from reaper.eval.llm_score import Scorer, _pairs
from reaper.harness.llm_client import ReaperLLMClient

# sample data: ground truth vs recovered names (aligned by address)
_GT = {
    "0x1000": {"function_name": "cJSON_ParseWithOpts"},
    "0x2000": {"function_name": "print_help"},
    "0x3000": {"function_name": "freeing_stuff"},
}
_OUT = {
    "0x2000": {"canon_name": "display_usage"},
    "0x1000": {"canon_name": "parse_cjson_with_options"},
    # 0x3000 intentionally missing -> recovered name stays empty
    "0x5000": {"canon_name": "unrelated_but_present"},
}


def test_pairs_aligned_by_address_and_deterministic():
    pairs = _pairs(_GT, _OUT)
    assert [p["address"] for p in pairs] == ["0x1000", "0x2000", "0x3000"]
    assert pairs[0]["true"] == "cJSON_ParseWithOpts"
    assert pairs[0]["recovered"] == "parse_cjson_with_options"
    assert pairs[2]["recovered"] == ""  # missing -> empty, still scored


def test_pairs_ignore_non_functions_and_blank_true_names():
    gt = {"0x1000": {"function_name": "ok"}, "0x9000": {}, "0x8000": {"x": 1}}
    pairs = _pairs(gt, {})
    assert len(pairs) == 1 and pairs[0]["address"] == "0x1000"


def test_aggregate_math_and_rates():
    rows = [
        {"address": "a", "true_name": "x", "recovered_name": "x",
         "scores": [10, 10, 10], "mean": 10.0, "min": 10, "max": 10,
         "std": 0.0, "justifications": []},
        {"address": "b", "true_name": "y", "recovered_name": "y2",
         "scores": [8, 9, 7], "mean": 8.0, "min": 7, "max": 9,
         "std": 0.82, "justifications": []},
        {"address": "c", "true_name": "z", "recovered_name": "",
         "scores": [0, 0, 0], "mean": 0.0, "min": 0, "max": 0,
         "std": 0.0, "justifications": []},
    ]
    agg = Scorer._aggregate(rows)
    assert agg["functions"] == 3
    assert agg["overall_mean"] == 6.0
    assert agg["strict_equivalent_rate"] == 66.7
    assert agg["related_or_better_rate"] == 66.7
    assert agg["failed_no_rename_rate"] == 33.3
    assert [r["address"] for r in agg["worst"]] == ["c", "b", "a"]


def test_resume_cache_skips_cached_runs(tmp_path, monkeypatch):
    import asyncio
    cache = tmp_path / "score_cache.jsonl"
    # run 0 already scored; run 1 is missing -> must hit the LLM
    cache.write_text(json_line("0x1000", 0, 9, "ok"))
    llm = ReaperLLMClient("http://127.0.0.1:9999", "m", run_id="test")
    scorer = Scorer(llm, str(cache), n_runs=2)
    calls = []

    async def fake_send(session_id, message, thinking_level="low", structured_output=None):
        calls.append(session_id)
        return '{"score": 9, "justification": "cached_or_new"}'

    monkeypatch.setattr(llm, "send", fake_send)
    pair = {"address": "0x1000", "true": "T", "recovered": "R"}
    r0 = asyncio.run(scorer.rate(pair, 0))
    assert r0.score == 9 and calls == []  # run 0 served from cache, no LLM call
    r1 = asyncio.run(scorer.rate(pair, 1))
    assert r1.score == 9 and len(calls) == 1  # run 1: cache miss -> real LLM call


def json_line(addr: str, run: int, score: int, just: str) -> str:
    import json
    return json.dumps({"address": addr, "run": run,
                       "result": {"score": score, "justification": just}}) + "\n"


@pytest.mark.parametrize("base,expected", [
    ("http://h:1/v1", "http://h:1"),
    ("http://h:1", "http://h:1"),
])
def test_scorer_client_normalizes_base_url(base, expected):
    llm = ReaperLLMClient(base, "m")
    try:
        assert llm.base_url == expected
    finally:
        pass  # no http client created by close needed here
