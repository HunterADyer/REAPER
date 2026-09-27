"""Tests for Deliverable 8.3 — ground-truth evaluator (eval/evaluate.py).

Validates the six design metrics against canned data, the semantic judges
(collapse to exact when no judge is provided; lift with a stub judge), and
the LLMJudge wrapper's structured-output request.
"""

from __future__ import annotations

import json

import pytest

from eval.evaluate import LLMJudge, Metrics, compute_metrics, to_report

from fake_harness import StubLLM


class StubJudge:
    def __init__(self, answers):
        self.answers = list(answers)
        self.questions = []

    async def yes_no(self, question):
        self.questions.append(question)
        return self.answers.pop(0) if self.answers else False


_GT = {
    "0x1000": {
        "function_name": "cJSON_Parse",
        "parameters": [{"name": "value", "type": "const char *"}],
        "local_variables": [{"name": "c", "type": "cJSON *"}],
    },
    "0x2000": {
        "function_name": "cJSON_Delete",
        "parameters": [{"name": "item", "type": "cJSON *"}],
        "local_variables": [],
    },
}


def _rp_exact():
    return {
        "0x1000": {"canon_name": "cJSON_Parse",
                   "parameters": [{"canon_name": "value"}],
                   "variables": [{"canon_name": "c"}]},
        "0x2000": {"canon_name": "cJSON_Delete",
                   "parameters": [{"canon_name": "item"}]},
    }


@pytest.mark.asyncio
async def test_exact_match_without_judge_equals_semantic():
    metrics = await compute_metrics(_GT, _rp_exact(), judge=None)
    report = to_report(metrics)
    assert report["function_name_exact_match"] == 100.0
    assert report["function_name_semantic_match"] == 100.0
    assert report["variable_name_exact_match"] == 100.0


@pytest.mark.asyncio
async def test_non_matching_without_judge_collapses_to_exact():
    rp = {"0x1000": {"canon_name": "CompletelyWrongName",
                     "parameters": [], "variables": []},
          "0x2000": {"canon_name": "AlsoWrong", "parameters": [], "variables": []}}
    report = to_report(await compute_metrics(_GT, rp, judge=None))
    assert report["function_name_exact_match"] == 0.0
    assert report["function_name_semantic_match"] == 0.0  # no judge → exact only


@pytest.mark.asyncio
async def test_judge_boosts_semantic_match():
    rp = {"0x1000": {"canon_name": "parse_json_input",
                     "parameters": [{"canon_name": "raw_string"}],
                     "variables": []},
          "0x2000": {"canon_name": "cJSON_Delete", "parameters": [], "variables": []}}
    judge = StubJudge([True, False])  # fn semantic yes; var semantic no
    metrics = await compute_metrics(_GT, rp, judge=judge)
    report = to_report(metrics)
    # 0x1000 exact=0, semantic=1 (judge yes); 0x2000 exact=1
    assert report["function_name_exact_match"] == 50.0
    assert report["function_name_semantic_match"] == 100.0
    # variable: 0x1000 var position 0 = "value" vs "raw_string" → judge says no
    assert report["variable_name_semantic_match"] == 0.0


@pytest.mark.asyncio
async def test_claim_coverage_and_type_recovery_and_false_confidence():
    gt = dict(_GT)
    gt["structs"] = {"cJSON": [{"offset": 0, "size": 8},
                               {"offset": 8, "size": 8}]}
    rp = {
        "0x1000": {
            "canon_name": "cJSON_Parse",
            "claims": [{"truth_level": "high_confidence",
                        "claim_text": "raises on bad input"}],
            "fields": [],
        },
        "0x2000": {"canon_name": "cJSON_Delete", "claims": [], "fields": []},
        "structs": {"cJSON": [{"offset": 0, "size": 8}]},  # 1 of 2 matched
    }
    judge = StubJudge([True])  # high-confidence claim is correct
    metrics = await compute_metrics(gt, rp, judge=judge)
    report = to_report(metrics)
    assert report["claim_coverage"] == 50.0  # 1 of 2 functions covered
    assert report["type_recovery_accuracy"] == 50.0  # 1 of 2 fields by off+size
    assert report["high_confidence_claims"] == 1
    assert report["false_confidence_rate"] == 0.0  # judge said correct


@pytest.mark.asyncio
async def test_llm_judge_requests_structured_output():
    llm = StubLLM(responses=[json.dumps({"equivalent": True})],
                  structured_model_name="_JudgeYesNo")
    judge = LLMJudge(llm)
    assert await judge.yes_no("same?") is True
    sends = [c for c in llm.calls if c["op"] == "send"]
    assert sends and "same?" in sends[0]["message"]


@pytest.mark.asyncio
async def test_to_report_and_format_round_trip():
    m = Metrics(total_functions=2, exact_names=2, semantic_names=2)
    report = to_report(m)
    assert report["function_name_exact_match"] == 100.0
    from eval.evaluate import format_report
    assert "function name exact match" in format_report(report)
