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
    completed_at TEXT
);

CREATE TABLE IF NOT EXISTS task_dependencies (
    task_id INTEGER NOT NULL,
    depends_on INTEGER NOT NULL,
    PRIMARY KEY (task_id, depends_on),
    FOREIGN KEY (task_id) REFERENCES tasks(id),
    FOREIGN KEY (depends_on) REFERENCES tasks(id)
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
        """Open aiosqlite connection, set PRAGMAs, create tables."""
        self._db = await aiosqlite.connect(self.db_path)
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.execute("PRAGMA foreign_keys=ON")
        await self._db.executescript(_SCHEMA)
        await self._db.commit()
        self._write_lock = asyncio.Lock()

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()

    def _require_ready(self) -> aiosqlite.Connection:
        if self._db is None:
            raise RuntimeError("TodoLedger.init() not called")
        return self._db

    @staticmethod
    def _row_to_dict(row) -> dict:
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
        """Sets status to 'in_progress'. Acquires _write_lock."""
        db = self._require_ready()
        async with self._write_lock:
            await db.execute(
                "UPDATE tasks SET status = 'in_progress', assigned_to = ? WHERE id = ?",
                (session_id, task_id),
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
        rejection_count. Returns the new count. Acquires _write_lock."""
        db = self._require_ready()
        async with self._write_lock:
            await db.execute(
                "UPDATE tasks SET status = 'pending', critic_feedback = ?, "
                "rejection_count = rejection_count + 1 WHERE id = ?",
                (feedback, task_id),
            )
            await db.commit()
            cursor = await db.execute(
                "SELECT rejection_count FROM tasks WHERE id = ?", (task_id,)
            )
            row = await cursor.fetchone()
            return int(row[0]) if row else 0

    async def delete_task(self, task_id: int) -> None:
        """Remove a merged-away task. Acquires _write_lock."""
        db = self._require_ready()
        async with self._write_lock:
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
        """All 'pending' tasks whose dependencies are all 'completed'."""
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
                if dep_task is None or dep_task["status"] != "completed":
                    all_done = False
                    break
            if all_done:
                ready.append(t)
        return ready

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

