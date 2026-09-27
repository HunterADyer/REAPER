"""PrimeVul dataset build / validate / sample — post-testing eval pipeline.

Fetches the PrimeVul release from the official Google Drive folder (original
release, Mar 2024), validates each JSONL split, and selects a compact,
C-only, vulnerable-function manifest suitable for RE post-testing (compile the
function into a stripped binary, run REAPER, compare recovered identifiers
against the ORIGINAL identifiers that survived in the source text).

The HF mirror (`PrimeVul/PrimeVul`) is gated; the GDrive folder is public and
requires no token. `gdown` is used only when it is importable; all other
logic is stdlib-only so tests never need network.

Schema of v0 JSONL rows (verified against the actual download):
  project (str), commit_id (str), target (0/1), func (str source),
  cwe (str), big_vul_idx (int), idx (int), hash (str)
v0.1 rows add commit/vulnerability/file metadata — extra keys are ignored.

Usage:
  python eval/primevul/build_dataset.py --download --validate --sample --limit 100
"""

from __future__ import annotations

import argparse
import json
import os
import sys

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
MANIFEST_PATH = os.path.join(os.path.dirname(__file__), "manifest.json")

# Official GDrive folder: PrimeVul original release (Mar 27 2024).
GDRIVE_FOLDER = "https://drive.google.com/drive/folders/19iLaNDS0z99N8kB_jBRTmDLehwZBolMY"

SPLITS = [
    "primevul_train.jsonl",
    "primevul_train_paired.jsonl",
    "primevul_valid.jsonl",
    "primevul_valid_paired.jsonl",
    "primevul_test.jsonl",
    "primevul_test_paired.jsonl",
]

# Heuristic C/C++ discriminator. PrimeVul is C/C++; the RE post-test compiles
# with gcc, so we prefer plain-C functions. This is deliberately conservative:
# we only EXCLUDE obvious C++ (we never mislabel C as C++).
_CPP_SIGNALS = (
    "->", "::", "template", "class ", "std::", "using namespace",
    "namespace std", "nullptr", "public:", "private:", "protected:",
    "constexpr", "typename", "override", "virtual ", "enum class",
    "operator", "new ", "cout", "&&",
)


def _is_plain_c(func: str) -> bool:
    """Conservative heuristic: True unless obvious C++ constructs appear."""
    if not func or not func.strip():
        return False
    return not any(sig in func for sig in _CPP_SIGNALS)


try:
    from _compile import compile_standalone as _cs
except ImportError:  # pragma: no cover - package-relative invocation
    from eval.primevul._compile import compile_standalone as _cs


def is_vulnerable_function(row: dict) -> bool:
    """Post-testing targets = vulnerable (target==1) plain-C functions."""
    return bool(row.get("target") == 1 and _is_plain_c(row.get("func", "")))


def compiles_standalone(func: str, tmpdir: str) -> bool:
    """True if the function survives the standalone compile gate."""
    import tempfile
    if not tmpdir:
        tmpdir = tempfile.mkdtemp(prefix="reaper_pv_")
    ok, _bin, _msg, _fname = _cs(func, __import__("pathlib").Path(tmpdir), "probe")
    return ok


def iter_rows(path: str):
    """Yield parsed JSON rows; skip malformed lines."""
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def validate_split(path: str) -> dict:
    """Return per-split stats. Pure python — safe anywhere."""
    total = 0
    vulnerable = 0
    cwes: set[str] = set()
    for row in iter_rows(path):
        total += 1
        cwes.add(str(row.get("cwe", "?")))
        if row.get("target") == 1:
            vulnerable += 1
    return {
        "file": os.path.basename(path),
        "total": total,
        "vulnerable": vulnerable,
        "benign": total - vulnerable,
        "num_cwes": len(cwes),
    }


def sample_vulnerable_c(test_path: str, limit: int, require_compile: bool = False,
                        tmpdir: str = "") -> list[dict]:
    """Deterministically select `limit` vulnerable plain-C rows from test split."""
    picked: list[dict] = []
    seen: set[str] = set()
    for row in iter_rows(test_path):
        if len(picked) >= limit:
            break
        if not is_vulnerable_function(row):
            continue
        key = str(row.get("hash") or row.get("idx"))
        if key in seen:
            continue
        if require_compile and not compiles_standalone(row.get("func", ""), tmpdir):
            continue
        seen.add(key)
        picked.append({
            "idx": row.get("idx"),
            "hash": key,
            "project": row.get("project"),
            "cwe": str(row.get("cwe", "?")),
            "func": row.get("func", ""),
            "out_binary": f"primevul_sample_{len(picked):04d}",
        })
    # Stable order regardless of hash randomization.
    for i, p in enumerate(sorted(picked, key=lambda r: r["idx"] or r["hash"])):
        p["sample_id"] = f"primevul_{i:04d}"
    return picked
def download(data_dir: str = DATA_DIR) -> None:
    """Fetch the GDrive folder via gdown; skip files already present."""
    os.makedirs(data_dir, exist_ok=True)
    missing = [s for s in SPLITS if not os.path.exists(os.path.join(data_dir, s))]
    if not missing:
        print("All splits already present — nothing to download.")
        return
    try:
        import gdown  # optional; needed only for the download step
    except ImportError as e:  # pragma: no cover - env-specific
        print(f"gdown not installed; cannot download. Missing: {missing} ({e})")
        return
    gdown.download_folder(GDRIVE_FOLDER, output=data_dir, quiet=False)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--download", action="store_true")
    ap.add_argument("--validate", action="store_true")
    ap.add_argument("--sample", action="store_true")
    ap.add_argument("--limit", type=int, default=100)
    ap.add_argument("--require-compile", action="store_true",
                    help="only include functions that compile standalone")
    ap.add_argument("--tmpdir", default="")
    ap.add_argument("--data-dir", default=DATA_DIR)
    ap.add_argument("--out", default=MANIFEST_PATH)
    args = ap.parse_args()

    if args.download:
        download(args.data_dir)
    if args.validate:
        for split in SPLITS:
            path = os.path.join(args.data_dir, split)
            if not os.path.exists(path):
                print(f"[skip] {split}: not present")
                continue
            stats = validate_split(path)
            print(
                f"{stats['file']}: total={stats['total']} "
                f"vuln={stats['vulnerable']} benign={stats['benign']} "
                f"cwes~{stats['num_cwes']}"
            )
    if args.sample:
        test_path = os.path.join(args.data_dir, "primevul_test.jsonl")
        if not os.path.exists(test_path):
            print("primevul_test.jsonl not present; run --download first.", file=sys.stderr)
            return
        manifest = sample_vulnerable_c(test_path, args.limit,
                                       require_compile=args.require_compile,
                                       tmpdir=args.tmpdir)
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(manifest, f, indent=2)
        print(f"wrote {len(manifest)} samples -> {args.out}")


if __name__ == "__main__":
    main()
