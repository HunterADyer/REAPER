"""Offline heavy-test gate — runs every per-mechanism heavy case.

These are the deterministic, no-network regression cases. Each one drives the
REAL mechanism (CriticEvaluator, RenameVariableAgent, ReviewAgent,
TypeRecoveryAgent, ResynthesisAgent, Scheduler, InvestigationLoop) against a
seeded harness with a scripted StubLLM, and must fully pass. They deliberately
mirror eval/heavy.runner's Code path so the live mode shares the same code.

These run in CI as part of the Tier-1 gate (no Binja/Neo4j/vLLM needed).
"""

from __future__ import annotations

import asyncio

import pytest

from reaper.eval.heavy import runner


@pytest.mark.parametrize(
    "case_id",
    [cid for cid, _, _ in runner.list_cases()],
)
def test_heavy_case_offline(case_id: str):
    report = asyncio.run(runner.run_case(case_id, mode="offline"))
    summaries = "; ".join(
        f"{'OK' if c['passed'] else 'FAIL'}: {c['message']}"
        for c in report.get("checks", [])
    )
    assert report.get("passed", False), (
        f"heavy case {case_id} (offline) failed:\n{summaries}")


def test_heavy_registry_is_populated():
    cases = runner.list_cases()
    required = {"critic_loop", "renaming", "claim_labelling", "type_recovery",
                "logic_conclusion", "resynthesis", "scheduler", "investigation"}
    assert required <= {c for c, _, _ in cases}, (
        f"missing heavy cases; got {[c for c, _, _ in cases]}")


def test_heavy_unknown_case_raises():
    with pytest.raises(KeyError, match="unknown heavy case"):
        runner.get_case("nope")
