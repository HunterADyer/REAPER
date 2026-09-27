"""Deliverable 2.4 — Symbol preservation (Pass -1).

Before any LLM pass, identify and pin known symbols so the pipeline never
wastes effort renaming them. Sources (via the live BinaryView):

* import table          — ``SymbolType.ImportedFunctionSymbol``
* debug/exported names  — ``SymbolType.FunctionSymbol`` that don't start
                          with ``sub_``
* signature libraries   — ``SymbolType.LibraryFunctionSymbol``

Known symbols are pinned on their Neo4j Function node (pinned=true and a
canonical name) and every StringRef node is inherently pinned. Pinned
functions are excluded from rename passes (5.x) and treated as leaves in
traversal ordering (2.5).
"""

from __future__ import annotations

from reaper.tools import _compat


def _hex(address) -> str:
    if isinstance(address, str):
        text = address.strip()
        address = int(text, 16) if text.lower().startswith("0x") else int(text)
    return f"0x{int(address):x}"


def _collect_known_symbols(bv) -> dict:
    """Return {address: canonical_name} for every known/pinned symbol, with
    deterministic precedence: imports, then library matches, then named
    functions. Later sources never overwrite earlier (setdefault)."""
    known: dict = {}
    for sym in bv.get_symbols_of_type(_compat.SymbolType.ImportedFunctionSymbol):
        known.setdefault(sym.address, sym.name)
    for sym in bv.get_symbols_of_type(_compat.SymbolType.LibraryFunctionSymbol):
        known.setdefault(sym.address, sym.name)
    for sym in bv.get_symbols_of_type(_compat.SymbolType.FunctionSymbol):
        name = getattr(sym, "name", "")
        if not name.startswith("sub_"):
            known.setdefault(sym.address, name)
    return known


async def pin_symbols(extractor, neo4j_driver) -> None:
    """Module-level async function (not a class). See design-re-stage.md § 2.4.

    Updates Function nodes for known symbols (pinned=true, canonical name)
    and marks all StringRef nodes pinned. Runs against an already-populated
    graph (after 2.2/2.3). Idempotent — MATCH+SET only.
    """
    known = _collect_known_symbols(extractor.bv)
    async with neo4j_driver.session() as session:
        for address, name in known.items():
            await session.run(
                "MATCH (f:Function {address: $address}) "
                "SET f.pinned = true, f.llm_name = $name, f.canon_name = $name",
                {"address": _hex(address), "name": name},
            )
        # StringRef nodes are inherently pinned.
        await session.run("MATCH (s:StringRef) SET s.pinned = true")


__all__ = ["pin_symbols"]
