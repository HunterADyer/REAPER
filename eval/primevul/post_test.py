"""PrimeVul post-testing — compile original function + measure identifier recovery.

Reads the manifest produced by build_dataset.py. For each sample it:

  1. Recovers the ORIGINAL identifiers from the PrimeVul `func` source
     (function name + heuristic local-variable identifier set). Because the
     original names survive in the source, they are the ground truth for
     identifier recovery — no separate DWARF needed.
  2. Wraps the function in a standalone .c (common headers + a main() that
     calls it) and gcc-compiles + strips it into a real stripped binary.
     Functions that depend on project-only headers fail to compile and are
     recorded as skips — never errors.
  3. (Gated) If Binary Ninja headless is importable, invokes the 8.2 runner
     against each binary; if a recovered-identifier report is later produced
     (reaper writes *_recovered.json per sample), computes precision/recall/F1
     of the recovered identifier SET against the original set.

This is intentionally set-based (no fragile source→var-node mapping); the
function-name exact match is the per-function headline metric.

Usage:
  python eval/primevul/post_test.py --manifest eval/primevul/manifest.json
  python eval/primevul/post_test.py --recovered-dir /path/to/recovered
"""

from __future__ import annotations

import argparse
import json
import pathlib
import re
import subprocess

OUT_DEFAULT = pathlib.Path(__file__).resolve().parent / "post_test_out"

# Shared standalone-compile machinery lives in _compile.py (also used by
# build_dataset.py's sampling-time compile gate). Imported defensively so the
# script runs both as `python eval/primevul/post_test.py` and as a package.
try:
    from _compile import (extract_func_name, extract_original_identifiers,
                          compile_standalone)
except ImportError:  # pragma: no cover - package-relative invocation
    from eval.primevul._compile import (extract_func_name,
                                        extract_original_identifiers,
                                        compile_standalone)


def set_metrics(original: set[str], recovered: set[str]) -> dict:
    """Precision/recall/F1 over an identifier set (all ints/floats)."""
    if not original and not recovered:
        return {"precision": 1.0, "recall": 1.0, "f1": 1.0, "n": 0}
    tp = len(original & recovered)
    p = tp / len(recovered) if recovered else 0.0
    r = tp / len(original) if original else 0.0
    f1 = 2 * p * r / (p + r) if (p + r) else 0.0
    return {"precision": round(p, 3), "recall": round(r, 3),
            "f1": round(f1, 3), "n": len(original)}


def run_reaper(bin_path: pathlib.Path, run_id: str,
               config: str = "configs/default.toml",
               python: str = "python3") -> dict:
    """(Binja-gated) run the 8.2 runner against a compiled primevul binary."""
    r = subprocess.run([python, "-m", "reaper.run", "--binary", str(bin_path),
                        "--config", config, "--run-id", run_id],
                       capture_output=True, text=True, timeout=600)
    return {"rc": r.returncode, "stdout_tail": r.stdout[-300:],
            "stderr_tail": r.stderr[-300:]}


def _binja_available() -> bool:
    try:
        import binaryninja  # noqa: F401
        return True
    except ImportError:
        return False


def compile_sample(sample: dict, out_dir: pathlib.Path,
                   gcc: str = "gcc") -> tuple[bool, pathlib.Path | None, str]:
    ok, bin_path, msg, _fname = compile_standalone(
        sample["func"], out_dir, sample["sample_id"], gcc=gcc)
    return ok, bin_path, msg


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--manifest", required=True)
    ap.add_argument("--out", default=str(OUT_DEFAULT))
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--recovered-dir", default=None,
                    help="dir with <sample_id>.recovered.json to score")
    ap.add_argument("--with-reaper", action="store_true")
    args = ap.parse_args()

    samples = [s for s in json.load(open(args.manifest, encoding="utf-8"))]
    if args.limit:
        samples = samples[:args.limit]
    out_dir = pathlib.Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    results = []
    ok = skipped = 0
    for s in samples:
        compiled, bin_path, msg = compile_sample(s, out_dir)
        entry = {"sample_id": s["sample_id"], "cwe": s["cwe"],
                 "project": s["project"], "func_name": None,
                 "compiled": compiled, "msg": msg,
                 "originals": None}
        if compiled and bin_path:
            ok += 1
            fname, _ = extract_func_name(s["func"])
            entry["func_name"] = fname
            entry["originals"] = extract_original_identifiers(s["func"])
            if args.with_reaper and _binja_available():
                entry["reaper"] = run_reaper(bin_path, s["sample_id"])
        else:
            skipped += 1
        if args.recovered_dir:
            rc = pathlib.Path(args.recovered_dir) / f"{s['sample_id']}.recovered.json"
            if rc.is_file():
                rec = json.load(open(rc, encoding="utf-8")).get("identifiers", [])
                entry["set_metrics"] = set_metrics(
                    set(extract_original_identifiers(s["func"])), set(rec))
            else:
                entry["set_metrics"] = {"error": "recovered file missing"}
        results.append(entry)

    report = {"total": len(samples), "compiled": ok, "skipped": skipped,
              "samples": results}
    rpath = out_dir / "post_test_report.json"
    json.dump(report, open(rpath, "w", encoding="utf-8"), indent=2)
    print(f"compiled={ok} skipped={skipped} total={len(results)}")
    print(f"report -> {rpath}")


if __name__ == "__main__":
    main()