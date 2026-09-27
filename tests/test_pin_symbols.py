"""Tests for Deliverable 2.4 — pin_symbols (Pass -1 symbol preservation).

Verifies that known symbols (imports, library matches, non-sub_ named
functions) are pinned on their Neo4j Function nodes with a canonical name,
that auto-named sub_ functions are NOT pinned, and that every StringRef node
is pinned too. All writes are MATCH+SET (idempotent).
"""

from __future__ import annotations

import pytest

from reaper.tools.pin_symbols import pin_symbols

from conftest import RecordingNeo4jDriver
from fake_binja import FakeExtractor, FakeFunction, FakeSymbol, SymbolType


def _make_extractor():
    funcs = []
    defines = [
        (0x2000, "malloc", SymbolType.ImportedFunctionSymbol),
        (0x3000, "strlen", SymbolType.ImportedFunctionSymbol),
        (0x4000, "setup_config", SymbolType.FunctionSymbol),
        (0x5000, "sub_5000", SymbolType.FunctionSymbol),  # auto-named -> skip
        (0x6000, "printf", SymbolType.LibraryFunctionSymbol),
    ]
    for addr, name, stype in defines:
        f = FakeFunction.simple(addr, name)
        f.symbol = FakeSymbol(name, addr, stype)
        funcs.append(f)
    return FakeExtractor.with_functions(funcs)


def _pinned(driver):
    return {q[1]["address"]: q[1]["name"]
            for q in driver.queries_matching("SET f.pinned = true")}


@pytest.mark.asyncio
async def test_pins_known_symbols_only():
    driver = RecordingNeo4jDriver()
    await pin_symbols(_make_extractor(), driver)

    pinned = _pinned(driver)
    assert pinned == {
        "0x2000": "malloc",
        "0x3000": "strlen",
        "0x4000": "setup_config",
        "0x6000": "printf",
    }
    # auto-named sub_ function is NOT pinned
    assert "0x5000" not in pinned


@pytest.mark.asyncio
async def test_pin_query_sets_canonical_names():
    driver = RecordingNeo4jDriver()
    await pin_symbols(_make_extractor(), driver)

    malloc_q = [q for q in driver.queries if q[1].get("address") == "0x2000"]
    assert malloc_q
    query, params = malloc_q[0]
    assert "SET f.pinned = true" in query
    assert 'f.llm_name = $name, f.canon_name = $name' in query
    assert params["name"] == "malloc"
    assert params["canon_name"] == "malloc" if "canon_name" in params else True


@pytest.mark.asyncio
async def test_pins_all_stringref_nodes():
    driver = RecordingNeo4jDriver()
    await pin_symbols(_make_extractor(), driver)

    str_q = [q for q in driver.queries if "StringRef" in q[0]]
    assert str_q
    assert "MATCH (s:StringRef) SET s.pinned = true" in str_q[0][0]


@pytest.mark.asyncio
async def test_writes_are_match_set_not_create():
    driver = RecordingNeo4jDriver()
    await pin_symbols(_make_extractor(), driver)
    assert driver.queries
    assert all("MATCH" in q[0] or "StringRef" in q[0] for q in driver.queries)
    assert not any("CREATE" in q[0] for q in driver.queries)
