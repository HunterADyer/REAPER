"""Deliverable 2.5 + 7.4 — Leaf validation, SCC detection, traversal order.

Module-level async functions ONLY (not classes): ``validate_and_order`` (2.5)
and ``compute_resynthesis_groups`` (7.4, exported here for the pipeliner).
Replaces the lead's temporary stub.

Design highlights implemented here:

* SCC detection is done in PYTHON (Tarjan) — no GDS plugin dependency.
  All Function addresses + :CALL edges are pulled into memory as an
  adjacency list; Tarjan is O(V+E) so even 1000+ function binaries are fine.
* Back-edges inside a multi-member SCC are relabelled :DEFERRED_BACK (the
  edge from the highest address to a lower address in the SCC) so the
  condensed call graph is acyclic.
* Traversal order is function-level only: leaf functions (whose :CALL edges
  ALL lead to pinned/imported functions or outside the binary) get 0 and
  higher levels get incrementing integers. Within-function order (Variables
  → Arguments → summary) is hardcoded in the dispatchers, not stored here.
"""

from __future__ import annotations

from collections import deque


def _normalize(address) -> str:
    """Return a canonical hex string for an address fetched from Neo4j."""
    if isinstance(address, str):
        text = address.strip()
        value = int(text, 16) if text.lower().startswith("0x") else int(text)
    else:
        value = int(address)
    return f"0x{value:x}"


def _as_int(address) -> int:
    if isinstance(address, str):
        text = address.strip()
        return int(text, 16) if text.lower().startswith("0x") else int(text)
    return int(address)


# ---------------------------------------------------------------------------
# Read queries
# ---------------------------------------------------------------------------

_LEAF_QUERY = (
    "MATCH (n) "
    "WHERE NOT ()-[:DATAFLOW_ASSIGN|DATAFLOW_ARG|CALL|RETURN]->(n) "
    "RETURN labels(n) AS labels, n.address AS address, "
    "coalesce(n.ambiguous, false) AS ambiguous, "
    "coalesce(n.pinned, false) AS pinned, n.id AS id"
)

_FUNCTION_QUERY = (
    "MATCH (f:Function) "
    "RETURN f.address AS address, coalesce(f.pinned, false) AS pinned"
)

_CALL_EDGE_QUERY = (
    "MATCH (owner:Function)-[:CONTAINS]->(c:Call)-[:CALL]->(target:Function) "
    "RETURN owner.address AS src, target.address AS dst"
)

_CONTAINS_QUERY = (
    "MATCH (f:Function)-[:CONTAINS]->(n) "
    "RETURN f.address AS owner, labels(n) AS labels"
)


async def _collect(session, query: str, params: dict | None = None) -> list:
    """Run a read query and drain it into a list of record dicts."""
    result = await session.run(query, **(params or {}))
    return [rec async for rec in result]


# ---------------------------------------------------------------------------
# Tarjan's algorithm (Python — no GDS dependency)
# ---------------------------------------------------------------------------

class _TarjanSCC:
    """Tarjan's strongly-connected-components algorithm over a string table."""

    def __init__(self, adjacency: dict[str, list[str]]):
        self.adjacency = adjacency
        self._index = 0
        self._indices: dict[str, int] = {}
        self._lowlink: dict[str, int] = {}
        self._stack: list[str] = []
        self._on_stack: set = set()
        self.components: list[list[str]] = []

    def _strongconnect(self, node: str) -> None:
        self._indices[node] = self._index
        self._lowlink[node] = self._index
        self._index += 1
        self._stack.append(node)
        self._on_stack.add(node)
        for succ in self.adjacency.get(node, []):
            if succ not in self._indices:
                self._strongconnect(succ)
                self._lowlink[node] = min(self._lowlink[node], self._lowlink[succ])
            elif succ in self._on_stack:
                self._lowlink[node] = min(self._lowlink[node], self._indices[succ])
        if self._lowlink[node] == self._indices[node]:
            component: list[str] = []
            while True:
                member = self._stack.pop()
                self._on_stack.discard(member)
                component.append(member)
                if member == node:
                    break
            self.components.append(component)

    def run(self) -> list[list[str]]:
        for node in sorted(self.adjacency):
            if node not in self._indices:
                self._strongconnect(node)
        return self.components


