"""Tests for Deliverable 1.4 — tracer (JSONL trace logging).

Verifies concurrent logging produces a clean, parseable JSONL file with no
interleaved/corrupted lines. Uses the real Tracer with tmp_path as log dir.
"""

from __future__ import annotations

import asyncio
import json

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
async def test_unsupported_event_type_rejected(tmp_path):
    tracer = Tracer(str(tmp_path))
    try:
        with pytest.raises(ValueError):
            await tracer.log("not_an_event", "s", {})
    finally:
        await tracer.close()


@pytest.mark.asyncio
async def test_session_id_optional(tmp_path):
    tracer = Tracer(str(tmp_path))
    try:
        await tracer.log("rename", "", {"node_id": "0x1400:var_18"})
        line = (tmp_path / "trace.jsonl").read_text().strip()
        assert json.loads(line)["event"] == "rename"
    finally:
        await tracer.close()
