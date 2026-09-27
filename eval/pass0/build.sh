#!/bin/bash
# Build a small Pass-0 (type recovery) evaluation corpus — BOTH variants.
#
# Produces (in this directory):
#   pass0_sample_symbols  — unstripped, -g (ground-truth source)
#   pass0_sample          — STRIPPED (the REAPER input)
#
# Next step (no Binary Ninja needed):
#   python3 extract_ground_truth.py   -> writes ground_truth.json
#
# -O1 keeps explicit struct-field accesses in the disassembly while producing
# a realistically-shaped binary; -fno-inline + external linkage keep every
# accessor a real, addressable function (metrics 1-4 need the denominator).
set -euo pipefail
cd "$(dirname "$0")"

gcc -O1 -g -fno-inline -Wall -x c sample.c -o pass0_sample_symbols
cp pass0_sample_symbols pass0_sample
strip pass0_sample

echo "Built pass0_sample_symbols (unstripped) + pass0_sample (stripped)."
echo "Next: python3 extract_ground_truth.py  (writes ground_truth.json)"
