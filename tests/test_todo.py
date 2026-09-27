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
        # Backdate the stale task's ASSIGNMENT clock 10 minutes so timeout=5s
        # flags it. created_at is untouchable — staleness is measured from
        # when the agent started working (started_at), not queue insertion.
        old_ts = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
        await todo._db.execute(
            "UPDATE tasks SET started_at = ? WHERE id = ?", (old_ts, stale)
        )
        await todo._db.commit()

        count = await todo.reset_stale_in_progress(5)
        assert count == 1
        stale_task = await todo.get_task(stale)
        fresh_task = await todo.get_task(fresh)
        assert stale_task["status"] == "pending"
        assert stale_task["assigned_to"] is None
        assert stale_task["started_at"] is None
        assert fresh_task["status"] == "in_progress"  # not stale
    finally:
        await todo.close()


@pytest.mark.asyncio
async def test_stale_clock_is_assignment_not_creation_time(tmp_path):
    from datetime import datetime, timezone, timedelta
    todo = TodoLedger(str(tmp_path / "todo.db"))
    await todo.init()
    try:
        t = await todo.create_task("queued long ago", {}, "0x1", "g", [])
        # Backdate creation by 10 minutes, but assign it JUST now.
        old_ts = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
        await todo._db.execute(
            "UPDATE tasks SET created_at = ? WHERE id = ?", (old_ts, t)
        )
        await todo._db.commit()
        await todo.assign_task(t, "s1")
        # Long-queued-but-recently-started work must NOT be reset as stale.
        assert await todo.reset_stale_in_progress(5) == 0
        task = await todo.get_task(t)
        assert task["status"] == "in_progress"
    finally:
        await todo.close()


@pytest.mark.asyncio
async def test_parent_waits_for_subtasks_and_deleted_dep_unblocks(tmp_path):
    todo = TodoLedger(str(tmp_path / "todo.db"))
    await todo.init()
    try:
        parent = await todo.create_task("parent", {}, "0x1", "g", [])
        sub1 = await todo.create_task("sub1", {}, "0x2", "g", [])
        sub2 = await todo.create_task("sub2", {}, "0x3", "g", [])
        await todo.add_dependencies(parent, [sub1, sub2])

        ready_ids = {t["id"] for t in await todo.get_ready_tasks()}
        assert sub1 in ready_ids and sub2 in ready_ids
        assert parent not in ready_ids  # waits on its subtasks

        # one subtask completes → parent still blocked on the other
        await todo.complete_task(sub1)
        assert parent not in {t["id"] for t in await todo.get_ready_tasks()}

        # subtask deleted by the scheduler → missing dep counts as satisfied
        await todo.delete_task(sub2)
        assert parent in {t["id"] for t in await todo.get_ready_tasks()}
    finally:
        await todo.close()


@pytest.mark.asyncio
async def test_delete_task_removes_dependency_rows_both_directions(tmp_path):
    todo = TodoLedger(str(tmp_path / "todo.db"))
    await todo.init()
    try:
        a = await todo.create_task("a", {}, "0x1", "g", [])
        b = await todo.create_task("b", {}, "0x2", "g", [])
        c = await todo.create_task("c", {}, "0x3", "g", [])
        await todo.add_dependencies(a, [b])  # a depends on b
        await todo.add_dependencies(c, [a])  # c depends on a

        # Deleting a (referenced by c, referencing b) must not FK-violate.
        await todo.delete_task(a)

        assert await todo._dependencies(c) == set()  # join rows cleaned
        assert await todo._dependencies(b) == set()
        assert b in {t["id"] for t in await todo.get_ready_tasks()}
        assert c in {t["id"] for t in await todo.get_ready_tasks()}
    finally:
        await todo.close()

