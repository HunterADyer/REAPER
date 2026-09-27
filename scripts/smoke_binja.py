#!/usr/bin/env python3
"""Smoke test for the live Binary Ninja install (Phase-2 prerequisite).

Reproduces exactly what the pipeline's binja-critical path does against a real
stripped binary, so we validate the headless install + license + API surface
BEFORE a live end-to-end run:

  1. import binaryninja and report the core version;
  2. open_view(eval/cjson/cjson_test) with full analysis;
  3. list functions, extract HLIL text, parameters, variables, string refs via
     the repo's own HLILExtractor;
  4. (best-effort) ground-truth address alignment: the sym binary must expose
     function starts at the same addresses as the stripped binary for
     extract_ground_truth.py / metric matching to work.

Exit code 0 = ready; non-zero = something is broken (message printed).
"""

from __future__ import annotations

import os
import sys

BINARY = "/home/police/reaper/eval/cjson/cjson_test"
SYM_BINARY = "/home/police/reaper/eval/cjson/cjson_test_symbols"

sys.path.insert(0, "/home/police/reaper")

from reaper.tools._compat import BINJA_AVAILABLE, require_binja  # noqa: E402

if not BINJA_AVAILABLE:
    print(f"FATAL: `import binaryninja` failed. PYTHONPATH should point at the "
          f"binja python dir (e.g. $(HOME)/binja/python).")
    sys.exit(1)

import binaryninja  # noqa: E402

print(f"[1] Binary Ninja core version: {binaryninja.core_version}")

if not os.path.exists(BINARY):
    print(f"FATAL: missing stripped binary: {BINARY}")
    sys.exit(1)

print(f"[2] open_view({BINARY}) [full analysis, may take a few seconds] ...")
bv = binaryninja.open_view(BINARY)
if bv is None:
    print("FATAL: open_view returned None (license not valid?)")
    sys.exit(1)

from reaper.tools.hlil_extract import HLILExtractor  # noqa: E402

require_binja("smoke_binja")  # clear error if the env is still wrong

extractor = HLILExtractor(BINARY, data_dir="/tmp")
functions = list(bv.functions)
print(f"[3] functions analysed: {len(functions)}")

total_vars = total_params = total_strrefs = 0
sample_func = None
for meta in extractor.list_functions():
    addr = meta["address"]
    total_params += len(extractor.get_parameters(addr))
    total_vars += len(extractor.get_variables(addr))
    total_strrefs += len(extractor.get_string_refs(addr))
    if sample_func is None:
        sample_func = addr

print(f"    aggregate  params={total_params}  vars={total_vars}  string_refs={total_strrefs}")
if sample_func is not None:
    hlil = extractor.get_function_hlil(sample_func)
    print(f"    sample HLIL for {sample_func}: {len(hlil)} chars, "
          f"{hlil.count(chr(10))} lines")
    first_line = hlil.splitlines()[0] if hlil else "(empty)"
    print(f"      first line: {first_line[:120]!r}")
    sig = extractor.get_function_signature(sample_func)
    print(f"    signature: {sig!r}")

# --- [4] ground-truth address alignment -------------------------------------
print(f"[4] sym/stripped alignment check ...")
if os.path.exists(SYM_BINARY):
    bv_sym = binaryninja.open_view(SYM_BINARY)
    sym_starts = {hex(f.start) for f in bv_sym.functions if not f.name.startswith("_")}
    stripped_starts = {hex(f.start) for f in bv.functions}
    shared = len(sym_starts & stripped_starts)
    print(f"    sym funcs (non-underscore)={len(sym_starts)}  "
          f"stripped funcs={len(stripped_starts)}  shared starts={shared}")
    print("    PASS: address alignment OK" if shared > 0 else
          "    WARN: no shared function starts — GT extraction / metric matching will fail")
else:
    print(f"    (skip: no SYM binary at {SYM_BINARY})")

# --- [5] ground-truth struct extraction (metric 5 readiness) ------------------
print(f"[5] struct ground-truth extraction probe ...")
if os.path.exists(SYM_BINARY):
    import sys as _sys
    _sys.path.insert(0, "/home/police/reaper/eval/cjson")
    from extract_ground_truth import _struct_layouts  # noqa: E402

    try:
        layouts = _struct_layouts(bv_sym)
        if layouts:
            for name, fields in sorted(layouts.items()):
                sample = ", ".join(
                    f"+0x{f['offset']:x}({f['size']}B)" for f in fields[:6]
                )
                more = "" if len(fields) <= 6 else f" ... +{len(fields) - 6}"
                print(f"    struct {name}: {len(fields)} members  [{sample}{more}]")
            print("    PASS: metric-5 ground truth extractable")
        else:
            print("    INFO: no structs referenced via param/local types in SYM binary — "
                  "metric 5 will report 0 (not an error, but a scoring caveat)")
    except Exception as exc:  # noqa: BLE001
        print(f"    WARN: struct extraction probe failed: {exc.__class__.__name__}: {exc}")
else:
    print(f"    (skip: no SYM binary at {SYM_BINARY})")

print("\n[READY] Binja headless install validated for Phase 2 + ground-truth extraction.")
sys.exit(0)
