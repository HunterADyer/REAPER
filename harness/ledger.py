"""Function ledger store — Deliverable 1.5.

Per-function notepad stored as a SQLite database (``aiosqlite``). Claims are
created with ``truth_level = NULL`` and later evaluated by the critic. All
writes are serialized through a per-instance ``asyncio.Lock``; reads are
lock-free (WAL mode allows concurrent readers). Neo4j is written first and
SQLite second (no cross-store atomicity — failures are logged, not rolled
back).
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

import aiosqlite

log = logging.getLogger(__name__)

_TRUTH_LEVELS = (
    "speculation", "inferred", "low_confidence", "mid_confidence", "high_confidence"
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS functions (
    address TEXT PRIMARY KEY,
    llm_name TEXT,
    canon_name TEXT,
    summary TEXT
);

CREATE TABLE IF NOT EXISTS claims (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    function_address TEXT NOT NULL,
    claim_text TEXT NOT NULL,
    truth_level TEXT CHECK(truth_level IN
        ('speculation','inferred','low_confidence','mid_confidence','high_confidence')
        OR truth_level IS NULL),
    submitted_by TEXT,
    reviewed_by TEXT,
    created_at TEXT,
    updated_at TEXT,
    FOREIGN KEY (function_address) REFERENCES functions(address)
);

CREATE TABLE IF NOT EXISTS evidence_links (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    claim_id INTEGER NOT NULL,
    address_start TEXT NOT NULL,
    address_end TEXT NOT NULL,
    description TEXT,
    FOREIGN KEY (claim_id) REFERENCES claims(id) ON DELETE CASCADE
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Ledger:
    """Async per-function claim/name store backed by SQLite + Neo4j sync."""

    def __init__(self, db_path: str, neo4j_driver):
        """Stores db_path and neo4j_driver only — NO I/O here.

        Caller MUST call ``await ledger.init()`` before any other method.
        """
        self.db_path = db_path
        self.neo4j_driver = neo4j_driver
        self._db: aiosqlite.Connection | None = None
        self._write_lock: asyncio.Lock | None = None

    # -- lifecycle ------------------------------------------------------------

    async def init(self) -> None:
        """Open aiosqlite connection with WAL + FK pragmas, create tables."""
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
            raise RuntimeError("Ledger.init() not called")
        return self._db

    # -- functions ------------------------------------------------------------

    async def register_functions_from_graph(self) -> None:
        """Query Neo4j for all :Function nodes, INSERT OR IGNORE into the
        functions table. Idempotent — safe to call multiple times."""
        db = self._require_ready()
        async with self._write_lock:
            rows = []
            try:
                async with self.neo4j_driver.session() as session:
                    res = await session.run(
                        "MATCH (f:Function) RETURN f.address AS address"
                    )
                    rows = [rec async for rec in res]
            except Exception:
                log.exception("register_functions_from_graph: Neo4j query failed")
                return
            for rec in rows:
                await db.execute(
                    "INSERT OR IGNORE INTO functions (address) VALUES (?)",
                    (rec["address"],),
                )
            await db.commit()
            log.info("registered %d functions from graph", len(rows))

    async def get_function(self, address: str) -> dict | None:
        db = self._require_ready()
        cursor = await db.execute(
            "SELECT address, llm_name, canon_name, summary FROM functions WHERE address = ?",
            (address,),
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        return {
            "address": row[0],
            "llm_name": row[1],
            "canon_name": row[2],
            "summary": row[3],
        }

    async def set_function_names(self, address: str, llm_name: str, canon_name: str) -> None:
        db = self._require_ready()
        async with self._write_lock:
            await db.execute(
                "UPDATE functions SET llm_name = ?, canon_name = ? WHERE address = ?",
                (llm_name, canon_name, address),
            )
            await db.commit()

    async def set_function_summary(self, address: str, summary: str) -> None:
        db = self._require_ready()
        async with self._write_lock:
            await db.execute(
                "UPDATE functions SET summary = ? WHERE address = ?",
                (summary, address),
            )
            await db.commit()

    # -- claims ---------------------------------------------------------------

    async def add_claim(
        self,
        function_address: str,
        claim_text: str,
        submitted_by: str,
        evidence: list[dict],
    ) -> int:
        """Insert claim with truth_level=NULL. Returns claim_id.

        Claim exists in the DB immediately — the critic evaluates it next.
        """
        db = self._require_ready()
        now = _now()
        async with self._write_lock:
            await db.execute(
                "INSERT OR IGNORE INTO functions (address) VALUES (?)",
                (function_address,),
            )
            cursor = await db.execute(
                "INSERT INTO claims (function_address, claim_text, submitted_by, "
                "created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                (function_address, claim_text, submitted_by, now, now),
            )
            claim_id = int(cursor.lastrowid)
            for ev in evidence:
                await db.execute(
                    "INSERT INTO evidence_links (claim_id, address_start, "
                    "address_end, description) VALUES (?, ?, ?, ?)",
                    (
                        claim_id,
                        ev.get("address_start"),
                        ev.get("address_end"),
                        ev.get("description"),
                    ),
                )
            await db.commit()
            return claim_id

    async def set_truth_level(self, claim_id: int, truth_level: str, reviewed_by: str) -> None:
        if truth_level not in _TRUTH_LEVELS:
            raise ValueError(f"invalid truth_level {truth_level!r}")
        db = self._require_ready()
        async with self._write_lock:
            await db.execute(
                "UPDATE claims SET truth_level = ?, reviewed_by = ?, updated_at = ? "
                "WHERE id = ?",
                (truth_level, reviewed_by, _now(), claim_id),
            )
            await db.commit()

    async def update_claim_text(self, claim_id: int, new_text: str) -> None:
        db = self._require_ready()
        async with self._write_lock:
            await db.execute(
                "UPDATE claims SET claim_text = ?, updated_at = ? WHERE id = ?",
                (new_text, _now(), claim_id),
            )
            await db.commit()

    async def delete_claim(self, claim_id: int) -> None:
        db = self._require_ready()
        async with self._write_lock:
            await db.execute("DELETE FROM claims WHERE id = ?", (claim_id,))
            await db.commit()

    async def get_claims(self, function_address: str) -> list[dict]:
        db = self._require_ready()
        cursor = await db.execute(
            "SELECT id, function_address, claim_text, truth_level, submitted_by, "
            "reviewed_by, created_at, updated_at "
            "FROM claims WHERE function_address = ? ORDER BY id",
            (function_address,),
        )
        rows = await cursor.fetchall()
        return [
            {
                "id": r[0],
                "function_address": r[1],
                "claim_text": r[2],
                "truth_level": r[3],
                "submitted_by": r[4],
                "reviewed_by": r[5],
                "created_at": r[6],
                "updated_at": r[7],
            }
            for r in rows
        ]

    async def get_evidence(self, claim_id: int) -> list[dict]:
        db = self._require_ready()
        cursor = await db.execute(
            "SELECT id, claim_id, address_start, address_end, description "
            "FROM evidence_links WHERE claim_id = ?",
            (claim_id,),
        )
        rows = await cursor.fetchall()
        return [
            {
                "id": r[0],
                "claim_id": r[1],
                "address_start": r[2],
                "address_end": r[3],
                "description": r[4],
            }
            for r in rows
        ]

    async def all_functions_have_claims(self) -> bool:
        """Every :Function in Neo4j has at least one claim with
        truth_level IS NOT NULL in the ledger."""
        db = self._require_ready()
        try:
            async with self.neo4j_driver.session() as session:
                res = await session.run("MATCH (f:Function) RETURN f.address AS address")
                func_addresses = [rec["address"] for rec in [r async for r in res]]
        except Exception:
            log.exception("all_functions_have_claims: Neo4j query failed")
            return False
        for addr in func_addresses:
            cursor = await db.execute(
                "SELECT COUNT(*) FROM claims WHERE function_address = ? "
                "AND truth_level IS NOT NULL",
                (addr,),
            )
            row = await cursor.fetchone()
            if row is None or row[0] == 0:
                return False
        return True


__all__ = ["Ledger"]

