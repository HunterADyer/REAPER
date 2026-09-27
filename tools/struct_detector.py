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


class StructCandidate(BaseModel):
    """Group of struct field accesses that likely share one struct type."""

    candidate_id: str
    base_type_hint: str
    accesses: list[FieldAccess]
    functions_involved: list[str]


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

    def find_struct_accesses(self) -> list[StructCandidate]:
        """Walk all functions' HLIL and group struct field accesses (4.1)."""
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

        # Cross-function key: only a MEANINGFUL Binja type may bridge two
        # functions; unknown types force independent candidates (no false
        # positive grouping — design § 4.1).
        cross_key: dict[tuple[str, str], str] = {}
        fallback = 0
        for key in within:
            hint = self._base_type(*key).strip()
            if hint and hint != "$unknown" and hint != "void" \
                    and "unknown" not in hint.lower():
                cross_key[key] = hint
            else:
                fallback += 1
                cross_key[key] = f"__untracked_{fallback}"

        clusters: dict[str, list[FieldAccess]] = {}
        func_sets: dict[str, set] = {}
        hinted: dict[str, str] = {}
        for key, group in within.items():
            ck = cross_key[key]
            clusters.setdefault(ck, []).extend(group)
            func_sets.setdefault(ck, set()).add(key[0])
            if not ck.startswith("__untracked_"):
                hinted[ck] = self._base_type(*key)

        candidates: list[StructCandidate] = []
        for i, ck in enumerate(sorted(clusters, key=str)):
            candidates.append(StructCandidate(
                candidate_id=f"struct_cand_{i}",
                base_type_hint=hinted.get(ck, ""),
                accesses=sorted(clusters[ck], key=lambda a: (
                    int(a.function_address, 16), a.instruction_address, a.offset)),
                functions_involved=sorted(func_sets[ck], key=lambda a: int(a, 16)),
            ))
        return candidates


__all__ = ["StructCandidate", "FieldAccess", "StructAccessDetector", "_type_size"]
