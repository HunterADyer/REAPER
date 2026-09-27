"""Tests for Deliverable 1.4 — tracer (JSONL trace logging).

Verifies concurrent logging produces a clean, parseable JSONL file with no
interleaved/corrupted lines. Uses the real Tracer with tmp_path as log dir.
"""

from __future__ import annotations

import asyncio
import json
import logging

import pytest

from reaper.harness.tracer import Tracer


@pytest.mark.asyncio
async def test_log_and_readback(tmp_path):
    tracer = Tracer(str(tmp_path))
    assert (tmp_path / "trace.jsonl").exists()
    try:
        events = [
            ("llm_request", "sess_1", {"message": f"msg {i}", "thinking_level": "low"})
            for i in range(10)
        ]
        await asyncio.gather(
            *(tracer.log(et, sid, data) for et, sid, data in events)
        )
        lines = (tmp_path / "trace.jsonl").read_text().strip().splitlines()
        assert len(lines) == 10
        parsed = [json.loads(line) for line in lines]
        # every record is well-formed with a timestamp
        for rec, (et, sid, data) in zip(parsed, events):
            assert rec["event"] == et
            assert rec["session"] == sid
            assert rec["data"] == data
            assert rec["ts"].startswith("20")  # ISO timestamp year prefix
    finally:
        await tracer.close()


@pytest.mark.asyncio
async def test_unknown_event_type_warned_and_dropped(tmp_path, caplog):
    """Unknown event types must never crash a live run: warn + drop."""
    tracer = Tracer(str(tmp_path))
    try:
        with caplog.at_level(logging.WARNING, logger="reaper.harness.tracer"):
            await tracer.log("not_an_event", "s", {})  # must NOT raise
        assert (tmp_path / "trace.jsonl").read_text().strip() == ""
        assert any("not_an_event" in rec.message for rec in caplog.records)
    finally:
        await tracer.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("event_type", [
    # run orchestrator
    "pipeline_complete", "pipeline_phases_done",
    # Pass 1 / Pass 2 dispatchers
    "pass1_no_functions", "pass1_scc_done", "pass1_level_done",
    "pass2_no_functions", "pass2_level_done", "pass2_review_retry",
    "pass2_function_done",
    # scheduler / resynthesis / investigation loops
    "scheduler_reviewed", "scheduler_stale_reset", "scheduler_assigned",
    "resynthesis_iteration", "resynthesis_agent", "completion_coverage",
    "investigation_complete", "investigation_stuck",
    "investigation_iteration_cap", "investigation_agent",
    # Pass 1 rename / Pass 2 review / critic agents
    "review_agent", "rename_agent",
    "claim_accepted", "claim_force_accepted", "claim_rejected",
])
async def test_all_emitted_event_types_are_accepted(tmp_path, event_type):
    """Regression guard (audit finding #1): every event type currently emitted
    across run.py, the dispatchers/loops and the agents must be accepted — they
    previously raised ValueError and crashed live runs (e.g. 'pipeline_phases_done')."""
    tracer = Tracer(str(tmp_path))
    try:
        await tracer.log(event_type, "session", {"k": "v"})
    finally:
        await tracer.close()
    lines = (tmp_path / "trace.jsonl").read_text().strip().splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["event"] == event_type


@pytest.mark.asyncio
async def test_session_id_optional(tmp_path):
    tracer = Tracer(str(tmp_path))
    try:
        await tracer.log("rename", "", {"node_id": "0x1400:var_18"})
        line = (tmp_path / "trace.jsonl").read_text().strip()
        assert json.loads(line)["event"] == "rename"
    finally:
        await tracer.close()
