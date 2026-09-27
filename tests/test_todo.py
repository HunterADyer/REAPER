"""Tests for Deliverable 1.6 — TodoLedger (SQLite task queue).

Verifies dependency-gated readiness, the no-`rejected`-status reject flow, and
task lifecycle. Uses real TodoLedger against tmp_path SQLite files.
"""

from __future__ import annotations

import pytest

from reaper.harness.todo import TodoLedger


@pytest.mark.asyncio
async def test_dependencies_gate_readiness(tmp_path):
    todo = TodoLedger(str(tmp_path / "todo.db"))
    await todo.init()
    try:
        t1 = await todo.create_task("dep 1", {}, "0x100", "g1", [])
        t2 = await todo.create_task("dep 2", {}, "0x200", "g2", [])
        t3 = await todo.create_task("dep on 1+2", {}, "0x300", "g3", [], depends_on=[t1, t2])

        ready_ids = {t["id"] for t in await todo.get_ready_tasks()}
        assert {t1, t2} <= ready_ids
        assert t3 not in ready_ids  # blocked until 1 and 2 complete

        # complete one dep -> still blocked
        await todo.complete_task(t1)
        ready_ids = {t["id"] for t in await todo.get_ready_tasks()}
        assert t3 not in ready_ids

        # complete the other -> t3 becomes ready
        await todo.complete_task(t2)
        ready_ids = {t["id"] for t in await todo.get_ready_tasks()}
        assert t3 in ready_ids
    finally:
        await todo.close()


@pytest.mark.asyncio
async def test_reject_returns_to_pending_with_feedback(tmp_path):
    todo = TodoLedger(str(tmp_path / "todo.db"))
    await todo.init()
    try:
        t = await todo.create_task("spec fn", {}, "0x400", "purpose", ["0x400:ref"])
        await todo.assign_task(t, "session_7")
        task = await todo.get_task(t)
        assert task["status"] == "in_progress"
        assert task["assigned_to"] == "session_7"

        count = await todo.reject_task(t, "no evidence, redo")
        assert count == 1
        task = await todo.get_task(t)
        assert task["status"] == "pending"
        assert task["critic_feedback"] == "no evidence, redo"
        assert task["rejection_count"] == 1
        # re-queued task appears in ready again
        assert t in {x["id"] for x in await todo.get_ready_tasks()}

        # second rejection increments the counter
        count = await todo.reject_task(t, "still weak")
        assert count == 2
    finally:
        await todo.close()


@pytest.mark.asyncio
async def test_completion_and_empty(tmp_path):
    todo = TodoLedger(str(tmp_path / "todo.db"))
    await todo.init()
    try:
        assert await todo.is_empty() is True
        t = await todo.create_task("task", {"functions": ["0x100"]}, "0x100", "goal", ["r1"])
        assert await todo.is_empty() is False
        task = await todo.get_task(t)
        assert task["context_spec"] == {"functions": ["0x100"]}
        assert task["graph_refs"] == ["r1"]
        assert task["start_position"] == "0x100"
        assert task["goal"] == "goal"
        await todo.complete_task(t)
        assert await todo.is_empty() is True
        assert await todo.has_in_progress() is False
    finally:
        await todo.close()


@pytest.mark.asyncio
async def test_assign_sets_in_progress_and_delete(tmp_path):
    todo = TodoLedger(str(tmp_path / "todo.db"))
    await todo.init()
    try:
        t = await todo.create_task("x", {}, "0x10", "g", [])
        await todo.assign_task(t, "session_9")
        assert await todo.has_in_progress() is True
        assert {x["id"] for x in await todo.get_ready_tasks()} == set()
        await todo.delete_task(t)
        assert await todo.get_task(t) is None
    finally:
        await todo.close()


@pytest.mark.asyncio
async def test_get_task_missing_returns_none(tmp_path):
    todo = TodoLedger(str(tmp_path / "todo.db"))
    await todo.init()
    try:
        assert await todo.get_task(424242) is None
    finally:
        await todo.close()


@pytest.mark.asyncio
async def test_get_pending_tasks_returns_only_pending(tmp_path):
    todo = TodoLedger(str(tmp_path / "todo.db"))
    await todo.init()
    try:
        t1 = await todo.create_task("a", {}, "0x1", "g", [])
        t2 = await todo.create_task("b", {}, "0x2", "g", [])
        await todo.assign_task(t1, "sess")
        pending = {t["id"] for t in await todo.get_pending_tasks()}
        assert pending == {t2}
    finally:
        await todo.close()


@pytest.mark.asyncio
async def test_reset_stale_in_progress_resets_old_tasks(tmp_path):
    from datetime import datetime, timezone, timedelta
    todo = TodoLedger(str(tmp_path / "todo.db"))
    await todo.init()
    try:
        stale = await todo.create_task("old", {}, "0x1", "g", [])
        fresh = await todo.create_task("new", {}, "0x2", "g", [])
        await todo.assign_task(stale, "s1")
        await todo.assign_task(fresh, "s2")
        # Backdate the stale task 10 minutes so timeout=5s flags it.
        old_ts = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
        await todo._db.execute(
            "UPDATE tasks SET created_at = ? WHERE id = ?", (old_ts, stale)
        )
        await todo._db.commit()

        count = await todo.reset_stale_in_progress(5)
        assert count == 1
        stale_task = await todo.get_task(stale)
        fresh_task = await todo.get_task(fresh)
        assert stale_task["status"] == "pending"
        assert stale_task["assigned_to"] is None
        assert fresh_task["status"] == "in_progress"  # not stale
    finally:
        await todo.close()

