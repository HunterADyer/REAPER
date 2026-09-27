"""End-to-end smoke regression for run.py (Deliverable 8.2).

The real pipeline needs Binary Ninja + Neo4j at runtime; this test drives
``run_pipeline`` through the Phase-3 (harness init) subset against the test
doubles, but with the REAL Tracer, ledger/todo (SQLite) and report-export code,
so we assert on real artifacts (trace events, reaper_output.json) rather than
mocks.

Regression guard for audit finding #1: ``Tracer.log("pipeline_phases_done")``
used to raise ``ValueError`` and crash every live run before the reaper-output
report could be written.
"""

from __future__ import annotations

import json
import types
from pathlib import Path

import pytest

from reaper import run as run_mod


def _config_for(tmp_path, *, data_dir: str, traces_dir: str) -> str:
    # Nested so that config_root (config-file-path ../..) == tmp_path, which
    # lets the relative-path test assert artifacts land under tmp_path.
    run_dir = tmp_path / "run"
    run_dir.mkdir(parents=True, exist_ok=True)
    cfg = run_dir / "cfg.toml"
    cfg.write_text(
        "[neo4j]\n"
        'uri = "bolt://localhost:7687"\nuser = "neo4j"\npassword = "reaper"\n'
        "[vllm]\n"
        'base_url = "http://localhost:8035/v1"\nmodel = "deepseek"\n'
        "[thinking_levels]\n"
        'pass0_type_recovery = "high"\nrecovery_followup = "max"\n'
        'pass1_rename = "minimal"\n'
        'pass2_review = "max"\ncritic = "high"\n'
        'investigation = "high"\nmerge = "max"\n'
        'resynthesis = "max"\nscheduler = "minimal"\n'
        "[limits]\n"
        "max_critic_rejections = 3\nmax_review_retries = 2\n"
        "max_resynthesis_iterations = 5\nmax_investigation_iterations = 200\n"
        "max_type_recovery_rounds = 5\n"
        "shadow_copy_hops = 2\ntask_timeout_seconds = 600\n"
        "max_concurrent_agents = 8\nmax_context_tokens = 32768\n"
        "[paths]\n"
        f'data_dir = "{data_dir}"\n'
        f'traces_dir = "{traces_dir}"\n',
        encoding="utf-8",
    )
    return str(cfg)


def _install_fakes(monkeypatch) -> "RecordingNeo4jDriver":
    """Patch run_pipeline's external dependencies for the full test class."""
    from tests.conftest import RecordingNeo4jDriver

    async def _noop_init_schema(driver):
        return None

    class _FakeExtractor:
        def __init__(self, binary_path, data_dir):
            self.binary_path = binary_path
            self.data_dir = data_dir
            self.bndb_path = f"{data_dir}/target.bndb"
            self.binary_view = None

        def list_functions(self):
            return []

    class _FakeBNDBWriter:
        def __init__(self, extractor):
            self.extractor = extractor

        def save(self) -> str:
            return self.extractor.bndb_path

    class _FakeLLM:
        async def close(self):
            return None

    driver = RecordingNeo4jDriver()
    monkeypatch.setattr(
        run_mod.neo4j, "AsyncGraphDatabase",
        types.SimpleNamespace(driver=lambda *_a, **_k: driver),
    )
    monkeypatch.setattr(run_mod, "init_schema", _noop_init_schema)
    monkeypatch.setattr(run_mod, "HLILExtractor", _FakeExtractor)
    monkeypatch.setattr(run_mod, "BNDBWriter", _FakeBNDBWriter)
    monkeypatch.setattr(run_mod, "ReaperLLMClient", lambda *_a, **_k: _FakeLLM())
    return driver


def _trace_events(traces_root: Path, run_id: str) -> list[str]:
    trace_file = traces_root / run_id / "trace.jsonl"
    assert trace_file.exists(), "real Tracer should have written a trace file"
    return [json.loads(line)["event"]
            for line in trace_file.read_text(encoding="utf-8").strip().splitlines()]


@pytest.mark.asyncio
async def test_run_pipeline_phase3_harness_init_without_binja(tmp_path, monkeypatch):
    """A Phase-3-only run must complete against fakes, logging the phase-done
    and pipeline-complete events through the REAL Tracer without raising, and
    must still write the reaper_output.json report."""
    _install_fakes(monkeypatch)
    config_path = _config_for(tmp_path, data_dir=str(tmp_path / "data"),
                              traces_dir=str(tmp_path / "traces"))
    await run_mod.run_pipeline(str(tmp_path / "binary"), config_path,
                               "smoke_run", ["3"])
    events = _trace_events(tmp_path / "traces", "smoke_run")
    assert "pipeline_phases_done" in events
    assert "pipeline_complete" in events
    assert (tmp_path / "data" / "smoke_run_reaper_output.json").exists()


@pytest.mark.asyncio
async def test_run_pipeline_relative_paths_resolve_against_config_root(  # finding #2
        tmp_path, monkeypatch):
    """Relative data/traces paths must resolve against the config's repo root
    (not the CWD) and the directories must be pre-created (audit finding #2:
    'reaper/data' used to be created as a literal wrong subdir)."""
    _install_fakes(monkeypatch)
    # Config is at tmp_path/run/cfg.toml -> config_root == tmp_path; relative
    # path entries must land under tmp_path, regardless of the CWD.
    config_path = _config_for(tmp_path, data_dir="data", traces_dir="data/traces")
    await run_mod.run_pipeline(str(tmp_path / "binary"), config_path,
                               "rel_run", ["3"])
    # Relative entries resolve against config_root (tmp_path): traces land under
    # <config_root>/data/traces, NOT under <config_root>/traces or the CWD.
    events = _trace_events(tmp_path / "data" / "traces", "rel_run")
    assert "pipeline_phases_done" in events
    assert (tmp_path / "data" / "rel_run_reaper_output.json").exists()
    assert not (tmp_path / "run" / "reaper" / "data").exists(), (
        "old literal 'reaper/data' subdir must not be created"
    )
