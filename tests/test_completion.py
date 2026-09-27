"""Tests for Deliverable 7.5 — ResynthesisLoop + is_re_complete.

Covers: convergence (a resynthesis round that creates a task triggers the
investigation loop, and a stable round stops), merged-claim application, and
the is_re_complete gate (claims on ALL functions AND empty TODO).
"""

from __future__ import annotations

import pytest

from reaper.harness.completion import ResynthesisLoop, is_re_complete
from reaper.harness.submission import (
    MergedClaim,
    ResynthesisResult,
    TaskContextSpec,
    TaskSpec,
)

from conftest import RecordingNeo4jDriver
from fake_harness import RecordingTracer


class StubResynth:
    def __init__(self, results):
        self.results = list(results)
        self.calls = []

    async def run(self, context):
        self.calls.append(context)
        return self.results.pop(0)


class StubInvLoop:
    def __init__(self):
        self.runs = 0

    async def run(self):
        self.runs += 1
        return True


class StubTodo:
    def __init__(self, is_empty=True):
        self.empty = is_empty
        self.created = []

    async def is_empty(self):
        return self.empty

    async def create_task(self, **kwargs):
        self.created.append(kwargs)
        return len(self.created)


class StubLedger:
    def __init__(self, has_claims=True):
        self._has_claims = has_claims
        self.updated = []
        self.deleted = []
        self.registered = 0

    async def all_functions_have_claims(self):
        return self._has_claims

    async def update_claim_text(self, claim_id, new_text):
        self.updated.append((claim_id, new_text))

    async def delete_claim(self, claim_id):
        self.deleted.append(claim_id)

    async def register_functions_from_graph(self):
        self.registered += 1


class FakeContext:
    async def for_subgraph(self, function_addresses, include_claims=True):
        return "SUBGRAPH: " + ",".join(function_addresses)


def _driver_with_group():
    def respond(query, params):
        if "f.scc_id IS NOT NULL" in query:
            return [{"sid": "scc:0x1000", "addrs": ["0x1000", "0x2000"]}]
        return []

    return RecordingNeo4jDriver(respond)


def _task_spec(desc):
    return TaskSpec(description=desc, start_position="0x1000", goal="answer",
                    graph_refs=["0x1000"],
                    context_spec=TaskContextSpec(functions=["0x1000"]))


@pytest.mark.asyncio
async def test_loop_creates_tasks_runs_investigation_then_converges():
    inv = StubInvLoop()
    todo = StubTodo(is_empty=True)
    ledger = StubLedger(has_claims=True)
    resynth = StubResynth([
        ResynthesisResult(new_tasks=[_task_spec("resolve null contract")]),
        ResynthesisResult(),
    ])
    loop = ResynthesisLoop(resynth, inv, todo, ledger, _driver_with_group(),
                           FakeContext(), RecordingTracer(),
                           {"limits": {"max_resynthesis_iterations": 5}})

    assert await loop.run() is True
    assert len(todo.created) == 1
    assert "null contract" in todo.created[0]["description"]
    assert inv.runs == 1          # investigation ran for the discovered task
    assert ledger.registered == 1
    assert resynth.calls  # context was assembled per group


@pytest.mark.asyncio
async def test_loop_applies_merged_claims_and_stops_when_stable():
    inv = StubInvLoop()
    todo = StubTodo(is_empty=True)
    ledger = StubLedger(has_claims=True)
    resynth = StubResynth([
        ResynthesisResult(merged_claims=[
            MergedClaim(keep_id=1, remove_ids=[2, 3], merged_text="consolidated"),
        ]),
    ])
    loop = ResynthesisLoop(resynth, inv, todo, ledger, _driver_with_group(),
                           FakeContext(), RecordingTracer(),
                           {"limits": {"max_resynthesis_iterations": 5}})

    assert await loop.run() is True
    assert ledger.updated == [(1, "consolidated")]
    assert ledger.deleted == [2, 3]
    assert inv.runs == 0  # no new tasks → stable, no investigation round


@pytest.mark.asyncio
async def test_is_re_complete_requires_claims_on_all_functions_and_empty_todo():
    todo = StubTodo(is_empty=False)
    ledger = StubLedger(has_claims=False)
    assert await is_re_complete(ledger, todo) is False

    todo.empty = True
    assert await is_re_complete(ledger, todo) is False  # claims still missing

    ledger._has_claims = True
    assert await is_re_complete(ledger, todo) is True
