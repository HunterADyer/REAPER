#!/bin/bash
# Deliverable 8.1 — Build a stripped cJSON test binary for the integration test.
#
# Produces (in this directory):
#   cjson_test_symbols  — unstripped, with symbols (ground-truth source)
#   cjson_test          — STRIPPED (the REAPER input)
# Next run extract_ground_truth.py (requires Binja headless on PYTHONPATH).
set -euo pipefail
cd "$(dirname "$0")"

# Pin to v1.7.18 for reproducibility.
CJSON_TAG="v1.7.18"
if [ ! -f cJSON.c ] || [ ! -f cJSON.h ]; then
  curl -sL "https://raw.githubusercontent.com/DaveGamble/cJSON/${CJSON_TAG}/cJSON.c" -o cJSON.c
  curl -sL "https://raw.githubusercontent.com/DaveGamble/cJSON/${CJSON_TAG}/cJSON.h" -o cJSON.h
fi

cat > main.c << 'MAIN'
#include <stdio.h>
#include <stdlib.h>
#include "cJSON.h"
int main() {
    const char *json = "{\"key\": \"value\"}";
    cJSON *root = cJSON_Parse(json);
    if (root) {
        char *out = cJSON_Print(root);
        if (out) { printf("%s\n", out); free(out); }
        cJSON_Delete(root);
    }
    return 0;
}
MAIN

# Dynamic linking (NOT -static) so import symbols exist for Pass -1.
gcc -O2 -g -o cjson_test_symbols cJSON.c main.c -lm
cp cjson_test_symbols cjson_test
strip cjson_test

echo "Binaries built. Run extract_ground_truth.py next (requires Binja headless)."
