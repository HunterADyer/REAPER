"""Deliverable 2.3 — Graph edge builder (dataflow).

Walks HLIL instructions and creates the dataflow edges between the nodes
created by build_nodes (2.2), plus the :REFS_STRING links that were
deferred from 2.2 (they require instruction walking). Edge mapping follows
design-re-stage.md § 2.3; every write is MERGE for idempotency.

Instruction operations are compared against ``_compat.Op`` and the tree is
walked structurally (operand names dest/src/params/condition per
docs/binja-module.md § 6), so this runs identically with or without Binja
installed (against tests/fake_binja.py in the latter case).
"""

from __future__ import annotations

from reaper.tools import _compat

_CALL_OPS = frozenset({_compat.Op.HLIL_CALL, _compat.Op.HLIL_TAILCALL})
_ASSIGN_OPS = frozenset({_compat.Op.HLIL_ASSIGN, _compat.Op.HLIL_VAR_INIT})
_PHI_OPS = frozenset({_compat.Op.HLIL_VAR_PHI, _compat.Op.HLIL_MEM_PHI})
_CONTROL_OPS = frozenset({
    _compat.Op.HLIL_IF, _compat.Op.HLIL_WHILE, _compat.Op.HLIL_DO_WHILE,
    _compat.Op.HLIL_FOR, _compat.Op.HLIL_SWITCH, _compat.Op.HLIL_BLOCK,
    _compat.Op.HLIL_GOTO, _compat.Op.HLIL_BREAK, _compat.Op.HLIL_CONTINUE,
})
# memory/struct accessors that carry an optional .index operand
_MEM_OPS = frozenset({
    _compat.Op.HLIL_ARRAY_INDEX, _compat.Op.HLIL_STRUCT_FIELD,
    _compat.Op.HLIL_DEREF_FIELD,
})


def _hex(address) -> str:
    if isinstance(address, str):
        text = address.strip()
        address = int(text, 16) if text.lower().startswith("0x") else int(text)
    return f"0x{int(address):x}"


def _safe_list(value):
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return list(value)
    return [value]


def _variable_id(func_addr: str, var_name: str, param_map=None) -> str:
    """Stable node id for a variable reference.

    Parameter variables are :Argument nodes keyed by index
    (``{func_addr}:arg{index}``, see build_nodes), so references to them via
    their Binja parameter name must resolve there — never to a non-existent
    :Variable id (which would silently drop the dataflow edge).
    """
    if param_map and var_name in param_map:
        return param_map[var_name]
    return f"{func_addr}:{var_name}"


def _children(expr):
    """Return the child expressions of an instruction (mirrors walk_expr)."""
    if expr is None:
        return []
    op = getattr(expr, "operation", None)
    if op in _compat._BINARY_OPS:
        return [c for c in (getattr(expr, "left", None), getattr(expr, "right", None))
                if c is not None]
    if op in _compat._UNARY_SRC_OPS:
        src = getattr(expr, "src", None)
        return [src] if src is not None else []
    if op == _compat.Op.HLIL_ASSIGN:
        return [c for c in (getattr(expr, "dest", None), getattr(expr, "src", None))
                if c is not None]
    if op in (_compat.Op.HLIL_VAR_INIT, _compat.Op.HLIL_VAR_DECLARE):
        src = getattr(expr, "src", None)
        return [src] if src is not None else []
    if op in (_compat.Op.HLIL_CALL, _compat.Op.HLIL_TAILCALL, _compat.Op.HLIL_INTRINSIC):
        out = [getattr(expr, "dest", None)]
        out.extend(_safe_list(getattr(expr, "params", None)))
        return [c for c in out if c is not None]
    if op == _compat.Op.HLIL_RET:
        return [c for c in _safe_list(getattr(expr, "src", None)) if c is not None]
    if op in _MEM_OPS:
        return [c for c in (getattr(expr, "src", None), getattr(expr, "index", None))
                if c is not None]
    if op in (_compat.Op.HLIL_IF, _compat.Op.HLIL_WHILE, _compat.Op.HLIL_DO_WHILE):
        cond = getattr(expr, "condition", None)
        return [cond] if cond is not None else []
    if op == _compat.Op.HLIL_FOR:
        return [c for c in (getattr(expr, "init", None), getattr(expr, "condition", None),
                            getattr(expr, "update", None)) if c is not None]
    if op == _compat.Op.HLIL_SWITCH:
        cond = getattr(expr, "condition", None)
        return [cond] if cond is not None else []
    operands = getattr(expr, "operands", None)
    out = []
    for item in _safe_list(operands):
        for c in (item if isinstance(item, (list, tuple)) else [item]):
            if c is not None and hasattr(c, "operation"):
                out.append(c)
    return out


