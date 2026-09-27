"""Tests for Deliverable 7.3 — InvestigationLoop (reaper/harness/investigation_loop.py).

Scenario coverage: (A) a task answered + claim accepted completes and the loop
drains; (B) a stuck loop (nothing ready, nothing in_progress, non-empty
ledger) terminates with False; (C) a task that spawns a subtask is requeued
and, after its subtask completes on a later pass, the loop drains.
"""

from __future__ import annotations

import json

import pytest

from reaper.harness.investigation_loop import InvestigationLoop
from reaper.harness.submission import (
    InvestigationResult, TaskContextSpec, TaskSpec,
)

from fake_harness import RecordingTracer, StubLLM


class StubTodo:
    def __init__(self):
        self.by_id = {}
        self._next = 100
        self.completed = []
        self.requeued = []
        self.created = []
        self.deps_added = []

    def put(self, task):
        self.by_id[task["id"]] = task

    async def is_empty(self):
        return all(t.get("status") == "completed" for t in self.by_id.values())

    async def has_in_progress(self):
        return any(t.get("status") == "in_progress" for t in self.by_id.values())

    async def complete_task(self, task_id):
        self.by_id[task_id]["status"] = "completed"
        self.completed.append(task_id)

    async def requeue_task(self, task_id, feedback=""):
        self.by_id[task_id]["status"] = "pending"
        self.requeued.append(task_id)

    async def create_task(self, description, context_spec, start_position,
                          goal, graph_refs=None, depends_on=None):
        tid = self._next
        self._next += 1
        self.by_id[tid] = {"id": tid, "description": description,
                           "start_position": start_position, "goal": goal,
                           "graph_refs": graph_refs or [],
                           "context_spec": context_spec, "status": "pending"}
        self.created.append(tid)
        return tid

    async def add_dependencies(self, task_id, dep_ids):
        self.deps_added.append((task_id, list(dep_ids)))


class StubScheduler:
    """run_once selects pending tasks and marks them in_progress, like the
    real Scheduler — so requeued/created tasks are picked up on later passes.
    ``never_ready`` forces run_once to return [] (simulates a pending task
    whose dependencies can never be satisfied)."""

    def __init__(self, todo, never_ready=False):
        self.todo = todo
        self.reviews = 0
        self.never_ready = never_ready

    async def review_pending_tasks(self):
        self.reviews += 1

    async def run_once(self):
        if self.never_ready:
            return []
        out = [t for t in self.todo.by_id.values() if t.get("status") == "pending"]
        for t in out:
            t["status"] = "in_progress"
        return out


class StubInvAgent:
    def __init__(self, results):
        self.results = list(results)

    async def run(self, task):
        if not self.results:
            return InvestigationResult(answer="idle", claims=[], subtasks=[])
        return self.results.pop(0)


class FakeContext:
    async def for_evidence(self, evidence_links):
        return "EVIDENCE"

    async def for_function(self, func_addr):
        return f"SIG {func_addr}"


class StubLedger:
    def __init__(self):
        self.claims = []
        self.levels = []

    async def add_claim(self, function_address, claim_text, submitted_by, evidence):
        claim_id = len(self.claims) + 1
        self.claims.append({"id": claim_id, "function_address": function_address,
                            "claim_text": claim_text})
        return claim_id

    async def set_truth_level(self, claim_id, truth_level, reviewed_by):
        self.levels.append((claim_id, truth_level))


def _claim_diff():
    return {
        "answer": "arg2 may be NULL",
        "claims": [{
            "function_address": "0x1000",
            "claim_text": "arg2 may be NULL",
            "evidence": [{"address_start": "0x1020", "address_end": "0x1040",
                          "description": "NULL guard"}],
        }],
        "subtasks": [],
    }


def _task(task_id):
    return {"id": task_id, "description": "is arg2 nullable?",
            "start_position": "0x1000", "goal": "answer", "graph_refs": [],
            "context_spec": {}, "status": "pending"}


