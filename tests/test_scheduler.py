"""Tests for Deliverable 7.1 — Scheduler (reaper/harness/scheduler.py).

Covers review_pending_tasks (merge + decompose recommendation application)
and run_once (assign ready tasks without executing agents). Uses a stub TODO
ledger and the recording StubLLM.
"""

from __future__ import annotations

import json

import pytest

from reaper.harness.scheduler import Scheduler

from fake_harness import RecordingTracer, StubLLM


class StubTodo:
    def __init__(self, pending=None, ready=None):
        self.pending = list(pending or [])
        self.ready = list(ready or [])
        self.created = []
        self.deleted = []
        self.assigned = []
        self.stale_calls = 0
        self._next = 100

    async def get_pending_tasks(self):
        return list(self.pending)

    async def get_ready_tasks(self):
        return list(self.ready)

    async def create_task(self, description, context_spec, start_position,
                          goal, graph_refs=None, depends_on=None):
        tid = self._next
        self._next += 1
        self.created.append({"id": tid, "description": description,
                             "start_position": start_position, "goal": goal,
                             "graph_refs": graph_refs, "context_spec": context_spec})
        return tid

    async def delete_task(self, task_id):
        self.deleted.append(task_id)

    async def assign_task(self, task_id, session_id):
        self.assigned.append(task_id)

    async def reset_stale_in_progress(self, timeout_seconds):
        self.stale_calls += 1
        return 0


def _task(task_id, description="spec function", start="0x1000", goal="purpose"):
    return {"id": task_id, "description": description, "status": "pending",
            "start_position": start, "goal": goal, "graph_refs": []}


def _scheduler(todo, llm):
    return Scheduler(
        llm, todo, None, RecordingTracer(),
        {"limits": {"task_timeout_seconds": 7},
         "thinking_levels": {"scheduler": "low"}},
    )


@pytest.mark.asyncio
async def test_review_pending_tasks_merges_overlapping():
    todo = StubTodo(pending=[_task(1, "what does parse do?"),
                             _task(2, "purpose of parse function")])
    llm = StubLLM(responses=[json.dumps({
        "merges": [{
            "task_ids": [1, 2], "description": "determine what parse does",
            "start_position": "0x1000", "goal": "characterize parse",
            "graph_refs": [], "context_spec": {},
        }],
        "decomposes": [], "keep": [],
    })])
    scheduler = _scheduler(todo, llm)

    processed = await scheduler.review_pending_tasks()

    assert processed == 1
    assert len(todo.created) == 1
    assert todo.created[0]["description"] == "determine what parse does"
    assert todo.deleted == [1, 2]


@pytest.mark.asyncio
async def test_review_pending_tasks_decomposes_vague_task():
    todo = StubTodo(pending=[_task(5, "figure out parse and its null contract",
                                   start="0x2000")])
    llm = StubLLM(responses=[json.dumps({
        "merges": [],
        "decomposes": [
            {"source_task_id": 5, "description": "what does parse do",
             "start_position": "0x2000", "goal": "characterize parse"},
            {"source_task_id": 5, "description": "can arg1 be NULL",
             "start_position": "0x2000", "goal": "determine null contract"},
        ],
        "keep": [],
    })])
    scheduler = _scheduler(todo, llm)

    await scheduler.review_pending_tasks()

    assert len(todo.created) == 2
    assert todo.deleted == [5]  # source deleted exactly once


@pytest.mark.asyncio
async def test_run_once_assigns_ready_tasks_without_executing():
    todo = StubTodo(ready=[_task(7, "independent task")])
    scheduler = _scheduler(todo, StubLLM())

    assigned = await scheduler.run_once()

    assert [t["id"] for t in assigned] == [7]
    assert todo.assigned == [7]
    assert todo.stale_calls == 1  # stale check ran first