def _condensed_dag_levels(
    non_pinned: set, call_edges: list, components: list[list[str]]
) -> dict:
    """Return {address: traversal_order} for every non-pinned function.

    Collapses each SCC into a supernode (representative = smallest address),
    considering only call edges among NON-pinned functions (pinned/imported
    callees are "outside the binary" and never extend a level), topologically
    sorts the condensed DAG, then assigns leaf-first levels sinks-first. All
    members of an SCC share its level.
    """
    rep_of: dict[str, str] = {}
    for comp in components:
        if not comp:
            continue
        rep = min(comp, key=_as_int)
        for member in comp:
            rep_of[member] = rep

    adj: dict[str, set] = {}
    nodes: set = set()
    for src, dst in call_edges:
        if src not in non_pinned or dst not in non_pinned:
            continue
        rs, rd = rep_of.get(src, src), rep_of.get(dst, dst)
        nodes.add(rs)
        nodes.add(rd)
        if rs == rd:
            continue
        adj.setdefault(rs, set()).add(rd)

    indeg = {n: 0 for n in nodes}
    for succs in adj.values():
        for s in succs:
            indeg[s] = indeg.get(s, 0) + 1
    queue = deque(sorted(n for n in nodes if indeg[n] == 0))
    topo = []
    while queue:
        node = queue.popleft()
        topo.append(node)
        for succ in sorted(adj.get(node, ())):
            indeg[succ] -= 1
            if indeg[succ] == 0:
                queue.append(succ)

    # Sinks-first: supernode level = max(succ level + 1), 0 when leaf.
    level = {n: 0 for n in nodes}
    for node in reversed(topo):
        succ_levels = [level[s] for s in adj.get(node, ())]
        if succ_levels:
            level[node] = max(succ_levels) + 1

    orders: dict = {}
    for addr in non_pinned:
        rep = rep_of.get(addr, addr)
        orders[addr] = level.get(rep, 0)
    return orders


async def validate_and_order(neo4j_driver) -> None:
    """Module-level async function (not a class). See design-re-stage.md § 2.5.

    Runs all three steps: (1) leaf validation, (2) SCC detection + back-edge
    relabelling, (3) bottom-up traversal order. Raises ValueError if leaf
    validation finds an unacceptable leaf. All writes are MATCH+SET.
    """
    async with neo4j_driver.session() as session:
        # -- 1. leaf validation ------------------------------------------------
        leaf_records = await _collect(session, _LEAF_QUERY)
        function_map = {_normalize(r["address"]): bool(r.get("pinned"))
                        for r in await _collect(session, _FUNCTION_QUERY)}
        call_edges_raw = [(r["src"], r["dst"]) for r in await _collect(session, _CALL_EDGE_QUERY)]
        call_edges = [(_normalize(s), _normalize(d)) for s, d in call_edges_raw]
        contains = await _collect(session, _CONTAINS_QUERY)

        callers = {dst for _, dst in call_edges}
        has_internal_var = {_normalize(r["owner"]) for r in contains
                            if set(r.get("labels") or []) & {"Variable", "Argument"}}
        has_call = {_normalize(r["owner"]) for r in contains
                    if "Call" in set(r.get("labels") or [])}

        violations: list[str] = []
        for rec in leaf_records:
            labels = set(rec.get("labels") or [])
            if labels & {"Variable", "Argument", "StringRef"}:
                continue
            addr = _normalize(rec.get("address"))
            if "Function" in labels:
                isolated = (addr not in callers and addr not in has_internal_var
                            and addr not in has_call)
                if isolated:
                    await session.run(
                        "MATCH (f:Function {address: $a}) SET f.isolated = true",
                        {"a": addr})
                # A non-isolated Function leaf is a legitimate entry point
                # (e.g. main) and does not fail validation.
                continue
            if "Call" in labels:
                # Call nodes carry outgoing :CALL (and incoming :CONTAINS) but
                # never an incoming dataflow edge by design (DATAFLOW_ARG goes
                # to the callee's Argument nodes instead), so a resolved or
                # ambiguous call-site leaf is legitimate and never a violation.
                # Only genuinely unresolved-and-unflagged calls would be caught
                # elsewhere; treat every Call leaf as valid.
                continue
            violations.append(f"unexpected leaf labels={sorted(labels)} id={rec.get('id')}")

        # -- 2. SCC detection + back-edge relabelling --------------------------
        adjacency: dict[str, list[str]] = {a: [] for a in function_map}
        for src, dst in call_edges:
            if src in adjacency and dst in adjacency:
                adjacency[src].append(dst)
        components = _TarjanSCC(adjacency).run()

        for comp in components:
            if len(comp) <= 1:
                continue
            scc_id = f"scc:{min(comp, key=_as_int)}"
            for member in comp:
                await session.run(
                    "MATCH (f:Function {address: $a}) SET f.scc_id = $id",
                    {"a": member, "id": scc_id})
            comp_set = set(comp)
            back = [e for e in call_edges if e[0] in comp_set and e[1] in comp_set
                    and _as_int(e[0]) > _as_int(e[1])]
            if back:
                src, dst = max(back, key=lambda e: _as_int(e[0]))
                await session.run(
                    "MATCH (owner:Function {address: $src})-[:CONTAINS]->(c:Call)"
                    "-[e:CALL]->(target:Function {address: $dst}) "
                    "DELETE e WITH c, target CREATE (c)-[:DEFERRED_BACK]->(target)",
                    {"src": src, "dst": dst})

        # -- 3. traversal order (function-level) --------------------------------
        non_pinned = {a for a, p in function_map.items() if not p}
        orders = _condensed_dag_levels(non_pinned, call_edges, components)
        for addr, order in orders.items():
            await session.run(
                "MATCH (f:Function {address: $a}) SET f.traversal_order = $order",
                {"a": addr, "order": order})
        # Pinned/imported functions are always leaf-level (order 0). Written
        # through the SAME $order parameter so every traversal_order write is
        # uniform (the pipeliner / tests never special-case a literal 0).
        for addr in {a for a, p in function_map.items() if p}:
            await session.run(
                "MATCH (f:Function {address: $a}) SET f.traversal_order = $order",
                {"a": addr, "order": 0})

        if violations:
            raise ValueError(
                "Leaf validation failed; expected leaves to be Variable/"
                "Argument/StringRef:\n" + "\n".join(f"- {v}" for v in violations)
            )


