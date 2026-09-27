"""Shadow copy manager — Deliverable 3.2.

Checkout/check-in mechanism for agent working state. Agents check out an
N-hop neighborhood of a function (via :CALL/:CONTAINS edges), mutate a local
copy, and later ``diff``/``apply`` back to master.

Concurrency model (design § 3.2):
- Every ``apply`` bumps an in-memory monotonic version counter.
- A checkout records the version at checkout time.
- ``diff`` reports a CONFLICT when ``master_version > checkout_version`` AND
  the current master value for a field the agent changed differs from the
  value the agent saw at checkout. Conflict detection does not require real
  concurrency — two sequential checkouts of the same function with different
  renames reproduce it (this is how the integration test works).

Store discipline:
- Neo4j writes use MERGE, never CREATE (idempotent by design).
- Neo4j is written FIRST, SQLite ledger second. A SQLite failure after a
  successful Neo4j write is logged, NOT rolled back (accepted limitation).
"""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)

# Property keys REAPER writes through the graph — used to build safe Cypher.
_ALLOWED_FIELDS = {
    "llm_name", "canon_name", "type", "summary", "pinned", "ambiguous",
    "scc_id", "traversal_order", "isolated", "value",
}

_FUNCTION_NODE = "Function"


def _infer_label(node_id: str) -> str:
    """Function nodes are keyed by ``address``; Variable nodes by ``id``."""
    return _FUNCTION_NODE if ":" not in str(node_id) else "Variable"


