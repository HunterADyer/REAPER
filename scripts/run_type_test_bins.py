#!/usr/bin/env python3
"""Run type recovery (detector -> LLM verdict) over the stripped test
binaries in data/corpora/type_tests/, for hand-tuning the merge + verdict.

REQUIRES Binary Ninja headless (the detector reads real HLIL through
HLILExtractor). When Binja is absent it prints a clear hint and exits 0, so
CI-ish flows do not fail on Binja-less machines.

Usage:
    python3 scripts/run_type_test_bins.py            # all binaries, both opt
    python3 scripts/run_type_test_bins.py 01 04      # subsets by number
    python3 scripts/run_type_test_bins.py --no-llm   # detector only (no verdict)

What it prints per binary:
  - the detector's candidates (per-candidate offsets→functions matrix),
    so you can SEE whether the call-context merge correctly unions the
    partial views into one complete struct (or wrongly merges test 05),
  - the LLM verdict for each accepted/rejected candidate (name, kind, field
    union), which is the hand-tuning signal: over-merge = accepted wrongly;
    under-merge = split when it should be one.

The verdict uses the live configured LLM if available (llm_client), else a
no-op stub that accepts EVERYTHING with a placeholder name (so the
detector results still print).
"""

from __future__ import annotations

import argparse
import glob
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from reaper.tools.struct_detector import StructAccessDetector  # noqa: E402


def _detector_only(binary: Path):
    """Open the binary via HLILExtractor, run the detector, print candidates."""
    try:
        from reaper.tools.hlil_extract import HLILExtractor
        extractor = HLILExtractor(str(binary), str(binary.parent))
    except Exception as exc:  # Binja missing / import failure
        print(f"  [skip] {binary.name}: {exc}")
        return
    detector = StructAccessDetector(extractor)
    candidates = detector.find_struct_accesses()
    print(f"  candidates: {len(candidates)}")
    for c in candidates:
        print(f"    {c.candidate_id}  hint={c.base_type_hint!r} "
              f"overlap={c.overlap_hint}")
        print(f"      functions: {c.functions_involved}")
        print(f"      bases:     {c.base_names}")
        for a in sorted(c.accesses, key=lambda x: (x.function_address, x.offset)):
            print(f"        +0x{a.offset:x} [{a.access_type},{a.size}B] "
                  f"{a.function_address} @ {a.instruction_address} (base={a.base_var})")


async def _with_verdict(binary: Path):
    """Run detector + TypeRecoveryAgent verdict over the binary."""
    from reaper.agents.type_recovery import TypeRecoveryAgent
    from reaper.harness.submission import TypeVerdict
    try:
        from reaper.tools.hlil_extract import HLILExtractor
        extractor = HLILExtractor(str(binary), str(binary.parent))
    except Exception as exc:
        print(f"  [skip] {binary.name}: {exc}")
        return

    class StubLLM:
        """No LLM wired: accept everything, name from the hint (tuning stub)."""

        async def create_session(self, *a, **k):
            return None

        async def send(self, *a, **k):
            return '{"accepted": true, "struct_name": "stub_type", "kind": "struct", "fields": []}'

        async def destroy_session(self, *a, **k):
            return None

    class StubContext:
        corpus = binary.name
        ledger = None

        async def for_struct_candidate(self, candidate):
            rows = []
            for a in candidate.accesses:
                rows.append(
                    f"+0x{a.offset:x} [{a.access_type},{a.size}B] "
                    f"{a.function_address} @ {a.instruction_address}"
                )
            return "\n".join(rows)

    agent = TypeRecoveryAgent(StubLLM(), StubContext(), {})
    for c in StructAccessDetector(extractor).find_struct_accesses():
        verdict = await agent.run(c)
        _print_verdict(verdict)


def _print_verdict(verdict: TypeVerdict) -> None:
    status = "ACCEPTED" if verdict.accepted else "REJECTED"
    print(f"    {status}: name={verdict.struct_name!r} kind={verdict.kind} "
          f"fields={len(verdict.fields)}")
    if not verdict.accepted:
        print(f"      rejection_reason: {verdict.rejection_reason}")
    for f in verdict.fields:
        print(f"        +0x{f.offset:x} {f.type_str} {f.name} [{f.confidence}]")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("numbers", nargs="*", help="test numbers to run, e.g. 01 04")
    ap.add_argument("--no-llm", action="store_true",
                    help="detector only; skip the LLM verdict stage")
    args = ap.parse_args()

    pattern = os.path.join(str(ROOT), "data", "corpora", "type_tests",
                           "type_test_0*.c")
    sources = sorted(glob.glob(pattern))
    if args.numbers:
        sources = [s for s in sources
                   if os.path.basename(s).split("_")[2] in args.numbers]
    if not sources:
        print("no type test binaries found — run "
              "scripts/build_type_test_bins.sh first")
        return

    for src in sources:
        for opt in ("o0", "o2"):
            binary = src[:-2] + "_" + opt
            if not os.path.exists(binary):
                continue
            print(f"== {os.path.basename(binary)} ==")
            if args.no_llm:
                _detector_only(Path(binary))
            else:
                _with_verdict(Path(binary))


if __name__ == "__main__":
    main()