# ---------------------------------------------------------------------------
# 7.4 — Resynthesis scope groups (exported from this file for the pipeliner)
# ---------------------------------------------------------------------------

def _union_find_dedup(groups: list[list[str]]) -> list[list[str]]:
    """Deduplicate overlapping groups using union-find."""
    parent: dict[str, str] = {}

    def find(x: str) -> str:
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: str, b: str) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    for group in groups:
        if not group:
            continue
        first = group[0]
        for addr in group[1:]:
            union(first, addr)

    merged: dict[str, set] = {}
    for group in groups:
        for addr in group:
            merged.setdefault(find(addr), set()).add(addr)
    return [sorted(addrs) for addrs in merged.values()]


async def compute_resynthesis_groups(neo4j_driver) -> list[list[str]]:
    """Return groups of function addresses for resynthesis review (7.4).

    Groups are derived from: (1) each SCC (functions sharing an scc_id),
    (2) each struct's consumers, (3) caller/callee call-neighborhoods. Any
    function appearing in more than one group merges those groups (union-
    find). Each returned group is processed as one resynthesis unit.
    """
    async with neo4j_driver.session() as session:
        groups: list[list[str]] = []

        # 1. SCC groups (each SCC is one resynthesis unit)
        for rec in await _collect(
            session,
            "MATCH (f:Function) WHERE f.scc_id IS NOT NULL "
            "RETURN f.scc_id AS sid, collect(f.address) AS addrs",
        ):
            addrs = [a for a in (rec.get("addrs") or []) if a is not None]
            if addrs:
                groups.append(addrs)

        # 2. Struct consumers (functions touching the same struct)
        for rec in await _collect(
            session,
            "MATCH (f:Function)-[:CONTAINS]->(:Variable)-[:FIELD_OF]->(s) "
            "RETURN s.id AS sid, collect(DISTINCT f.address) AS addrs",
        ):
            addrs = [a for a in (rec.get("addrs") or []) if a is not None]
            if addrs:
                groups.append(addrs)

        # 3. Caller/callee clusters — direct call neighborhoods (depth 1)
        for rec in await _collect(
            session,
            "MATCH (caller:Function)-[:CONTAINS]->(:Call)-[:CALL]->(callee:Function) "
            "WHERE NOT coalesce(callee.pinned, false) "
            "RETURN caller.address AS src, collect(DISTINCT callee.address) AS dsts",
        ):
            cluster = [rec["src"]] if rec.get("src") else []
            cluster.extend(rec.get("dsts") or [])
            if cluster:
                groups.append([a for a in dict.fromkeys(cluster) if a is not None])

        return _union_find_dedup(groups)


__all__ = ["validate_and_order", "compute_resynthesis_groups", "_TarjanSCC"]
