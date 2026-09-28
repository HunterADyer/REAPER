"""Struct access detector — Deliverable 4.1.

Scans HLIL for pointer+offset patterns indicating struct field accesses.
Pure analysis — no LLM. Handles the walk patterns and grouping strategy from
design § 4.1 (details inline). ``StructCandidate`` / ``FieldAccess`` are the
ONLY agent-output models living OUTSIDE ``reaper/harness/submission.py``;
do not move them.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from reaper.tools._compat import (
    Op,
    _ARRAY_OPS,
    _BINARY_OPS,
    _UNARY_SRC_OPS,
    _safe_list,
)


class FieldAccess(BaseModel):
    """A single struct field access site discovered in HLIL (design § 4.1)."""

    offset: int
    size: int
    access_type: str = Field(description="'read' or 'write'")
    function_address: str
    instruction_address: str
    base_var: str = Field(
        default="",
        description="name of the base variable whose field is accessed; used "
        "to bind the recovered struct pointer type back onto the variable",
    )


class StructCandidate(BaseModel):
    """Group of struct field accesses that likely share one struct type."""

    candidate_id: str
    base_type_hint: str
    accesses: list[FieldAccess]
    functions_involved: list[str]
    base_names: dict[str, list[str]] = Field(
        default_factory=dict,
        description="function_address -> base variable names bound to this "
        "candidate; the re-apply step retags these bases with the recovered "
        "struct pointer type",
    )
    overlap_hint: bool = Field(
        default=False,
        description="two accesses alias the same byte range at the same "
        "offset with different sizes — may indicate a union",
    )


def _hex(address) -> str:
    if isinstance(address, str):
        text = address.strip()
        value = int(text, 16) if text.lower().startswith("0x") else int(text)
    else:
        value = int(address)
    return f"0x{value:x}"


def _type_size(type_str: str) -> int:
    t = (type_str or "").strip()
    if t.endswith("*") or "[" in t or t in ("void *", "void*"):
        return 8
    base = t.rstrip("*").strip()
    if base in ("char", "int8_t", "uint8_t", "bool", "byte"):
        return 1
    if base in ("int16_t", "uint16_t", "short", "wchar_t"):
        return 2
    if base in ("int32_t", "uint32_t", "int", "unsigned int", "float"):
        return 4
    return 8  # int64 / pointers / unknown


def _root_var(expr):
    """Return the root Variable for a base-expression chain, or None."""
    seen = set()
    while expr is not None and id(expr) not in seen:
        seen.add(id(expr))
        if expr.operation == Op.HLIL_VAR:
            return getattr(expr, "var", None)
        if expr.operation in (Op.HLIL_DEREF, Op.HLIL_ADDRESS_OF,
                              Op.HLIL_LOW_PART, Op.HLIL_ZX, Op.HLIL_SX):
            expr = getattr(expr, "src", None)
            continue
        if expr.operation in _ARRAY_OPS:
            expr = getattr(expr, "src", None)
            continue
        if expr.operation in _BINARY_OPS:
            left = getattr(expr, "left", None)
            right = getattr(expr, "right", None)
            for cand in (left, right):
                if cand is not None and cand.operation == Op.HLIL_CONST:
                    continue
                rv = _root_var(cand)
                if rv is not None:
                    return rv
            return None
        return None
    return None


class StructAccessDetector:
    """Scan HLIL for pointer+offset patterns indicating struct field accesses.

    Pure analysis — no LLM (design § 4.1). Walk patterns handled:
      - ``*(base + offset)``  — HLIL_DEREF of HLIL_ADD with a constant offset
      - ``base->field``       — HLIL_STRUCT_FIELD / HLIL_DEREF_FIELD
      - ``*base``             — plain deref recorded at offset 0
    Grouping follows the design's conservative strategy: WITHIN a function by
    base variable; ACROSS functions ONLY when Binja assigns the same type;
    unknown-typed bases never bridge functions.
    """

    def __init__(self, extractor):
        self.extractor = extractor
        self._found: list[tuple[str, str, FieldAccess]] = []
        self._functions_by_addr: dict[str, object] = {}

    # ---------------------------------------------------------------- #
    # Access recording
    # ---------------------------------------------------------------- #

    def _record(self, func_addr, base_expr, access: dict, instruction_addr) -> None:
        base_var = _root_var(base_expr)
        if base_var is None or not getattr(base_var, "name", None):
            return
        self._found.append((func_addr, base_var.name, FieldAccess(
            offset=int(access.get("offset", 0)),
            size=int(access.get("size", 8)),
            access_type=access.get("access_type", "read"),
            function_address=func_addr,
            instruction_address=instruction_addr,
            base_var=base_var.name,
        )))

    def _deref_access(self, expr, func_addr, write) -> None:
        """``*(base [+ N])`` — HLIL_DEREF (patterns 1 and 3)."""
        src = getattr(expr, "src", None)
        base_expr, offset = src, 0
        if src is not None and src.operation in (Op.HLIL_ADD, Op.HLIL_SUB):
            left, right = getattr(src, "left", None), getattr(src, "right", None)
            const_side = None
            for cand in (left, right):
                if cand is not None and cand.operation == Op.HLIL_CONST:
                    const_side, offset = cand, int(cand.constant)
            base_expr = (left if const_side is right else right) \
                if const_side is not None else src
        self._record(func_addr, base_expr,
                     {"offset": offset, "size": self._access_size(expr),
                      "access_type": "write" if write else "read"},
                     _hex(getattr(expr, "address", 0)))

    def _field_access(self, expr, func_addr, write) -> None:
        """``base -> field`` — STRUCT_FIELD / DEREF_FIELD (pattern 2)."""
        offset = getattr(expr, "offset", None)
        if offset is None:
            index = getattr(expr, "index", None)
            offset = int(index) if index is not None else 0
        self._record(func_addr, getattr(expr, "src", None),
                     {"offset": int(offset), "size": self._access_size(expr),
                      "access_type": "write" if write else "read"},
                     _hex(getattr(expr, "address", 0)))

    def _access_size(self, expr) -> int:
        for attr in ("type_ref", "var"):
            ref = getattr(expr, attr, None)
            if ref is None:
                continue
            t = getattr(ref, "type", None)
            if t is not None:
                size = _type_size(str(t))
                if size:
                    return size
        return 8

    # ---------------------------------------------------------------- #
    # Context-aware walker (carries the read/write flag for access_type)
    # ---------------------------------------------------------------- #

    def _walk(self, expr, func_addr, write) -> None:
        if expr is None or not hasattr(expr, "operation"):
            return
        op = expr.operation
        if op == Op.HLIL_DEREF:
            self._deref_access(expr, func_addr, write)
        elif op in (Op.HLIL_STRUCT_FIELD, Op.HLIL_DEREF_FIELD):
            self._field_access(expr, func_addr, write)

        if op in _BINARY_OPS:
            self._walk(getattr(expr, "left", None), func_addr, False)
            self._walk(getattr(expr, "right", None), func_addr, False)
        elif op == Op.HLIL_ASSIGN:
            self._walk(getattr(expr, "dest", None), func_addr, True)
            self._walk(getattr(expr, "src", None), func_addr, False)
        elif op in (Op.HLIL_VAR_INIT, Op.HLIL_VAR_DECLARE):
            self._walk(getattr(expr, "src", None), func_addr, False)
        elif op in (Op.HLIL_CALL, Op.HLIL_TAILCALL, Op.HLIL_INTRINSIC):
            self._walk(getattr(expr, "dest", None), func_addr, False)
            for p in _safe_list(getattr(expr, "params", None)):
                self._walk(p, func_addr, False)
        elif op == Op.HLIL_RET:
            for val in _safe_list(getattr(expr, "src", None)):
                self._walk(val, func_addr, False)
        elif op in _ARRAY_OPS:
            self._walk(getattr(expr, "src", None), func_addr, write)
            self._walk(getattr(expr, "index", None), func_addr, False)
        elif op in _UNARY_SRC_OPS:
            self._walk(getattr(expr, "src", None), func_addr, False)
        elif op == Op.HLIL_IF:
            self._walk(getattr(expr, "condition", None), func_addr, False)
        elif op in (Op.HLIL_WHILE, Op.HLIL_DO_WHILE):
            self._walk(getattr(expr, "condition", None), func_addr, False)
        elif op == Op.HLIL_FOR:
            self._walk(getattr(expr, "init", None), func_addr, False)
            self._walk(getattr(expr, "condition", None), func_addr, False)
            self._walk(getattr(expr, "update", None), func_addr, False)
        elif op == Op.HLIL_SWITCH:
            self._walk(getattr(expr, "condition", None), func_addr, False)
        else:
            for child in _safe_list(getattr(expr, "operands", None)):
                self._walk(child, func_addr, False)

    # ---------------------------------------------------------------- #
    # Type lookup + public entry point
    # ---------------------------------------------------------------- #

    def _base_type(self, func_addr: str, base_name: str) -> str:
        f = self._functions_by_addr.get(func_addr)
        if f is None:
            return ""
        for v in getattr(f, "vars", []) or []:
            if getattr(v, "name", None) == base_name:
                return str(getattr(v, "type", "") or "")
        return ""

    # ---------------------------------------------------------------- #
    # Call-context sharing (design: merge by SHARED DATA TYPE, not 3
    # partial structs). When a field-accessed base is passed into a callee
    # whose matching parameter is also field-accessed — OR a callee returns
    # a field-accessed base that the caller stores into a field-accessed
    # variable — a reverse engineer concludes the SAME data type and unions
    # the partial field observations into ONE complete struct. Driven by a
    # union-find over (func_addr, base_name) keys.
    # ---------------------------------------------------------------- #

    @staticmethod
    def _call_target_address(instr) -> str | None:
        c = getattr(instr, "const", None)
        if c is not None:
            return _hex(c) if isinstance(c, int) else str(c)
        dest = getattr(instr, "dest", None)
        if dest is not None:
            c = getattr(dest, "constant", None)
            if c is not None:
                return _hex(c)
        return None

    def _callee_return_bases(self, callee) -> list[str]:
        """Names of field-accessed bases returned by ``callee`` (from HLIL_RET
        src root variables)."""
        out = []
        for ins in getattr(getattr(callee, "hlil", None), "instructions", None) or []:
            if getattr(ins, "operation", None) not in (
                Op.HLIL_RET, Op.HLIL_TAILCALL,
            ):
                continue
            for src in _safe_list(getattr(ins, "src", None)) or []:
                rv = _root_var(src)
                if rv is not None and getattr(rv, "name", None):
                    out.append(rv.name)
        return out

    def _iter_calls(self, expr):
        """Yield every CALL/TAILCALL expression, recursing through the tree.

        Mirrors :meth:`_walk` structural descent (address not needed here) so
        call-context sharing is discovered even when a call sits inside an
        assignment, an if-condition, or any other container instruction.
        """
        if expr is None or not hasattr(expr, "operation"):
            return
        op = expr.operation
        if op in (Op.HLIL_CALL, Op.HLIL_TAILCALL):
            yield expr
            return
        if op in _ARRAY_OPS:
            yield from self._iter_calls(getattr(expr, "src", None))
            yield from self._iter_calls(getattr(expr, "index", None))
        elif op in _BINARY_OPS:
            yield from self._iter_calls(getattr(expr, "left", None))
            yield from self._iter_calls(getattr(expr, "right", None))
        elif op == Op.HLIL_ASSIGN:
            yield from self._iter_calls(getattr(expr, "dest", None))
            yield from self._iter_calls(getattr(expr, "src", None))
        elif op in (Op.HLIL_VAR_INIT, Op.HLIL_VAR_DECLARE):
            yield from self._iter_calls(getattr(expr, "src", None))
        elif op == Op.HLIL_RET:
            for val in _safe_list(getattr(expr, "src", None)):
                yield from self._iter_calls(val)
        elif op in _UNARY_SRC_OPS:
            yield from self._iter_calls(getattr(expr, "src", None))
        elif op in (Op.HLIL_IF, Op.HLIL_WHILE, Op.HLIL_DO_WHILE):
            yield from self._iter_calls(getattr(expr, "condition", None))
        elif op == Op.HLIL_FOR:
            yield from self._iter_calls(getattr(expr, "init", None))
            yield from self._iter_calls(getattr(expr, "condition", None))
            yield from self._iter_calls(getattr(expr, "update", None))
        elif op == Op.HLIL_SWITCH:
            yield from self._iter_calls(getattr(expr, "condition", None))
        else:
            for child in _safe_list(getattr(expr, "operands", None)):
                yield from self._iter_calls(child)

    def _sharing_edges(self) -> list[tuple[tuple, tuple]]:
        """Return union edges (key_A, key_B) from call-context evidence."""
        edges: list[tuple[tuple, tuple]] = []
        for f in self.extractor.get_functions():
            func_addr = _hex(getattr(f, "start", 0))
            for ins in getattr(getattr(f, "hlil", None), "instructions", None) or []:
                for call_ins in self._iter_calls(ins):
                    op = getattr(call_ins, "operation", None)
                    if op not in (Op.HLIL_CALL, Op.HLIL_TAILCALL):
                        continue
                    target = self._call_target_address(call_ins)
                    callee = self._functions_by_addr.get(target)
                    if callee is None:
                        continue
                    callee_params = getattr(callee, "parameter_vars", None) or []
                    for i, pexpr in enumerate(_safe_list(getattr(call_ins, "params", None)) or []):
                        if i >= len(callee_params):
                            break
                        cb = _root_var(pexpr)
                        pb = getattr(callee_params[i], "name", None)
                        if cb is None or not getattr(cb, "name", None) or not pb:
                            continue
                        edges.append(((func_addr, cb.name), (target, pb)))
                    # return-flow: caller stores callee's returned base into a var
                    dest = getattr(call_ins, "dest", None)
                    if dest is not None:
                        db = _root_var(dest)
                        if db is not None and getattr(db, "name", None):
                            for rb in self._callee_return_bases(callee):
                                edges.append(((func_addr, db.name), (target, rb)))
        return edges

    def find_struct_accesses(self) -> list[StructCandidate]:
        """Walk all functions' HLIL and group struct field accesses (4.1).

        Grouping (design § 4.1 + type-sharing merge): WITHIN a function by
        base variable; ACROSS functions when (a) Binja assigns the same
        meaningful type, or (b) call-context evidence shows the same value
        flows between them (a field-accessed base is passed as an argument
        to a callee whose parameter is also field-accessed, or is returned
        and stored). Merged candidates carry the UNION of all observed
        offsets — one complete struct, never N partial structs.
        """
        self._found = []
        self._functions_by_addr = {}
        for f in self.extractor.get_functions():
            func_addr = _hex(getattr(f, "start", 0))
            self._functions_by_addr[func_addr] = f
            for ins in getattr(getattr(f, "hlil", None), "instructions", None) or []:
                self._walk(ins, func_addr, False)
        if not self._found:
            return []

        within: dict[tuple[str, str], list[FieldAccess]] = {}
        for func_addr, base_name, acc in self._found:
            within.setdefault((func_addr, base_name), []).append(acc)

        # -- union-find over (func_addr, base_name) keys ------------------
        keys = list(within)
        parent = {k: k for k in keys}
        rank = {k: 0 for k in keys}

        def find(k):
            while parent[k] != k:
                parent[k] = parent[parent[k]]
                k = parent[k]
            return k

        def union(a, b):
            ra, rb = find(a), find(b)
            if ra == rb:
                return
            if rank[ra] < rank[rb]:
                ra, rb = rb, ra
            parent[rb] = ra
            if rank[ra] == rank[rb]:
                rank[ra] += 1

        # Edge set 1: identical meaningful Binja type (conservative, as before).
        typed: dict[str, list[tuple]] = {}
        for key in keys:
            hint = self._base_type(*key).strip()
            if hint and hint != "$unknown" and hint != "void" \
                    and "unknown" not in hint.lower():
                typed.setdefault(hint, []).append(key)
        for _hint, group in typed.items():
            root = group[0]
            for k in group[1:]:
                union(root, k)

        # Edge set 2: call-context sharing (SAME DATA TYPE across functions).
        for a, b in self._sharing_edges():
            if a in within and b in within:
                union(a, b)

        # -- assemble one candidate per connected component --------------
        comps: dict[tuple, list[tuple]] = {}
        for key in keys:
            comps.setdefault(find(key), []).append(key)

        candidates: list[StructCandidate] = []
        for i, root in enumerate(sorted(comps, key=str)):
            member_keys = comps[root]
            cluster = [acc for k in member_keys for acc in within[k]]
            func_sets = {k[0] for k in member_keys}
            base_names: dict[str, list[str]] = {}
            for func_addr, base_name in member_keys:
                base_names.setdefault(func_addr, []).append(base_name)
            for fa, names in base_names.items():
                base_names[fa] = sorted(set(names))

            hint = ""
            for fa, bn in member_keys:
                h = self._base_type(fa, bn).strip()
                if h and h != "$unknown" and h != "void" \
                        and "unknown" not in h.lower():
                    hint = h
                    break

            # overlap hint: same offset aliased with different sizes -> union
            overlap = False
            sizes_by_offset: dict[int, set[int]] = {}
            for acc in cluster:
                sizes_by_offset.setdefault(acc.offset, set()).add(acc.size)
            if any(len(sz) > 1 for sz in sizes_by_offset.values()):
                overlap = True

            candidates.append(StructCandidate(
                candidate_id=f"struct_cand_{i}",
                base_type_hint=hint,
                accesses=sorted(cluster, key=lambda a: (
                    int(a.function_address, 16), a.instruction_address, a.offset)),
                functions_involved=sorted(func_sets, key=lambda a: int(a, 16)),
                base_names=base_names,
                overlap_hint=overlap,
            ))
        return candidates


__all__ = ["StructCandidate", "FieldAccess", "StructAccessDetector", "_type_size"]