class ShadowCopyManager:
    """Checkout / diff / apply / discard for agent working state."""

    def __init__(self, neo4j_driver, ledger, config: dict):
        self.neo4j_driver = neo4j_driver
        self.ledger = ledger
        limits = (config or {}).get("limits") or {}
        self.hops = int(limits.get("shadow_copy_hops", 2))
        # In-memory monotonic version counter, incremented on every apply().
        self._version = 0
        # session_id -> checkout snapshot dict (see checkout()).
        self._checkouts: dict[str, dict] = {}

    async def checkout(self, func_address: str, session_id: str) -> dict:
        """Checkout ``func_address`` + N-hop neighborhood.

        Returns a snapshot dict::

            {
                "session_id": ..., "function": func_address,
                "checkout_version": self._version,
                "nodes": {node_id: props, ...},
                "edges": [{"source":..., "target":..., "type":...}, ...],
                "claims": {function_address: [claim, ...], ...},
            }
        """
        snapshot = {
            "session_id": session_id,
            "function": func_address,
            "checkout_version": self._version,
            "nodes": {},
            "edges": [],
            "claims": {},
        }

        seen: set[str] = set()
        seed = str(func_address)
        node = await self._fetch_node(seed)
        if node is not None:
            snapshot["nodes"][seed] = node
        seen.add(seed)

        frontier = {seed}
        # hop budget = number of edge traversals (0 hops => seed node only).
        for _hop in range(self.hops):
            if not frontier:
                break
            next_frontier: set[str] = set()
            for node_id in frontier:
                edges = await self._fetch_edges(node_id)
                for rec in edges:
                    snapshot["edges"].append({
                        "source": rec.get("source"),
                        "target": rec.get("target"),
                        "type": rec.get("type"),
                    })
                    target = rec.get("target")
                    if target and target not in seen:
                        seen.add(target)
                        snapshot["nodes"].setdefault(target, self._node_props(rec.get("node")))
                        next_frontier.add(target)
            frontier = next_frontier

        # Claims for every function-like node in the neighborhood.
        for node_id in list(snapshot["nodes"]):
            if ":" in node_id:  # Variable/Argument/Call node, not a function
                continue
            try:
                snapshot["claims"][node_id] = await self.ledger.get_claims(node_id)
            except Exception:
                log.exception("checkout: ledger.get_claims(%s) failed", node_id)
                snapshot["claims"][node_id] = []

        self._checkouts[session_id] = snapshot
        return snapshot

    def get_checkout(self, session_id: str) -> dict | None:
        """Return the stored checkout snapshot for a session (or None)."""
        return self._checkouts.get(session_id)

    async def diff(self, session_id: str, modified: dict) -> dict:
        """Compare a modified snapshot against current master state.

        ``modified`` shape (mirrors a checkout snapshot subset)::

            {
                "nodes": {node_id: {"field": proposed_value, ...}, ...},
                "claims": [ {claim payload}, ... ],   # always safe to add
            }

        Returns ``{"mutations": [...], "conflicts": [...]}``.

        Conflict criterion (literal, per design § 3.2): master version is
        higher than the checkout version AND the current master value differs
        from the value the agent observed at checkout for a field the agent
        changed. Mutations are the non-conflicting changes; claims never
        conflict (they are pure additions).
        """
        snap = self._require_checkout(session_id)
        mutations: list[dict] = []
        conflicts: list[dict] = []

        for node_id, fields in (modified.get("nodes") or {}).items():
            for field, proposed in fields.items():
                checkout_value = snap["nodes"].get(node_id, {}).get(field)
                master_value = await self._master_value(node_id, field)
                record = {
                    "session_id": session_id,
                    "node_id": node_id,
                    "field": field,
                    "value": proposed,
                    "checkout_value": checkout_value,
                    "master_value": master_value,
                    "conflict": False,
                }
                if (
                    self._version > snap["checkout_version"]
                    and master_value != checkout_value
                ):
                    record["conflict"] = True
                    conflicts.append(record)
                else:
                    mutations.append(record)

        for claim in (modified.get("claims") or []):
            mutations.append({"kind": "claim", **dict(claim)})

        return {"mutations": mutations, "conflicts": conflicts}

    async def apply(self, session_id: str, mutations: list[dict]) -> None:
        """Apply mutations to Neo4j first, then the SQLite ledger.

        Increments the version counter. If a Neo4j write fails it is logged and
        skipped (no cross-store rollback); if a SQLite write fails after a
        successful Neo4j write, that inconsistency is logged, never rolled back.
        """
        for mutation in mutations or []:
            await self._apply_mutation(mutation)
        self._version += 1

    def discard(self, session_id: str) -> None:
        """Discard a checkout."""
        self._checkouts.pop(session_id, None)

    def current_version(self) -> int:
        """Expose the monotonic version counter (test/observability helper)."""
        return self._version

    def _require_checkout(self, session_id: str) -> dict:
        snap = self._checkouts.get(session_id)
        if snap is None:
            raise KeyError(
                f"no checkout for session {session_id!r} — call checkout() first"
            )
        return snap


    # ------------------------------------------------------------------ #
    # Internals
    # ------------------------------------------------------------------ #

    async def _fetch_node(self, node_id: str) -> dict | None:
        try:
            async with self.neo4j_driver.session() as session:
                res = await session.run(
                    "MATCH (n) WHERE n.id = $id OR n.address = $id RETURN n",
                    id=node_id,
                )
                rows = [r async for r in res]
        except Exception:
            log.exception("checkout: fetch node %s failed", node_id)
            return None
        if not rows:
            return None
        return self._node_props(rows[0].get("n"))

    async def _fetch_edges(self, node_id: str) -> list[dict]:
        try:
            async with self.neo4j_driver.session() as session:
                res = await session.run(
                    "MATCH (a) WHERE a.id = $id OR a.address = $id WITH a "
                    "MATCH (a)-[r:CALL|CONTAINS]-(b) "
                    "RETURN a.id AS source, b.id AS target, type(r) AS type, b AS node",
                    id=node_id,
                )
                return [r async for r in res]
        except Exception:
            log.exception("checkout: fetch edges for %s failed", node_id)
            return []

    async def _master_value(self, node_id: str, field: str):
        node = await self._fetch_node(node_id)
        if node is None:
            return None
        return node.get(field)

    async def _apply_mutation(self, mutation: dict) -> None:
        kind = mutation.get("kind")
        if kind == "claim":
            try:
                await self.ledger.add_claim(
                    function_address=mutation["function_address"],
                    claim_text=mutation["claim_text"],
                    submitted_by=mutation.get("submitted_by", mutation.get("session_id")),
                    evidence=list(mutation.get("evidence") or []),
                )
            except Exception:
                log.exception("shadow apply: SQLite claim write failed: %s", mutation)
            return

        # Neo4j first.
        node_id = mutation["node_id"]
        field = mutation["field"]
        value = mutation["value"]
        try:
            await self._merge_node_prop(node_id, field, value)
        except Exception:
            log.exception("shadow apply: Neo4j write failed: %s", mutation)
            return  # no rollback; skip the SQLite follow-up too

        # SQLite ledger second — keep function names in sync (best-effort).
        if _infer_label(node_id) == _FUNCTION_NODE and field in ("llm_name", "canon_name"):
            try:
                current = await self.ledger.get_function(node_id) or {}
                llm = value if field == "llm_name" else current.get("llm_name")
                canon = value if field == "canon_name" else current.get("canon_name")
                await self.ledger.set_function_names(node_id, llm, canon)
            except Exception:
                log.exception("shadow apply: SQLite function-rename sync failed: %s", node_id)

    async def _merge_node_prop(self, node_id: str, field: str, value) -> None:
        """MERGE the node by its stable id and SET a single property.

        ``field`` is drawn from the caller (mutations) — guarded through
        _ALLOWED_FIELDS to keep the Cypher injection-safe.
        """
        if field not in _ALLOWED_FIELDS:
            log.warning("shadow apply: skipping unmanaged field %r", field)
            return
        label = _infer_label(node_id)
        async with self.neo4j_driver.session() as session:
            if label == _FUNCTION_NODE:
                query = f"MERGE (n:Function {{address: $id}}) SET n.{field} = $value"
            else:
                query = f"MERGE (n:{label} {{id: $id}}) SET n.{field} = $value"
            await session.run(query, id=node_id, value=value)

    @staticmethod
    def _node_props(value) -> dict:
        """Normalize a Neo4j node OR a fake dict into a plain props dict.

        Property keys are read from a whitelist so both real neo4j Node
        objects (which support ``.get``) and plain test dicts work.
        """
        if isinstance(value, dict):
            return {
                k: v for k, v in value.items()
                if k in _ALLOWED_FIELDS or k in ("id", "address")
            }
        out = {}
        if value is not None and hasattr(value, "get"):
            for key in list(_ALLOWED_FIELDS) + ["id", "address"]:
                try:
                    v = value.get(key)
                except Exception:
                    v = None
                if v is not None:
                    out[key] = v
        return out


__all__ = ["ShadowCopyManager"]

