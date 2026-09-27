"""TODO ledger — Deliverable 1.6.

Task queue stored in SQLite (``aiosqlite``). Tasks have descriptions,
dependencies (via a join table), status, graph references, and critic
feedback. There is NO ``rejected`` status — a rejected task returns to
``pending`` with ``critic_feedback`` set and ``rejection_count`` incremented,
and ``get_ready_tasks()`` picks it up again. All writes serialized through a
per-instance ``asyncio.Lock``; reads are lock-free (WAL mode).
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone

import aiosqlite

log = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    description TEXT NOT NULL,
    status TEXT CHECK(status IN ('pending','in_progress','completed'))
        DEFAULT 'pending',
    context_spec TEXT,
    start_position TEXT,
    goal TEXT,
    graph_refs TEXT,
    assigned_to TEXT,
    critic_feedback TEXT,
    rejection_count INTEGER DEFAULT 0,
    created_at TEXT,
    completed_at TEXT,
    started_at TEXT
);

CREATE TABLE IF NOT EXISTS task_dependencies (
    task_id INTEGER NOT NULL,
    depends_on INTEGER NOT NULL,
    PRIMARY KEY (task_id, depends_on),
    FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE,
    FOREIGN KEY (depends_on) REFERENCES tasks(id) ON DELETE CASCADE
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class TodoLedger:
    """Async task queue (TODO ledger) backed by SQLite.

    Requires ``await todo.init()`` after construction.
    """

    def __init__(self, db_path: str):
        self.db_path = db_path
        self._db: aiosqlite.Connection | None = None
        self._write_lock: asyncio.Lock | None = None

    async def init(self) -> None:
        """Open aiosqlite connection, set PRAGMAs, create tables + migrate."""
        self._db = await aiosqlite.connect(self.db_path)
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.execute("PRAGMA foreign_keys=ON")
        await self._db.executescript(_SCHEMA)
        await self._migrate()
        await self._db.commit()
        self._write_lock = asyncio.Lock()

    async def _migrate(self) -> None:
        """Backfill columns added after first release (started_at). Existing
        SQLite files created before the field existed must not crash on reads."""
        cursor = await self._db.execute("PRAGMA table_info(tasks)")
        cols = {r[1] for r in await cursor.fetchall()}
        if "started_at" not in cols:
            await self._db.execute("ALTER TABLE tasks ADD COLUMN started_at TEXT")

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()

    def _require_ready(self) -> aiosqlite.Connection:
        if self._db is None:
            raise RuntimeError("TodoLedger.init() not called")
        return self._db

    @staticmethod
    def _row_to_dict(row) -> dict:
        started_at = row[12] if len(row) > 12 else None
        return {
            "id": row[0],
            "description": row[1],
            "status": row[2],
            "context_spec": json.loads(row[3]) if row[3] else {},
            "start_position": row[4],
            "goal": row[5],
            "graph_refs": json.loads(row[6]) if row[6] else [],
            "assigned_to": row[7],
            "critic_feedback": row[8],
            "rejection_count": row[9],
            "created_at": row[10],
            "completed_at": row[11],
            "started_at": started_at,
        }

    # -- writes ----------------------------------------------------------------

    async def create_task(
        self,
        description: str,
        context_spec: dict,
        start_position: str,
        goal: str,
        graph_refs: list[str],
        depends_on: list[int] | None = None,
    ) -> int:
        """Create a task. Acquires _write_lock. Returns new task id."""
        db = self._require_ready()
        depends_on = depends_on or []
        now = _now()
        async with self._write_lock:
            cursor = await db.execute(
                "INSERT INTO tasks (description, context_spec, start_position, "
                "goal, graph_refs, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    description,
                    json.dumps(context_spec or {}),
                    start_position,
                    goal,
                    json.dumps(graph_refs or []),
                    now,
                ),
            )
            task_id = int(cursor.lastrowid)
            for dep in depends_on:
                await db.execute(
                    "INSERT OR IGNORE INTO task_dependencies (task_id, depends_on) "
                    "VALUES (?, ?)",
                    (task_id, dep),
                )
            await db.commit()
            return task_id

    async def assign_task(self, task_id: int, session_id: str) -> None:
        """Sets status to 'in_progress' and records started_at (stale clock).
        Acquires _write_lock."""
        db = self._require_ready()
        async with self._write_lock:
            await db.execute(
                "UPDATE tasks SET status = 'in_progress', assigned_to = ?, "
                "started_at = ? WHERE id = ?",
                (session_id, _now(), task_id),
            )
            await db.commit()

    async def complete_task(self, task_id: int) -> None:
        """Sets status to 'completed', records completed_at. Acquires _write_lock."""
        db = self._require_ready()
        async with self._write_lock:
            await db.execute(
                "UPDATE tasks SET status = 'completed', completed_at = ? WHERE id = ?",
                (_now(), task_id),
            )
            await db.commit()

    async def reject_task(self, task_id: int, feedback: str) -> int:
        """Return task to 'pending', set critic_feedback, increment
        rejection_count, clear the started_at stale clock. Returns the new
        count. Acquires _write_lock."""
        db = self._require_ready()
        async with self._write_lock:
            await db.execute(
                "UPDATE tasks SET status = 'pending', critic_feedback = ?, "
                "rejection_count = rejection_count + 1, started_at = NULL "
                "WHERE id = ?",
                (feedback, task_id),
            )
            await db.commit()
            cursor = await db.execute(
                "SELECT rejection_count FROM tasks WHERE id = ?", (task_id,)
            )
            row = await cursor.fetchone()
            return int(row[0]) if row else 0

    async def requeue_task(self, task_id: int, feedback: str = "") -> None:
        """Return task to 'pending' WITHOUT bumping rejection_count, and clear
        the started_at stale clock.

        Used by the investigation loop (7.3) to retry a task that spawned
        subtasks or whose claims were all rejected — counting that as a
        'rejection' would be misleading (it is a legitimate retry).
        """
        db = self._require_ready()
        async with self._write_lock:
            await db.execute(
                "UPDATE tasks SET status = 'pending', assigned_to = NULL, "
                "started_at = NULL, critic_feedback = ? WHERE id = ?",
                (feedback, task_id),
            )
            await db.commit()

    async def add_dependencies(self, task_id: int, dep_ids: list[int]) -> None:
        """Add dependencies to an EXISTING task (e.g. a parent now waiting on
        the subtasks it spawned). Missing/deleted dep ids are simply ignored.
        Acquires _write_lock."""
        dep_ids = [int(d) for d in (dep_ids or []) if d is not None]
        if not dep_ids:
            return
        db = self._require_ready()
        async with self._write_lock:
            for dep in dep_ids:
                await db.execute(
                    "INSERT OR IGNORE INTO task_dependencies (task_id, depends_on) "
                    "VALUES (?, ?)",
                    (int(task_id), dep),
                )
            await db.commit()

    async def delete_task(self, task_id: int) -> None:
        """Remove a merged-away task and any dependency rows referencing it.

        Deleting a task that others depend on (or that depends on others)
        must not hit an FK violation — with pragma foreign_keys=ON and without
        ON DELETE CASCADE the join rows must be removed explicitly (this also
        covers legacy DBs created before the CASCADE clause was added).
        Acquires _write_lock."""
        db = self._require_ready()
        async with self._write_lock:
            await db.execute(
                "DELETE FROM task_dependencies WHERE task_id = ? OR depends_on = ?",
                (task_id, task_id),
            )
            await db.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
            await db.commit()

    # -- reads -----------------------------------------------------------------

    async def get_task(self, task_id: int) -> dict | None:
        db = self._require_ready()
        cursor = await db.execute("SELECT * FROM tasks WHERE id = ?", (task_id,))
        row = await cursor.fetchone()
        if row is None:
            return None
        return self._row_to_dict(row)

    async def _all_tasks(self) -> list[dict]:
        db = self._require_ready()
        cursor = await db.execute("SELECT * FROM tasks ORDER BY id")
        rows = await cursor.fetchall()
        return [self._row_to_dict(r) for r in rows]

    async def _dependencies(self, task_id: int) -> set[int]:
        db = self._require_ready()
        cursor = await db.execute(
            "SELECT depends_on FROM task_dependencies WHERE task_id = ?", (task_id,)
        )
        rows = await cursor.fetchall()
        return {r[0] for r in rows}

    async def get_ready_tasks(self) -> list[dict]:
        """All 'pending' tasks whose dependencies are all 'completed'.

        A dependency whose task no longer exists (merged away / deleted) counts
        as satisfied — otherwise a parent whose subtask was deleted by the
        scheduler would strand forever.
        """
        tasks = await self._all_tasks()
        ready = []
        for t in tasks:
            if t["status"] != "pending":
                continue
            deps = await self._dependencies(t["id"])
            if not deps:
                ready.append(t)
                continue
            all_done = True
            for dep in deps:
                dep_task = await self.get_task(dep)
                if dep_task is None:
                    continue  # deleted dep no longer blocks (missing = satisfied)
                if dep_task["status"] != "completed":
                    all_done = False
                    break
            if all_done:
                ready.append(t)
        return ready

    async def get_pending_tasks(self) -> list[dict]:
        """All tasks still in 'pending' status (regardless of dependencies).

        Used by the scheduler (7.1) to review pending descriptions for merge /
        decompose recommendations.
        """
        tasks = await self._all_tasks()
        return [t for t in tasks if t["status"] == "pending"]

    async def reset_stale_in_progress(self, timeout_seconds: int) -> int:
        """Reset in_progress tasks whose execution window (``started_at``, set
        by ``assign_task`` — NOT ``created_at``, which is the queue-insertion
        time) elapsed more than ``timeout_seconds`` ago, back to pending (stale
        task check, design § 7.1). Returns the count of tasks reset. Tasks whose
        timestamp cannot be parsed are treated as stale (conservative — never
        strand an in_progress task forever).
        """
        tasks = await self._all_tasks()
        now = datetime.now(timezone.utc)
        stale_ids: list[int] = []
        for t in tasks:
            if t["status"] != "in_progress":
                continue
            # The stale clock is the assignment time; created_at is only a
            # fallback for rows written before started_at existed.
            started_at = t.get("started_at") or t.get("created_at") or ""
            try:
                started = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
                if started.tzinfo is None:
                    started = started.replace(tzinfo=timezone.utc)
                stale = (now - started).total_seconds() > int(timeout_seconds)
            except ValueError:
                stale = True  # unparseable -> conservative reset
            if stale:
                stale_ids.append(t["id"])
        if not stale_ids:
            return 0
        db = self._require_ready()
        async with self._write_lock:
            for task_id in stale_ids:
                await db.execute(
                    "UPDATE tasks SET status = 'pending', assigned_to = NULL, "
                    "started_at = NULL WHERE id = ?", (task_id,)
                )
            await db.commit()
        return len(stale_ids)

    async def is_empty(self) -> bool:
        """All tasks are 'completed'. No pending or in_progress."""
        tasks = await self._all_tasks()
        return all(t["status"] == "completed" for t in tasks)

    async def has_in_progress(self) -> bool:
        """Any tasks currently in_progress."""
        tasks = await self._all_tasks()
        return any(t["status"] == "in_progress" for t in tasks)

    async def pending_count(self) -> int:
        tasks = await self._all_tasks()
        return sum(1 for t in tasks if t["status"] == "pending")


__all__ = ["TodoLedger"]