def _collect_variable_ids(expr, func_addr: str, param_map=None) -> set:
    """All variable/argument node ids referenced anywhere in ``expr``."""
    ids: set = set()

    def _grab(node):
        if node.operation == _compat.Op.HLIL_VAR:
            name = getattr(getattr(node, "var", None), "name", None)
            if name:
                ids.add(_variable_id(func_addr, name, param_map))

    _compat.walk_expr(expr, _grab)
    return ids


def _string_ref_ids(expr, extractor) -> set:
    """StringRef node ids for HLIL_CONST_PTR leaves that point at strings."""
    bv = getattr(extractor, "bv", None)
    known = {s.start: s for s in getattr(bv, "strings", [])}
    ids: set = set()

    def _grab(node):
        if node.operation == _compat.Op.HLIL_CONST_PTR and node.constant in known:
            ids.add(f"str:{_hex(node.constant)}")

    _compat.walk_expr(expr, _grab)
    return ids


def _resolve_write_targets(expr, func_addr: str, param_map=None) -> list:
    """Resolve an assignment destination to Variable/Argument node id(s).

    A plain HLIL_VAR destination resolves to its node (parameters via
    param_map). A deref / struct-field / array-index destination follows the
    chain ("following the deref chain") back to its base variable — the
    memory the write goes through.
    """
    if expr is None:
        return []
    op = getattr(expr, "operation", None)
    if op is None and getattr(expr, "name", None):
        # Bare Variable object (VAR_INIT/VAR_DECLARE/PHI store the target var
        # directly rather than as an HLIL_VAR leaf instruction).
        return [_variable_id(func_addr, expr.name, param_map)]
    if op == _compat.Op.HLIL_VAR:
        name = getattr(getattr(expr, "var", None), "name", None)
        return [_variable_id(func_addr, name, param_map)] if name else []
    if op in (_compat.Op.HLIL_DEREF, _compat.Op.HLIL_DEREF_FIELD,
              _compat.Op.HLIL_STRUCT_FIELD, _compat.Op.HLIL_ARRAY_INDEX,
              _compat.Op.HLIL_ADDRESS_OF, _compat.Op.HLIL_MEM_PHI):
        return _resolve_write_targets(getattr(expr, "src", None), func_addr, param_map)
    return []


def _call_target_address(callee):
    dest = getattr(callee, "dest", None)
    if dest is not None and getattr(dest, "operation", None) == _compat.Op.HLIL_CONST_PTR:
        return _hex(getattr(dest, "constant", 0))
    return None


