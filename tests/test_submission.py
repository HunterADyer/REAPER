"""Tests for Deliverable 3.3 — Submission Protocol models + helpers.

Verifies round-trip (model_dump_json / model_validate_json) for every model,
schema validity, truth-level validation, and the parse_response helper.
"""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from reaper.harness.submission import (
    Claim,
    Contradiction,
    CriticOutcome,
    CriticVerdict,
    EvidenceLink,
    FunctionSummary,
    InvestigationResult,
    MergedClaim,
    Rename,
    ResynthesisResult,
    ReviewOutput,
    StructDefinition,
    StructField,
    Submission,
    TaskContextSpec,
    TaskSpec,
    get_schema,
    parse_response,
)

_MODELS = [
    Rename,
    EvidenceLink,
    Claim,
    Submission,
    CriticVerdict,
    TaskContextSpec,
    TaskSpec,
    ReviewOutput,
    InvestigationResult,
    Contradiction,
    MergedClaim,
    StructField,
    StructDefinition,
    CriticOutcome,
    FunctionSummary,
    ResynthesisResult,
]

_VALID_INSTANCES = {
    Rename: {
        "node_id": "0x1400:var_18",
        "llm_name": "parse_config_blob",
        "canon_name": "Parse config blob",
        "justification": "reads from config global",
    },
    EvidenceLink: {
        "address_start": "0x1400",
        "address_end": "0x1430",
        "description": "loads config pointer",
    },
    Claim: {
        "function_address": "0x1400",
        "claim_text": "parses configuration",
        "evidence": [
            {
                "address_start": "0x1400",
                "address_end": "0x1430",
                "description": "why",
            }
        ],
    },
    Submission: {
        "renames": [
            {
                "node_id": "0x1400:var_18",
                "llm_name": "parse_config_blob",
                "canon_name": "Parse config blob",
                "justification": "j",
            }
        ],
        "claims": [],
    },
    CriticVerdict: {
        "truth_level": "mid_confidence",
        "accepted": True,
        "feedback": "good",
    },
    TaskContextSpec: {
        "functions": ["0x1400"],
        "include_callees": True,
        "include_claims": True,
        "include_neighborhood": False,
    },
    TaskSpec: {
        "description": "resolve var_18 type",
        "start_position": "0x1400",
        "goal": "determine struct type",
        "graph_refs": ["0x1400:var_18"],
        "context_spec": {"functions": ["0x1400"], "include_callees": False},
    },
    ReviewOutput: {
        "renames": [
            {
                "node_id": "0x1400:var_18",
                "llm_name": "parse_config_blob",
                "canon_name": "Parse config blob",
                "justification": "j",
            }
        ],
        "claims": [],
        "tasks": [],
    },
    InvestigationResult: {"answer": "it parses config", "claims": [], "subtasks": []},
    Contradiction: {
        "claim_id_a": 1,
        "claim_id_b": 2,
        "explanation": "conflicting",
    },
    MergedClaim: {"keep_id": 1, "remove_ids": [2, 3], "merged_text": "merged"},
    StructField: {
        "offset": 0,
        "name": "next",
        "type_str": "struct cJSON*",
        "size": 8,
        "confidence": "high_confidence",
    },
    StructDefinition: {
        "struct_name": "cJSON",
        "fields": [
            {
                "offset": 0,
                "name": "next",
                "type_str": "struct cJSON*",
                "size": 8,
                "confidence": "high_confidence",
            }
        ],
    },
    CriticOutcome: {"accepted": True, "feedback": "ok"},
    FunctionSummary: {
        "llm_name": "parse_config_blob",
        "canon_name": "Parse config blob",
        "summary": "Parses configuration blobs.",
    },
    ResynthesisResult: {"contradictions": [], "merged_claims": [], "new_tasks": []},
}

def test_every_model_roundtrips():
    for model in _MODELS:
        data = _VALID_INSTANCES[model]
        obj = model.model_validate(data)
        dumped = obj.model_dump_json()
        again = model.model_validate_json(dumped)
        assert again == obj, f"round-trip mismatch for {model.__name__}"


def test_critic_verdict_rejects_bad_truth_level():
    with pytest.raises(ValidationError):
        CriticVerdict(truth_level="banana", accepted=True, feedback="nope")


def test_critic_verdict_rejects_missing_feedback():
    with pytest.raises(ValidationError):
        CriticVerdict(truth_level="speculation", accepted=False)


def test_get_schema_is_valid_json_schema():
    schema = get_schema(Submission)
    assert isinstance(schema, dict)
    assert schema["type"] == "object"
    assert "renames" in schema["properties"]
    assert "claims" in schema["properties"]
    json.dumps(schema)  # must be JSON-serializable


def test_parse_response_bare_json():
    text = json.dumps(
        {
            "renames": [],
            "claims": [
                {
                    "function_address": "0x1400",
                    "claim_text": "parses config",
                    "evidence": [],
                }
            ],
        }
    )
    out = parse_response(Submission, text)
    assert len(out.claims) == 1
    assert out.claims[0].claim_text == "parses config"


def test_parse_response_fenced_json():
    out = parse_response(Submission, "```json\n{\"renames\": [], \"claims\": []}\n```")
    assert out.renames == []


def test_parse_response_surrounding_noise():
    text = (
        "Here is the result:\n"
        + json.dumps(
            {"truth_level": "high_confidence", "accepted": True, "feedback": "ok"}
        )
        + "\nHope that helps."
    )
    out = parse_response(CriticVerdict, text)
    assert out.accepted is True
    assert out.truth_level == "high_confidence"


def test_parse_response_malformed_raises():
    with pytest.raises(ValidationError):
        parse_response(CriticVerdict, "this is not json at all")

