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


def extract() -> dict:
    import binaryninja  # may raise ImportError when Binja is absent

    sym_bv = binaryninja.open_view(SYM_BINARY)
    stripped_bv = binaryninja.open_view(STRIPPED_BINARY)

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
    print(f"Extracted {len(ground_truth)} functions -> {OUT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
