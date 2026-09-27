"""Tests for Deliverable 3.2 — ShadowCopyManager (reaper/harness/shadow.py).

Runs the design § 3.2 scenario against a stateful in-memory Neo4j double:
checkout → rename in a copy → diff() → apply() writes to master, and the
two-checkout case where the LATTER diff() reports a CONFLICT because master
advanced. Also covers N-hop edge collection and claim mutations.
"""

from __future__ import annotations

import pytest

from reaper.harness.shadow import ShadowCopyManager


class _Rows:
    """Minimal async-iterable / awaitable of records (mirrors neo4j)."""

    def __init__(self, records):
        self._records = list(records)

    def __await__(self):
        if False:  # pragma: no cover
            yield
        return self

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            return self._records.pop(0)
        except IndexError:
            raise StopAsyncIteration


class StatefulNeo4j:
    """In-memory driver with REAL state, so apply() truly mutates master."""

    def __init__(self, nodes=None, edges=None):
        self.nodes = {k: dict(v) for k, v in (nodes or {}).items()}
        self.edges = [(str(s), str(t), str(ty)) for s, t, ty in (edges or [])]
        self.queries = []

    def session(self):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def _field_name(self, query: str) -> str:
        return query.split("SET n.", 1)[1].split(" ", 1)[0].strip()

    def run(self, query: str, **params):
        self.queries.append(query)
        nid = params.get("id")
        if query.startswith("MATCH (n) WHERE n.id = $id OR n.address = $id"):
            if nid in self.nodes:
                return _Rows([{"n": dict(self.nodes[nid])}])
            return _Rows([])
        if "RETURN a.id AS source, b.id AS target" in query:
            out = []
            for (s, t, ty) in self.edges:
                if s == nid and t in self.nodes:
                    out.append({"source": s, "target": t, "type": ty,
                                "node": dict(self.nodes[t])})
                elif t == nid and s in self.nodes:
                    out.append({"source": s, "target": t, "type": ty,
                                "node": dict(self.nodes[s])})
            return _Rows(out)
        if query.startswith("MERGE (n:Function {address: $id})"):
            field = self._field_name(query)
            self.nodes.setdefault(nid, {"address": nid})
            self.nodes[nid][field] = params["value"]
            return _Rows([])
        if query.startswith("MERGE (n:Variable {id: $id})"):
            field = self._field_name(query)
            self.nodes.setdefault(nid, {"id": nid})
            self.nodes[nid][field] = params["value"]
            return _Rows([])
        return _Rows([])


class StubLedger:
    def __init__(self, claims=None):
        self._claims = claims or {}
        self.added_claims = []
        self._functions = {}

    async def get_claims(self, addr):
        return list(self._claims.get(str(addr), []))

    async def get_function(self, addr):
        return dict(self._functions.get(str(addr), {}))

    async def set_function_names(self, addr, llm, canon):
        self._functions[str(addr)] = {"address": str(addr),
                                      "llm_name": llm, "canon_name": canon}

    async def add_claim(self, function_address, claim_text, submitted_by, evidence):
        self.added_claims.append({
            "function_address": function_address, "claim_text": claim_text,
            "submitted_by": submitted_by, "evidence": evidence,
        })
        return 99


_HOP_CONFIG = {"limits": {"shadow_copy_hops": 1}}


def _scenario_driver():
    """A two-function + one variable neighborhood for checkout tests."""
    return StatefulNeo4j(
        nodes={
            "0x1000": {"address": "0x1000"},
            "0x2000": {"address": "0x2000"},
            "0x1000:var_18": {"id": "0x1000:var_18", "llm_name": None},
        },
        edges=[
            ("0x1000", "0x1000:var_18", "CONTAINS"),
            ("0x1000", "0x2000", "CALL"),
        ],
    )


@pytest.mark.asyncio
async def test_diff_shows_rename_and_apply_writes_to_master():
    driver = _scenario_driver()
    ledger = StubLedger()
    shadow = ShadowCopyManager(driver, ledger, _HOP_CONFIG)

    await shadow.checkout("0x1000", "sess_a")
    modified = {"nodes": {"0x1000:var_18": {"llm_name": "input_data"}}, "claims": []}
    result = await shadow.diff("sess_a", modified)
    assert not result["conflicts"]
    assert len(result["mutations"]) == 1
    assert result["mutations"][0]["node_id"] == "0x1000:var_18"

    await shadow.apply("sess_a", result["mutations"])
    assert driver.nodes["0x1000:var_18"]["llm_name"] == "input_data"
    assert shadow.current_version() == 1


@pytest.mark.asyncio
async def test_claim_mutations_always_safe_and_applied_to_ledger():
    driver = _scenario_driver()
    ledger = StubLedger()
    shadow = ShadowCopyManager(driver, ledger, _HOP_CONFIG)

    await shadow.checkout("0x1000", "sess_c")
    modified = {
        "nodes": {},
        "claims": [{
            "kind": "claim", "function_address": "0x1000",
            "claim_text": "this function parses json",
            "submitted_by": "sess_c", "evidence": [],
        }],
    }
    result = await shadow.diff("sess_c", modified)
    # claims never conflict — they are pure additions
    assert not result["conflicts"]
    await shadow.apply("sess_c", result["mutations"])
    assert len(ledger.added_claims) == 1
    assert ledger.added_claims[0]["claim_text"] == "this function parses json"
    assert shadow.current_version() == 1


@pytest.mark.asyncio
async def test_two_checkouts_second_diff_reports_conflict():
    """Design § 3.2 scenario: both rename the same variable; the first applies,
    so the SECOND diff() must report a CONFLICT (master advanced)."""
    driver = _scenario_driver()
    ledger = StubLedger()
    shadow = ShadowCopyManager(driver, ledger, _HOP_CONFIG)

    await shadow.checkout("0x1000", "sess_one")
    await shadow.checkout("0x1000", "sess_two")

    d1 = await shadow.diff("sess_one", {"nodes": {"0x1000:var_18": {"llm_name": "aaa"}},
                                        "claims": []})
    assert not d1["conflicts"]
    await shadow.apply("sess_one", d1["mutations"])
    assert shadow.current_version() == 1

    d2 = await shadow.diff("sess_two", {"nodes": {"0x1000:var_18": {"llm_name": "bbb"}},
                                        "claims": []})
    assert len(d2["conflicts"]) == 1
    conflict = d2["conflicts"][0]
    assert conflict["node_id"] == "0x1000:var_18"
    assert conflict["field"] == "llm_name"
    assert conflict["value"] == "bbb"
    assert conflict["master_value"] == "aaa"
    assert conflict["checkout_value"] is None


@pytest.mark.asyncio
async def test_discard_removes_checkout():
    driver = _scenario_driver()
    shadow = ShadowCopyManager(driver, StubLedger(), _HOP_CONFIG)
    await shadow.checkout("0x1000", "sess_d")
    assert shadow.get_checkout("sess_d") is not None
    shadow.discard("sess_d")
    assert shadow.get_checkout("sess_d") is None


@pytest.mark.asyncio
async def test_diff_without_checkout_raises_key_error():
    shadow = ShadowCopyManager(_scenario_driver(), StubLedger(), _HOP_CONFIG)
    with pytest.raises(KeyError, match="no checkout"):
        await shadow.diff("missing", {"nodes": {}, "claims": []})
