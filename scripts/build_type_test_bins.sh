#!/usr/bin/env bash
# Build the stripped type-recovery test binaries.
#
# Each source is compiled at -O0 and -O2 (Binja HLIL differs substantially
# between optimization levels; the detector must handle both) and STRIPPED
# (-s) so no function names survive — mirroring a real stripped target.
#
# Output layout (per test k=01..05):
#   data/corpora/type_tests/type_test_0k_o0    (unoptimized, stripped)
#   data/corpora/type_tests/type_test_0k_o2    (optimized,   stripped)
#
# Requires: gcc. Run from the repo root:  bash scripts/build_type_test_bins.sh
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DIR="$ROOT/data/corpora/type_tests"
mkdir -p "$DIR"

for src in "$DIR"/type_test_*.c; do
  base="$(basename "$src" .c)"
  for opt in 0 2; do
    out="$DIR/${base}_o${opt}"
    # -fno-builtin: keep the calls LLVM/gcc would otherwise elide; -s: strip
    gcc -O$opt -fno-builtin -fno-omit-frame-pointer -s -o "$out" "$src"
    echo "built  $out"
  done
done

echo
echo "sanity: all functions are anonymous after stripping:"
for b in "$DIR"/type_test_*_o0; do
  printf '  %-28s visible non-stripped funcs: ' "$(basename "$b")"
  nm "$b" 2>/dev/null | awk '$2=="T"{print $3}' | tr '\n' ' '
  echo
done
