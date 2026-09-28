"""In-memory Neo4j driver for harness_phase3 tests — test-support only.

The shared ``conftest.FakeNeo4jDriver`` only answers function-address queries,
which is not enough for ShadowCopyManager/ContextAssembler/MergeAgent tests
(checkout neighborhoods, diff/master reads, MERGE writes, CALL edge traversal).

This driver implements the *exact subset* of Cypher that the harness
production code issues (recognized by distinctive substrings), backed by an
in-memory graph held in plain Python. Query recognition is intentional: the
production query strings are fixed in the deliverable sources, so this fake
does not attempt to be a general Cypher engine.

Supported query shapes (all others -> empty result):
  - MERGE (n:Label {id/address: $id}) SET n.field = $value
  - MATCH (n) WHERE n.id = $id OR n.address = $id RETURN n
  - MATCH (a) WHERE ... CALL|CONTAINS ... RETURN a.id AS source, b.id AS
    target, type(r) AS type, b AS node            (neighbor expansion)
  - MATCH (f:Function)-[:CALL]->(c:Function) WHERE f.address = $id RETURN
    c.address AS id, c.llm_name ..., c.canon_name ...        (callees)
  - MATCH (f:Function)<-[:CALL]-(p:Function) WHERE f.address = $id RETURN
    p.address AS id, ...                                     (callers)
  - MATCH (f:Function)-[:CALL]-(c:Function) ... RETURN count(c) AS n
  - MATCH (f:Function) WHERE f.pinned = true RETURN f.address AS id, ...
  - MATCH (f:Function) RETURN f.address AS address            (Ledger sync)
"""

from __future__ import annotations

import re

_PROP_KEYS = {
    "id", "address", "llm_name", "canon_name", "pinned", "ambiguous",
    "scc_id", "traversal_order", "isolated", "summary", "type", "value",
    "ordinal", "source", "name",
}


class _Result:
    """Async-iterable record result mirroring neo4j AsyncResult (see conftest)."""

    def __init__(self, records):
        self._records = list(records)

    def __await__(self):
        if False:  # pragma: no cover - unreachable; makes this a generator
            yield
        return self

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            return self._records.pop(0)
        except IndexError:
            raise StopAsyncIteration


class _Session:
    def __init__(self, driver):
        self._driver = driver

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def run(self, query, parameters=None, **kwargs):
        params = {}
        if parameters:
            params.update(parameters)
        params.update(kwargs)
        return _Result(self._driver._run(query, params))

