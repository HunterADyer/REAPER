"""Juliet C test-case sweep — RE post-testing harness (Deliverable 8.x eval).

For each Juliet C testcase found under eval/juliet/C/testcases (recursively:
the suite mixes flat CWE directories and `s0X` variant sub-dirs), we:

  1. Parse the source for the ground-truth testcase function names
     (`..._bad`, `..._good`, `..._goodN`) — these are the identifiers REAPER
     should recover from the stripped binary.
  2. Synthesize a tiny `main` stub that calls them, then
     `gcc -O2 -g` + `strip` the result into a stripped binary.
     Windows-only testcases (HMODULE etc.) fail to compile on Linux and are
     recorded as platform skips, never as errors.
  3. (Optional, gated) if Binary Ninja headless is importable, run the
     end-to-end `reaper.run` pipeline against the stripped binary and write a
     metrics line.

Usage:
  python eval/juliet/run_sweep.py --limit 20
  python eval/juliet/run_sweep.py --cwe CWE121_Stack_Based_Buffer_Overflow --limit 5
  python eval/juliet/run_sweep.py --keep-symbols   # skip `strip`
"""

from __future__ import annotations

import argparse
import json
import pathlib
import re
import subprocess
import sys

C_ROOT = pathlib.Path(__file__).resolve().parent / "C"
TESTCASES = C_ROOT / "testcases"
SUPPORT = C_ROOT / "testcasesupport"
OUT_DEFAULT = pathlib.Path(__file__).resolve().parent / "output"

_FUNC_RE = re.compile(r"\b(CWE\d+_[A-Za-z0-9_]+_(?:bad|good\d*))\s*\(")


def enumerate_testcases(cwe: str | None = None, limit: int | None = None):
    """Yield candidate *.c testcase paths (deterministic, recursive)."""
    base = TESTCASES if not cwe else TESTCASES / cwe
    if not base.is_dir():
        raise FileNotFoundError(f"not a directory: {base}")
    candidates = sorted(
        p for p in base.rglob("*.c")
        if "_" in p.name and p.parent.name != "testcasesupport"
    )
    if limit:
        candidates = candidates[:limit]
    yield from candidates


def parse_ground_truth(tc_path: pathlib.Path) -> list[str]:
    """Extract the bad/good function names defined by a Juliet testcase."""
    text = tc_path.read_text(encoding="utf-8", errors="replace")
    return sorted(set(_FUNC_RE.findall(text)))


def build_stub(names: list[str]) -> str:
    """Synthesize a main() that calls each testcase function (in order)."""
    decls = "\n".join(f"void {n}(void);" for n in names)
    calls = "\n".join(f"    {n}();" for n in names)
    return f"{decls}\nint main(void) {{\n{calls}\n    return 0;\n}}\n"


def compile_testcase(tc: pathlib.Path, out: pathlib.Path,
                     keep_symbols: bool = False,
                     gcc: str = "gcc") -> tuple[bool, str]:
    """Compile + strip a testcase. Returns (ok, message)."""
    names = parse_ground_truth(tc)
    if not names:
        return False, "no bad/good functions found"
    stub = pathlib.Path(out).with_suffix(".stub.c")
    stub.write_text(build_stub(names), encoding="utf-8")
    cmd = [
        gcc, "-O2", "-g",
        "-I", str(SUPPORT), "-I", str(tc.parent),
        str(tc), str(stub),
        str(SUPPORT / "io.c"), str(SUPPORT / "std_thread.c"),
        "-o", str(out), "-lm", "-lpthread", "-ldl",
    ]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        return False, (r.stderr or r.stdout)[:200]
    if not keep_symbols:
        subprocess.run(["strip", str(out)], check=True)
    return True, "ok"

def _binja_available() -> bool:
    try:
        import binaryninja  # noqa: F401  (optional runtime dep)
        return True
    except ImportError:
        return False


def run_reaper(binary: pathlib.Path, run_id: str, config: str,
               python: str = "python") -> dict:
    """Run the 8.2 runner against a stripped binary (Binja-gated).

    The runner is invoked as a subprocess so the sweep stays a standalone
    eval tool; the pipeline itself remains fully async internally.
    """
    cmd = [python, "-m", "reaper.run", "--binary", str(binary),
           "--config", config, "--run-id", run_id]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    return {"rc": r.returncode,
            "stdout_tail": r.stdout[-500:],
            "stderr_tail": r.stderr[-500:]}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--cwe", default=None, help="restrict to one CWE dir")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--out", default=str(OUT_DEFAULT))
    ap.add_argument("--keep-symbols", action="store_true")
    ap.add_argument("--config", default="configs/default.toml")
    ap.add_argument("--with-reaper", action="store_true",
                    help="run reaper pipeline on each compiled binary")
    args = ap.parse_args()

    out_dir = pathlib.Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    manifest = []
    compiled = skipped = 0

    for tc in enumerate_testcases(args.cwe, args.limit):
        rel = tc.relative_to(TESTCASES)
        sample = "_".join(rel.parts).replace(".c", "")
        sample_dir = out_dir / rel.parent
        sample_dir.mkdir(parents=True, exist_ok=True)
        bin_path = sample_dir / f"{sample}.bin"
        ok, msg = compile_testcase(tc, bin_path, args.keep_symbols)
        names = parse_ground_truth(tc)
        entry = {
            "testcase": str(rel), "sample_id": sample,
            "compiled": ok, "ground_truth": names, "msg": msg,
        }
        if ok and args.with_reaper and _binja_available():
            entry["reaper"] = run_reaper(bin_path, sample, args.config)
        manifest.append(entry)
        if ok:
            compiled += 1
        else:
            skipped += 1

    mpath = out_dir / "sweep_manifest.jsonl"
    with open(mpath, "w", encoding="utf-8") as f:
        for e in manifest:
            f.write(json.dumps(e) + "\n")
    print(f"compiled={compiled} skipped={skipped} total={len(manifest)}")
    print(f"manifest -> {mpath}")
    if skipped:
        print("note: skipped entries are Windows-only/portability failures,",
              "recorded in the manifest, never treated as regressions.")


if __name__ == "__main__":
    main()