async def _emit_edges(instr, *, func_addr: str, param_map=None, hlil, session,
                      extractor) -> None:
    """Emit dataflow edges for one instruction and recurse into its subtree.

    Control-flow instructions emit nothing themselves but are recursed into,
    so assignments/calls/returns hidden inside conditions and loop bodies are
    still captured (bodies referenced by instruction index are resolved
    through ``hlil`` when available, per binja-module § 5.6). Edge endpoints
    are MERGEd on demand so no dataflow is ever silently dropped.
    """
    if instr is None:
        return
    op = getattr(instr, "operation", None)

    # --- assignment: DATAFLOW_ASSIGN source(s) -> destination Variable ------
    if op in _ASSIGN_OPS:
        dest = getattr(instr, "dest", None)
        if dest is None:
            dest = getattr(instr, "var", None)
        src = getattr(instr, "src", None)
        for target in _resolve_write_targets(dest, func_addr, param_map):
            if src is not None:
                for src_id in _collect_variable_ids(src, func_addr, param_map):
                    if src_id != target:
                        await session.run(
                            "MERGE (a {id: $src}) MERGE (b {id: $dst}) "
                            "MERGE (a)-[:DATAFLOW_ASSIGN]->(b)",
                            {"src": src_id, "dst": target})
                for ref_id in _string_ref_ids(src, extractor):
                    await session.run(
                        "MERGE (v {id: $vid}) MERGE (s:StringRef {id: $sid}) "
                        "MERGE (v)-[:REFS_STRING]->(s)",
                        {"vid": target, "sid": ref_id})

    # --- function calls: CALL + DATAFLOW_ARG + REFS_STRING ------------------
    elif op in _CALL_OPS:
        target_addr = _call_target_address(instr)
        call_id = f"{func_addr}:call_{_hex(instr.address)}"
        if target_addr is not None:
            await session.run(
                "MERGE (c:Call {id: $cid}) MERGE (f:Function {address: $fa}) "
                "MERGE (c)-[:CALL]->(f)",
                {"cid": call_id, "fa": target_addr})
        for i, param in enumerate(_safe_list(getattr(instr, "params", None))):
            if target_addr is not None:
                arg_id = f"{target_addr}:arg{i}"
                for src_id in _collect_variable_ids(param, func_addr, param_map):
                    if src_id != arg_id:
                        await session.run(
                            "MERGE (a {id: $src}) MERGE (b:Argument {id: $dst}) "
                            "MERGE (a)-[:DATAFLOW_ARG]->(b)",
                            {"src": src_id, "dst": arg_id})
                for ref_id in _string_ref_ids(param, extractor):
                    await session.run(
                        "MERGE (v:Argument {id: $vid}) "
                        "MERGE (s:StringRef {id: $sid}) "
                        "MERGE (v)-[:REFS_STRING]->(s)",
                        {"vid": arg_id, "sid": ref_id})

    # --- returns: RETURN source(s) -> owning Function -----------------------
    elif op == _compat.Op.HLIL_RET:
        for retval in _safe_list(getattr(instr, "src", None)):
            for src_id in _collect_variable_ids(retval, func_addr, param_map):
                await session.run(
                    "MERGE (v {id: $vid}) MERGE (f:Function {address: $fa}) "
                    "MERGE (v)-[:RETURN]->(f)",
                    {"vid": src_id, "fa": func_addr})

    # --- PHI nodes: DATAFLOW_ASSIGN each SSA source -> phi target -----------
    elif op in _PHI_OPS:
        target = getattr(instr, "var", None) or getattr(instr, "dest", None)
        target_name = getattr(target, "name", None)
        if target_name:
            target_id = _variable_id(func_addr, target_name, param_map)
            for src_id in _collect_variable_ids(instr, func_addr, param_map):
                if src_id != target_id:
                    await session.run(
                        "MERGE (a {id: $src}) MERGE (b {id: $dst}) "
                        "MERGE (a)-[:DATAFLOW_ASSIGN]->(b)",
                        {"src": src_id, "dst": target_id})

    # --- control flow: no edges now, walk condition/init/update -------------
    elif op in _CONTROL_OPS:
        for child in _children(instr):
            await _emit_edges(child, func_addr=func_addr, param_map=param_map,
                              hlil=hlil, session=session, extractor=extractor)
        # Bodies referenced by instruction index (Binja semantics)
        for body_attr in ("true", "false", "body"):
            ref = getattr(instr, body_attr, None)
            for r in _safe_list(ref):
                if isinstance(r, int) and hlil is not None:
                    try:
                        r = hlil[r]
                    except (IndexError, KeyError, TypeError):
                        continue
                await _emit_edges(r, func_addr=func_addr, param_map=param_map,
                                  hlil=hlil, session=session, extractor=extractor)
        return

    # --- generic: recurse; leaves and arithmetic produce no edges -----------
    for child in _children(instr):
        await _emit_edges(child, func_addr=func_addr, param_map=param_map,
                          hlil=hlil, session=session, extractor=extractor)


async def build_edges(extractor, neo4j_driver) -> None:
    """Module-level async function (not a class). See design-re-stage.md § 2.3.

    Walks every function's HLIL and creates dataflow edges between existing
    nodes (created by build_nodes in 2.2) plus :REFS_STRING links. Runs after
    2.2 — it never creates Variable/Argument/Call nodes, only edges.
    """
    async with neo4j_driver.session() as session:
        for meta in extractor.list_functions():
            func = extractor.get_function(address=meta["address"])
            if func is None:
                continue
            hlil = getattr(func, "hlil", None)
            func_addr = _hex(meta["address"])
            # Parameter names -> :Argument node ids (by index).
            param_map = {}
            for p in extractor.get_parameters(meta["address"]):
                param_map[p["name"]] = f"{func_addr}:arg{p['index']}"
            for instr in list(getattr(hlil, "instructions", [])):
                await _emit_edges(instr, func_addr=func_addr, param_map=param_map,
                                  hlil=hlil, session=session, extractor=extractor)


__all__ = ["build_edges"]
