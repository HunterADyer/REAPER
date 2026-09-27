# eval/pass0 — Pass-0 (type-recovery) mini-corpus

Purpose-built SMALL binary to evaluate REAPER's Phase 2→4 type-recovery path,
as an alternative to `eval/cjson` (a real-world library with one giant
`cJSON` struct). This corpus is a single ~200-line C file with FIVE
deliberately **distinct** structs that are referenced ONLY by the binary's own
code, from multiple noinline accessors — giving the StructAccessDetector real
candidate groups and metric 5 a non-trivial layout set:

| struct  | purpose                                                 |
|---------|---------------------------------------------------------|
| cfg     | scalar variety + `char[64]` array + fn/data pointers    |
| point   | 16-byte geometry pair (embedded BY VALUE inside box)    |
| box     | nested struct member + int fields (deliberate padding)  |
| gnode   | linked node: ptr + enum + ptr + int                     |
| symtab  | pointer-to-pointer storage + counters                   |

## 1. Build BOTH variants

    ./build.sh

produces (gitignored build artifacts):
- `pass0_sample_symbols` — **unstripped**, `-g -O1` (ground-truth source)
- `pass0_sample`         — **STRIPPED** (the REAPER input)

## 2. Extract ground truth — NO Binary Ninja required

    python3 extract_ground_truth.py

writes `ground_truth.json` in the exact 8.1 schema `eval/evaluate.py` consumes,
via DWARF (`pyelftools`) + symbol-table analysis of the unstripped binary. Only
functions whose addresses survive into the STRIPPED binary's executable
segments are kept, and metric-5 structs are filtered to the corpus' OWN types
(empty/underscore/ALLCAPS libc aliases are excluded) — mirroring the Binja
extractor so corpora stay comparable. Requires `pyelftools`; the committed
`ground_truth.json` is regenerated deterministically by this script.

## 3. Run REAPER pass 0 → evaluate metric 5 (needs Binja for Phase 2/4)

    python -m reaper.run --binary eval/pass0/pass0_sample \
        --config configs/default.toml --run-id pass0_001        # phases 2..7
    # re-export reports without re-running LLM phases:
    python -m reaper.run --binary eval/pass0/pass0_sample \
        --config configs/default.toml --run-id pass0_001 --phases 3
    python3 eval/evaluate.py \
        --ground-truth eval/pass0/ground_truth.json \
        --reaper-output data/pass0_001_reaper_output.json

While Binja is unavailable, phases 2/4 can't run live yet — but building the
binary, extracting ground truth, and scoring the type-recovery metric are all
fully validated OFFLINE:

## 4. Offline validation (no Binja / LLM / network)

    python -m pytest tests/test_pass0_eval.py -q

These tests build the corpus if needed, verify extracted layouts against a
fresh compiled `offsetof` oracle, assert stripping survival, and exercise the
metric-5 scorer (identity → 100%, displaced → 0%, partial → partial).