class MemoryNeo4jDriver:
    """Cypher-subset-in-memory fake: nodes keyed by stable id + edge list."""

    def __init__(self):
        self._nodes: dict[str, dict] = {}  # stable_id -> {"label":..., "props": {...}}
        self._edges: list[tuple[str, str, str]] = []  # (source, target, type)
        self.writes: list[tuple[str, dict]] = []      # every MERGE query

    # -- graph seeding helpers ------------------------------------------------

    def add_function(self, address: str, **props) -> "MemoryNeo4jDriver":
        props = {"address": address, **props}
        self._nodes[address] = {"label": "Function", "props": props}
        return self

    def add_variable(self, node_id: str, **props) -> "MemoryNeo4jDriver":
        props = {"id": node_id, **props}
        self._nodes[node_id] = {"label": "Variable", "props": props}
        return self

    def add_argument(self, node_id: str, **props) -> "MemoryNeo4jDriver":
        props = {"id": node_id, **props}
        self._nodes[node_id] = {"label": "Argument", "props": props}
        return self

    def add_call(self, node_id: str, **props) -> "MemoryNeo4jDriver":
        props = {"id": node_id, **props}
        self._nodes[node_id] = {"label": "Call", "props": props}
        return self

    def add_edge(self, source: str, target: str, rtype: str) -> "MemoryNeo4jDriver":
        self._edges.append((source, target, rtype))
        return self

    def session(self):
        return _Session(self)

    async def close(self) -> None:
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    # -- inspection (tests only) ----------------------------------------------

    def get(self, node_id: str) -> dict | None:
        node = self._get(node_id)
        return dict(node) if node is not None else None

    def edges(self) -> list[tuple[str, str, str]]:
        return list(self._edges)

    # -- query dispatch -------------------------------------------------------

    def _run(self, query: str, params: dict) -> list[dict]:
        stripped = query.strip()
        if stripped.startswith("MERGE"):
            return self._run_merge(query, params)
        if "RETURN n" in query and "WHERE n.id" in query:
            node = self._get(params.get("id"))
            return [{"n": node}] if node is not None else []
        if "CALL|CONTAINS" in query and "source" in query:
            return self._neighbors(params.get("id"))
        if "count(c)" in query:
            return [{"n": self._call_count(params.get("id"))}]
        if "pinned = true" in query:
            return self._pinned()
        if "->(c:Function)" in query and "c.address AS id" in query:
            return self._out_calls(params.get("id"))
        if "<-[:CALL]-(p:Function)" in query and "p.address AS id" in query:
            return self._in_calls(params.get("id"))
        if "Function" in query and "RETURN f.address AS address" in query:
            addrs = self._function_addresses()
            if "pinned = false" in query or (
                "f.pinned IS NULL OR f.pinned = false" in query
            ):
                addrs = self._non_pinned_addresses()
            # Mirror the full projection when traversal_order is requested
            # (pass1/ratify dispatchers ORDER BY f.traversal_order).
            if "f.traversal_order AS traversal_order" in query:
                return [{
                    "address": a,
                    "traversal_order": self._node_prop(a, "traversal_order"),
                } for a in sorted(addrs)]
            return [{"address": a} for a in sorted(addrs)]
        if "f.pinned = true" in query or "pinned = true" in query:
            return self._pinned()
        if "RETURN f.name AS name" in query and "{address: $a}" in query:
            node = self._get_function(params.get("a"))
            return [node] if node else []
        if "->(n:" in query and "RETURN n.id AS id" in query and "CONTAINS" in query:
            return self._owned_nodes(params.get("a"), query)
        return []


    # -- internals ------------------------------------------------------------

    def _get(self, node_id):
        if node_id in self._nodes:
            node = self._nodes[node_id]
            return {k: v for k, v in node["props"].items() if k in _PROP_KEYS}
        for node in self._nodes.values():
            props = node["props"]
            if props.get("id") == node_id or props.get("address") == node_id:
                return {k: v for k, v in props.items() if k in _PROP_KEYS}
        return None

    def _get_function(self, address):
        """Return the raw Function node dict for a function address (exit
        alias: legacy searches by address; keeps context reads working)."""
        for node in self._nodes.values():
            props = node["props"]
            if node["label"] == "Function" and props.get("address") == address:
                return {
                    "name": props.get("name"),
                    "llm_name": props.get("llm_name"),
                    "canon_name": props.get("canon_name"),
                }
        return None

    def _node_prop(self, address, prop):
        for node in self._nodes.values():
            props = node["props"]
            if node["label"] == "Function" and props.get("address") == address:
                return props.get(prop)
        return None

    def _owned_nodes(self, func_addr, query):
        """Mirror the ratify for_ratify query:
        MATCH (f:Function {address: $a})-[:CONTAINS]->(n:Variable|Argument)
        WHERE NOT coalesce(n.pinned, false)
        RETURN n.id, n.name, n.llm_name, n.canon_name, n.type, n.ordinal
        ORDER BY n.ordinal
        """
        # the label appears as (:Variable) or (:Argument) in the query
        m = re.search(r"->\(n:(\w+)", query)
        label = m.group(1) if m else "Variable"
        out = []
        for node in self._nodes.values():
            props = node["props"]
            if node["label"] != label:
                continue
            if props.get("pinned"):
                continue
            out.append({
                "id": props.get("id"),
                "name": props.get("name"),
                "llm_name": props.get("llm_name"),
                "canon_name": props.get("canon_name"),
                "type": props.get("type"),
                "ordinal": props.get("ordinal"),
            })
        out.sort(key=lambda r: r.get("ordinal") or 0)
        return out

    def _run_merge(self, query: str, params: dict) -> list[dict]:
        # Support the multi-field rename form (ratify/pass1 merge):
        #   MERGE (n {id: $id}) SET n.llm_name = $llm, n.canon_name = $canon
        multi = re.findall(r"SET\s+n\.(\w+)\s*=\s*\$(\w+)|,\s*n\.(\w+)\s*=\s*\$(\w+)", query)
        multi = [(a or c, b or d) for a, b, c, d in multi]
        node_id = params.get("id")
        # MERGE must UPSERT the existing node by its stable id/address (a bare
        # 'MERGE (n {id: $id})' targets the SAME node across labels).
        entry = None
        if node_id is not None:
            for node in self._nodes.values():
                props = node["props"]
                if props.get("id") == node_id or props.get("address") == node_id:
                    entry = node
                    break
        if entry is None:
            if ":Function" in query:
                entry = self._nodes.setdefault(
                    node_id, {"label": "Function", "props": {"address": node_id}}
                )
            elif ":Variable" in query:
                entry = self._nodes.setdefault(
                    node_id, {"label": "Variable", "props": {"id": node_id}}
                )
            else:
                label = re.search(r":(\w+)", query)
                label = label.group(1) if label else "Function"
                key = "address" if label == "Function" else "id"
                entry = self._nodes.setdefault(
                    node_id, {"label": label, "props": {key: node_id}}
                )
        if multi:
            for field, pname in multi:
                if pname in params:
                    entry["props"][field] = params[pname]
        else:
            match = re.search(r"SET\s+n\.(\w+)\s*=\s*\$value", query)
            field = match.group(1) if match else "llm_name"
            entry["props"][field] = params.get("value")
        self.writes.append((query, dict(params)))
        return []

    def _neighbors(self, node_id):
        seen = set()
        out = []
        for s, t, rtype in self._edges:
            if s == node_id:
                key = (s, t, rtype)
                if key in seen:
                    continue
                seen.add(key)
                out.append({
                    "source": s, "target": t, "type": rtype, "node": self._get(t),
                })
            elif t == node_id:
                key = (node_id, s, rtype)
                if key in seen:
                    continue
                seen.add(key)
                out.append({
                    "source": node_id, "target": s, "type": rtype, "node": self._get(s),
                })
        return out

    def _call_count(self, node_id):
        return sum(
            1 for s, t, rtype in self._edges
            if rtype == "CALL" and (s == node_id or t == node_id)
        )

    def _out_calls(self, node_id):
        out = []
        for s, t, rtype in self._edges:
            if s == node_id and rtype == "CALL":
                node = self._get(t)
                if node:
                    out.append({
                        "id": node.get("address", t),
                        "llm_name": node.get("llm_name"),
                        "canon_name": node.get("canon_name"),
                    })
        return out

    def _in_calls(self, node_id):
        out = []
        for s, t, rtype in self._edges:
            if t == node_id and rtype == "CALL":
                node = self._get(s)
                if node:
                    out.append({
                        "id": node.get("address", s),
                        "llm_name": node.get("llm_name"),
                        "canon_name": node.get("canon_name"),
                    })
        return out

    def _pinned(self):
        out = []
        for node in self._nodes.values():
            props = node["props"]
            if props.get("pinned") is True and node["label"] == "Function":
                out.append({
                    "id": props.get("address"),
                    "llm_name": props.get("llm_name"),
                    "canon_name": props.get("canon_name"),
                })
        return out

    def _function_addresses(self):
        return [
            node["props"]["address"]
            for node in self._nodes.values()
            if node["label"] == "Function"
        ]

    def _non_pinned_addresses(self):
        return [
            node["props"]["address"]
            for node in self._nodes.values()
            if node["label"] == "Function"
            and not node["props"].get("pinned")
        ]


__all__ = ["MemoryNeo4jDriver"]

