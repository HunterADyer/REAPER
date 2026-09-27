"""Deliverable 2.2 — Graph node builder.

Creates Function, Variable, Argument, Call, and StringRef nodes in Neo4j
from the HLILExtractor's serialized view, plus the structural :CONTAINS
ownership edges (which function owns which nodes). Dataflow edges are the
responsibility of 2.3 (graph_edges.py).

All writes are MERGE (Pattern 2) so a re-run after a crash is idempotent.
Node IDs are STABLE — they are derived from the function address plus the
*original* Binja variable/argument names, so renaming a variable later
(Phase 5) never changes a node's identity (design hard rule).
"""

from __future__ import annotations

from reaper.tools import _compat

_CALL_OPS = frozenset({_compat.Op.HLIL_CALL, _compat.Op.HLIL_TAILCALL})


def _hex(address) -> str:
    if isinstance(address, str):
        text = address.strip()
        address = int(text, 16) if text.lower().startswith("0x") else int(text)
    return f"0x{int(address):x}"


def _iter_call_instructions(function) -> list:
    """Collect every call instruction reachable from func.hlil.instructions.

    Recurses through the tree (walk_expr) so calls nested inside if/loop
    conditions and expressions are found, not just top-level statements.
    """
    calls: list = []
    for instr in function.hlil.instructions:

        def _grab(node):
            if node.operation in _CALL_OPS:
                calls.append(node)

        _compat.walk_expr(instr, _grab)
    return calls


def _call_target_address(callee):
    """Resolve a call instruction's target address, or None if ambiguous.

    Direct calls are HLIL_CONST_PTR (pointer-sized constant). Indirect calls
    (variable / table destinations) are unresolvable statically => None,
    which the caller flags as ambiguous.
    """
    dest = getattr(callee, "dest", None)
    if dest is not None and getattr(dest, "operation", None) == _compat.Op.HLIL_CONST_PTR:
        return _hex(getattr(dest, "constant", 0))
    return None


async def build_nodes(extractor, neo4j_driver) -> None:
    """Module-level async function (not a class). See design-re-stage.md § 2.2.

    Populates Neo4j with Function/Variable/Argument/Call/StringRef nodes and
    the structural :CONTAINS ownership edges between them. Purely additive
    and idempotent (every write is MERGE).
    """
    async with neo4j_driver.session() as session:
        for meta in extractor.list_functions():
            func_addr = _hex(meta["address"])
            func = extractor.get_function(address=meta["address"])

            # 1. Function node
            await session.run(
                "MERGE (f:Function {address: $address}) "
                "SET f.pinned = false, f.ambiguous = false, "
                "f.name = $name, f.size = $size, f.imported = $imported",
                {"address": func_addr, "name": meta["name"],
                 "size": meta["size"],
                 "imported": bool(meta.get("imported", False))},
            )

            # 2. Variables
            for ordinal, var in enumerate(extractor.get_variables(meta["address"])):
                var_id = f"{func_addr}:{var['name']}"
                await session.run(
                    "MERGE (v:Variable {id: $id}) "
                    "SET v.name = $name, v.type = $type, "
                    "v.address = $address, v.source = $source, v.ordinal = $ordinal",
                    {"id": var_id, "name": var["name"], "type": var["type"],
                     "address": func_addr, "source": var.get("source"),
                     "ordinal": ordinal},
                )
                await session.run(
                    "MATCH (f:Function {address: $fa}), (v:Variable {id: $vid}) "
                    "MERGE (f)-[:CONTAINS]->(v)",
                    {"fa": func_addr, "vid": var_id},
                )

            # 3. Parameters -> Argument nodes
            for param in extractor.get_parameters(meta["address"]):
                arg_id = f"{func_addr}:arg{param['index']}"
                await session.run(
                    "MERGE (a:Argument {id: $id}) "
                    "SET a.name = $name, a.type = $type, a.address = $address, "
                    "a.ordinal = $ordinal",
                    {"id": arg_id, "name": param["name"], "type": param["type"],
                     "address": func_addr, "ordinal": param["index"]},
                )
                await session.run(
                    "MATCH (f:Function {address: $fa}), (a:Argument {id: $aid}) "
                    "MERGE (f)-[:CONTAINS]->(a)",
                    {"fa": func_addr, "aid": arg_id},
                )

            # 4. StringRef nodes (shared/global: no :CONTAINS parent)
            for ref in extractor.get_string_refs(meta["address"]):
                str_addr = _hex(ref["address"])
                await session.run(
                    "MERGE (s:StringRef {id: $id}) "
                    "SET s.value = $value, s.address = $address",
                    {"id": f"str:{str_addr}", "value": ref["value"],
                     "address": str_addr},
                )

            # 5. Call nodes (structural only; dataflow edges in 2.3)
            if func is None:
                continue
            for callee in _iter_call_instructions(func):
                call_id = f"{func_addr}:call_{_hex(callee.address)}"
                ambiguous = _call_target_address(callee) is None
                await session.run(
                    "MERGE (c:Call {id: $id}) "
                    "SET c.address = $address, c.ambiguous = $ambiguous",
                    {"id": call_id, "address": _hex(callee.address),
                     "ambiguous": ambiguous},
                )
                await session.run(
                    "MATCH (f:Function {address: $fa}), (c:Call {id: $cid}) "
                    "MERGE (f)-[:CONTAINS]->(c)",
                    {"fa": func_addr, "cid": call_id},
                )


__all__ = ["build_nodes"]