def _loop(llm, todo, inv_results, stuck=False, max_iterations=None):
    ledger = StubLedger()
    tracer = RecordingTracer()
    limits = {"max_concurrent_agents": 4, "max_critic_rejections": 3}
    if max_iterations is not None:
        limits["max_investigation_iterations"] = max_iterations
    config = {"limits": limits,
              "thinking_levels": {"critic": "high"}}
    loop = InvestigationLoop(
        StubScheduler(todo, never_ready=stuck), StubInvAgent(inv_results),
        llm, FakeContext(), todo, ledger, tracer, config,
    )
    return loop, ledger, tracer


@pytest.mark.asyncio
async def test_completes_when_task_answer_accepted():
    todo = StubTodo()
    todo.put(_task(1))
    llm = StubLLM(responses=[json.dumps({"truth_level": "mid_confidence",
                                         "accepted": True, "feedback": "ok"})])
    loop, ledger, tracer = _loop(llm, todo, [InvestigationResult(**_claim_diff())])

    assert await loop.run() is True
    assert todo.completed == [1]
    assert len(ledger.claims) == 1
    assert ledger.levels and ledger.levels[0][1] == "mid_confidence"


@pytest.mark.asyncio
async def test_requeue_on_subtasks_then_drain():
    todo = StubTodo()
    todo.put(_task(1))
    first = InvestigationResult(
        answer="needs callee", claims=[], subtasks=[TaskSpec(
            description="what does callee do", start_position="0x2000",
            goal="characterize callee", graph_refs=["0x2000"],
            context_spec=TaskContextSpec(functions=["0x2000"]))],
    )
    completed = InvestigationResult(answer="callee copies buffer", claims=[], subtasks=[])
    llm = StubLLM()  # no claims → no critic calls
    loop, ledger, tracer = _loop(llm, todo, [first, completed, completed])

    assert await loop.run() is True
    assert todo.requeued == [1]      # task1 requeued after spawning a subtask
    assert len(todo.created) == 1    # the subtask
    assert set(todo.completed) == {1, 100}  # task1 + subtask eventually completed


@pytest.mark.asyncio
async def test_stuck_when_nothing_ready_and_nothing_in_progress():
    todo = StubTodo()
    todo.put(_task(1))  # pending forever (deps never satisfiable) → stuck
    llm = StubLLM()
    loop, ledger, tracer = _loop(llm, todo, [], stuck=True)
    assert await loop.run() is False
    assert any(e["event_type"] == "investigation_stuck" for e in tracer.events)


@pytest.mark.asyncio
async def test_loop_is_bounded_by_iteration_cap():
    """A model that keeps spawning subtasks must not livelock the pipeline —
    the iteration cap returns with a clear signal instead."""
    class AlwaysSubtasks(StubInvAgent):
        def __init__(self):
            super().__init__([])

        async def run(self, task):
            return InvestigationResult(
                answer="deeper...", claims=[],
                subtasks=[TaskSpec(description="another sub", start_position="0x2000",
                                   goal="dig deeper")],
            )

    todo = StubTodo()
    todo.put(_task(1))
    loop, ledger, tracer = _loop(
        StubLLM(), todo, [], max_iterations=2,
    )
    loop.inv_agent = AlwaysSubtasks()
    assert await loop.run() is False
    # cap reached (not a stuck signal, but bounded) and reported
    assert any(e["event_type"] == "investigation_iteration_cap" for e in tracer.events)
    assert len(todo.requeued) >= 2        # the parent was retried at least twice
    assert not await loop.todo.is_empty()  # never drained within the budget


@pytest.mark.asyncio
async def test_parent_is_dependent_on_subtasks():
    """When a task spawns subtasks it is NOT retried until they complete — the
    parent is wired as a dependant of its own subtasks (no deadlock since the
    subtasks never depend on the parent)."""
    todo = StubTodo()
    todo.put(_task(1))
    first = InvestigationResult(
        answer="needs callee", claims=[], subtasks=[TaskSpec(
            description="what does callee do", start_position="0x2000",
            goal="characterize callee")],
    )
    completed = InvestigationResult(answer="done", claims=[], subtasks=[])
    llm = StubLLM()
    loop, ledger, tracer = _loop(llm, todo, [first, completed, completed])

    assert await loop.run() is True
    assert todo.deps_added == [(1, [100])]  # parent 1 now waits on subtask 100
