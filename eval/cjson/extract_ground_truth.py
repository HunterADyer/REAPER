"""Deliverable 8.1 — extract ground truth by comparing stripped vs unstripped.

Uses Binja headless on the UNSTRIPPED ``cjson_test_symbols`` to record the real
function name / parameters / local variables, matched by address against the
STRIPPED ``cjson_test`` (so only symbols that SURVIVE stripping are kept).

When Binja is not importable, writes an empty ``ground_truth.json`` and notes
the requirement — the pipeline still runs; the evaluator just reports 0.

Usage (from eval/cjson/):
    python3 extract_ground_truth.py
"""

from __future__ import annotations

import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SYM_BINARY = os.path.join(HERE, "cjson_test_symbols")
STRIPPED_BINARY = os.path.join(HERE, "cjson_test")
OUT_PATH = os.path.join(HERE, "ground_truth.json")


def _struct_layouts(bv) -> dict:
    """Metric-5 ground truth — named structs referenced by the binary's code.

    Audit 2026-09-27: the extractor only produced function name/params/locals,
    so type-recovery accuracy (metric 5) had NO ground truth and the evaluator
    could never score it. This walks every function's parameter + local
    variable types, resolves pointers to the underlying structure type, and
    records real member offsets + sizes from the (DWARF-enriched) unstripped
    view. Structures the binary's own code never references are omitted on
    purpose, which keeps libc noise (FILE, tm, ...) out of the score.

    Sizes deliberately mirror StructAccessDetector._type_size's x86-64
    assumption (pointers => 8) when a member's resolved width is unknown, so
    REAPER's inferred sizes are compared apples-to-apples on the only target we
    currently handle (x86-64). Extracted schemas land under the top-level
    ``"structs"`` key, matching what evaluate.metric_5 reads.
    """
    import binaryninja

    TypeClass = binaryninja.TypeClass
    layouts: dict[str, list[dict]] = {}

    def _structure_of(t):
        depth = 0
        while t is not None and depth < 8:
            tc = getattr(t, "type_class", None)
            if tc == TypeClass.PointerTypeClass:
                t = getattr(t, "target", None)
            elif tc == TypeClass.StructureTypeClass:
                return t
            else:
                return None
            depth += 1
        return None

    def _record(t) -> None:
        st = _structure_of(t)
        if st is None:
            return
        name = str(getattr(st, "name", "") or "").strip()
        for prefix in ("struct ", "union ", "enum "):
            if name.startswith(prefix):
                name = name[len(prefix):].strip()
        # Skip libc / implementation internals the LLM would never be asked to
        # recover (underscore-prefixed or ALLCAPS aliases e.g. FILE, _IO_FILE).
        if not name or name.startswith("_") or name.isupper():
            return
        members = [m for m in (getattr(st, "members", None) or []) if m]
        if not members:
            return
        rows: list[dict] = []
        for m in members:
            try:
                field_type = getattr(m, "type", None)
                tc = getattr(field_type, "type_class", None)
                size = int(getattr(field_type, "width", 0) or 0)
                if size <= 0:
                    size = 8 if tc == TypeClass.PointerTypeClass else 0
                rows.append({
                    "offset": int(getattr(m, "offset", 0) or 0),
                    "size": size,
                    "name": str(getattr(m, "name", "") or ""),
                    "type_str": str(field_type),
                })
            except Exception:
                continue
        if rows and name not in layouts:
            layouts[name] = rows

    # Never crash the whole extraction because of a type-API quirk.
    try:
        for func in bv.functions:
            for v in list(getattr(func, "parameter_vars", None) or []) + \
                      list(getattr(func, "vars", None) or []):
                _record(getattr(v, "type", None))
    except Exception:
        return {}
    return layouts


def _open_bv(path):
    """Open a live view regardless of API naming (open_view vs load)."""
    import binaryninja  # noqa: PLC0415 - may raise ImportError when absent
    opener = getattr(binaryninja, "open_view", None) or getattr(
        binaryninja, "load", None)
    if opener is None:
        raise RuntimeError(f"no BinaryView opener on {binaryninja!r}")
    return opener(path)


def extract() -> dict:
    import binaryninja  # may raise ImportError when Binja is absent

    sym_bv = _open_bv(SYM_BINARY)
    stripped_bv = _open_bv(STRIPPED_BINARY)

    ground_truth: dict = {}
    for func in sym_bv.functions:
        # Skip runtime/unnamed functions.
        if func.name.startswith("sub_") or func.name.startswith("_"):
            continue
        # Verify the address exists in the stripped binary (survived stripping).
        if stripped_bv.get_function_at(func.start) is None:
            continue
        params = [
            {"name": p.name, "type": str(p.type)}
            for p in func.parameter_vars
        ]
        locals_ = [
            {"name": v.name, "type": str(v.type)}
            for v in func.vars
            if v not in func.parameter_vars
        ]
        ground_truth[hex(func.start)] = {
            "function_name": func.name,
            "parameters": params,
            "local_variables": locals_,
        }
    ground_truth["structs"] = _struct_layouts(sym_bv)
    return ground_truth


def main() -> int:
    if not os.path.exists(SYM_BINARY) or not os.path.exists(STRIPPED_BINARY):
        print("missing binaries — run build.sh first", file=sys.stderr)
        return 1
    try:
        ground_truth = extract()
    except ImportError:
        print("Binary Ninja headless not importable — writing EMPTY ground "
              "truth (set PYTHONPATH=$HOME/binja-headless/python to extract).",
              file=sys.stderr)
        ground_truth = {}
    with open(OUT_PATH, "w", encoding="utf-8") as fh:
        json.dump(ground_truth, fh, indent=2)
    n_funcs = sum(
        1 for v in ground_truth.values()
        if isinstance(v, dict) and "function_name" in v
    )
    n_structs = len(ground_truth.get("structs") or {})
    print(f"Extracted {n_funcs} functions + {n_structs} structs -> {OUT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
