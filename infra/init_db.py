"""Neo4j schema initialization — Deliverable 1.2.

Provides ``init_schema(neo4j_driver)``: connects to Neo4j, runs every
statement in ``schema.cypher``, then verifies the expected constraints are
present. Fully async (``neo4j.AsyncDriver``) and idempotent — every Cypher
statement is guarded with ``IF NOT EXISTS`` so re-running is safe after a
crash or partial application.
"""

from __future__ import annotations

import logging
from pathlib import Path

log = logging.getLogger(__name__)

# Constraints that MUST exist after schema initialization. Keys are the
# (label, property) pairs introduced in schema.cypher.
_REQUIRED_CONSTRAINTS = [
    ("Function", "address"),
    ("Variable", "id"),
    ("Argument", "id"),
    ("Call", "id"),
    ("StringRef", "id"),
]

_SCHEMA_PATH = Path(__file__).parent / "schema.cypher"


def _load_schema_statements() -> list[str]:
    """Read schema.cypher and split into individual runnable statements.

    Splits on ';' boundaries while ignoring semicolons inside line comments
    (the schema file contains none) and blank lines.
    """
    text = _SCHEMA_PATH.read_text(encoding="utf-8")
    statements = []
    for raw in text.split(";"):
        stmt = " ".join(
            line.strip() for line in raw.splitlines()
            if line.strip() and not line.strip().startswith("//")
        ).strip()
        if stmt:
            statements.append(stmt)
    return statements


async def init_schema(neo4j_driver) -> None:
    """Apply the REAPER graph schema to the connected Neo4j instance.

    Args:
        neo4j_driver: an open ``neo4j.AsyncGraphDatabase.driver(...)`` object.

    Raises:
        RuntimeError: if a required constraint is missing after application.
    """
    statements = _load_schema_statements()
    if not statements:
        raise RuntimeError(f"no statements parsed from {_SCHEMA_PATH}")

    try:
        async with neo4j_driver.session() as session:
            for stmt in statements:
                log.debug("running schema statement: %s", stmt[:80])
                await session.run(stmt)
            await session.run(
                "CALL db.awaitIndexes()"
            )  # block until indexes come online
    except Exception:
        log.exception("schema application failed")
        raise

    missing = []
    async with neo4j_driver.session() as session:
        for label, prop in _REQUIRED_CONSTRAINTS:
            result = await session.run(
                """
                SHOW CONSTRAINTS
                YIELD labelsOrTypes, properties
                WHERE $label IN labelsOrTypes AND $prop IN properties
                RETURN count(*) AS n
                """,
                {"label": label, "prop": prop},
            )
            record = await result.single()
            if record is None or record["n"] == 0:
                missing.append(f"{label}.{prop}")

    if missing:
        raise RuntimeError(
            f"schema initialization incomplete; missing constraints: {missing}"
        )
    log.info("REAPER Neo4j schema verified (%d statements applied)", len(statements))


async def verify_schema(neo4j_driver) -> bool:
    """Non-raising check that the required schema constraints are present."""
    try:
        await init_schema(neo4j_driver)
        return True
    except Exception:
        return False
