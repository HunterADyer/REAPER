"""Tests for Deliverable 8.4 — the REAPER output exporter.

Validates that the exporter serializes the pipeline's final state (Neo4j names,
ledger claims + evidence, persisted struct definitions, Binja fallbacks) into
the exact JSON shape ``eval/evaluate.py --reaper-output`` consumes, and that
it degrades gracefully to Binja-only data when the graph has no rows.
"""

from __future__ import annotations

import json

import pytest

from reaper.harness.ledger import Ledger
from reaper.harness.submission import StructDefinition, StructField
from reaper.tools.export_report import (
    extract_reaper_output,
    write_reaper_output,
)

from conftest import RecordingNeo4jDriver, FakeNeo4jDriver
from fake_binja import FakeExtractor, FakeFunction
from fake_harness import ScriptedExtractor


def _driver(func_name="Parse JSON Config", func_llm="parse_json_config",
            arg=None, var=None):
    arg = arg or {"id": "0x1000:arg0", "ordinal": 0, "llm_name": "raw_value",
                  "canon_name": "RawValue", "type": "char*", "source": None}
    var = var or {"id": "0x1000:var_18", "ordinal": 0, "llm_name": "cursor",
                  "canon_name": "Cursor", "type": "cJSON*", "source": "stack"}

    def respond(query, params):
        if "RETURN f.address AS address" in query and "f.llm_name AS llm_name" in query:
            return [{"address": "0x1000", "llm_name": func_llm,
                     "canon_name": func_name, "name": "sub_1000", "pinned": False}]
        if ":CONTAINS]->(v:Argument" in query:
            return [arg]
        if ":CONTAINS]->(v:Variable" in query:
            return [var]
        return []

    return RecordingNeo4jDriver(respond)


def _extractor():
    return FakeExtractor.with_functions([FakeFunction.simple(0x1000, "sub_1000")])


@pytest.mark.asyncio
async def test_export_full_payload(tmp_path):
    driver = _driver()
    ledger = Ledger(str(tmp_path / "ledger.db"), driver)
    await ledger.init()
    try:
        cid = await ledger.add_claim(
            "0x1000", "parses JSON config blobs", "review_agent",
            [{"address_start": "0x1020", "address_end": "0x1040",
              "description": "NULL-guard before deref"}],
        )
        await ledger.set_truth_level(cid, "high_confidence", "critic_1")
        await ledger.add_claim("0x1000", "unreviewed leftover", "review_agent", [])
        sd = StructDefinition(struct_name="cJSON", fields=[
            StructField(offset=0, name="next", type_str="struct cJSON*", size=8,
                        confidence="high_confidence"),
        ])
        await ledger.record_struct(sd)

        out = await extract_reaper_output(driver, ledger, _extractor())

        assert "0x1000" in out
        rec = out["0x1000"]
        assert rec["canon_name"] == "Parse JSON Config"
        assert rec["llm_name"] == "parse_json_config"
        # parameters/variables come from Neo4j with rename info + ordinal order
        assert rec["parameters"] == [{
            "index": 0, "name": None, "llm_name": "raw_value",
            "canon_name": "RawValue", "type": "char*",
        }]
        assert rec["variables"] == [{
            "name": None, "llm_name": "cursor", "canon_name": "Cursor",
            "type": "cJSON*", "source": "stack",
        }]
        # every claim round-trips with its evidence; NULL claims included here
        # (the runner sweeps them before export in production)
        assert len(rec["claims"]) == 2
        by_level = {c["truth_level"]: c for c in rec["claims"]}
        assert "high_confidence" in by_level
        assert by_level["high_confidence"]["evidence"] == [{
            "address_start": "0x1020", "address_end": "0x1040",
            "description": "NULL-guard before deref",
        }]
        assert by_level.get(None) is not None
        # structs land at the top level for metric 5
        assert out["structs"]["cJSON"] == [{
            "offset": 0, "name": "next", "type_str": "struct cJSON*",
            "size": 8, "confidence": "high_confidence",
        }]
    finally:
        await ledger.close()


@pytest.mark.asyncio
async def test_export_falls_back_to_binja_without_graph_rows(tmp_path):
    # No variable/argument rows → exporter must degrade to the Binja names.
    driver = RecordingNeo4jDriver()  # returns [] for everything
    ledger = Ledger(str(tmp_path / "ledger.db"), FakeNeo4jDriver(["0x1000"]))
    await ledger.init()
    try:
        extractor = ScriptedExtractor(
            params={"0x1000": [{"index": 0, "name": "arg1", "type": "char*"}]},
            variables={"0x1000": [{"name": "var_18", "type": "int64_t"}]},
        )
        extractor.bv.functions.append(FakeFunction.simple(0x1000, "sub_1000"))
        out = await extract_reaper_output(driver, ledger, extractor)
        rec = out["0x1000"]
        # no Neo4j name → falls back to the Binja name
        assert rec["canon_name"] == "sub_1000"
        assert rec["parameters"] == [{
            "index": 0, "name": "arg1", "llm_name": None, "canon_name": None,
            "type": "char*",
        }]
        assert rec["variables"] == [{
            "name": "var_18", "llm_name": None, "canon_name": None,
            "type": "int64_t", "source": None,
        }]
        assert out["structs"] == {}
    finally:
        await ledger.close()


def test_write_reaper_output_round_trip(tmp_path):
    payload = {"0x1000": {"canon_name": "Parse", "claims": []}, "structs": {}}
    path = tmp_path / "nested" / "reaper_output.json"
    write_reaper_output(payload, str(path))
    with open(path, encoding="utf-8") as fh:
        assert json.load(fh) == payload


@pytest.mark.asyncio
async def test_reaper_output_feeds_evaluator_directly(tmp_path):
    """End-to-end: exporter output is directly consumable by eval/evaluate.py."""
    from eval.evaluate import compute_metrics

    driver = _driver()
    ledger = Ledger(str(tmp_path / "ledger.db"), driver)
    await ledger.init()
    try:
        cid = await ledger.add_claim("0x1000", "parses config", "review_agent", [])
        await ledger.set_truth_level(cid, "mid_confidence", "critic_1")
        out = await extract_reaper_output(driver, ledger, _extractor())

        ground_truth = {
            "0x1000": {"function_name": "Parse JSON Config",
                       "parameters": [{"name": "value", "type": "char *"}],
                       "local_variables": []},
        }
        metrics = await compute_metrics(ground_truth, out, judge=None)
        assert metrics.total_functions == 1
        assert metrics.exact_names == 1          # canon_name matches GT
        assert metrics.covered_claim_functions == 1  # mid+ claim present
    finally:
        await ledger.close()
