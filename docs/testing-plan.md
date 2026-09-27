# REAPER RE Stage — Post-Testing & Evaluation Plan

Lead artifact for the 8.x deliverables: how we validate the recovered names,
types, and claims end-to-end, given the current environment constraints.

## 1. Environment constraints (today)
| Component | Status | Consequence |
|---|---|---|
| Binary Ninja headless 6.0 | NOT installed | Phase-2 HLIL extraction / BNDB writeback staged behind `reaper/tools/_compat.require_binja()`; unit tests use `tests/fake_binja.py` |
| Neo4j | LIVE (schema verified, 5 constraints) | runner needs a reachable instance — config `[neo4j] uri` |
| vLLM `:8010` | unverified in this run | LLM-dependent steps must degrade / be mocked (FakeVLLMServer) |
| gcc 13.3 / strip | present | Juliet + PrimeVul binaries compile NOW |
| Juliet C suite | ~54,484 testcase files, 118 CWEs | compile gate on samples |
| PrimeVul dataset | 6/6 GDrive files downloaded + validated | per-split stats recorded below |

Fixes needed before a *real* end-to-end run: install `binja-headless` (set
`PYTHONPATH=$HOME/binja-headless/python`; NOT pip-installable), start vLLM on
`:8010` with a chat model, ensure Neo4j is up with the 5 UNIQUENESS
constraints from `init_schema`.

## 2. Three-tier testing strategy
- **Tier 1 — Unit (always runs, no Binja/Neo4j/vLLM).** `pytest tests/ -q`.
  `fake_binja` + `FakeVLLMServer` + tmp SQLite keep every pipeline component
  testable. This is the release gate: keep the existing 30 + all new tests green.
- **Tier 2 — Integration smoke (optional hardware).** With Binja+Neo4j+vLLM
  present: `python -m reaper.run --binary <stripped> --config ... --run-id smoke`
  on the cJSON binary, Juliet samples, and compiled PrimeVul samples. Verifies
  the whole wire-up (see `docs/testing-plan.md` §"Run it").
- **Tier 3 — Ground-truth evaluation.** `eval/evaluate.py` (8.3, teammate-owned)
  scoring recovered output vs ground truth; plus the Juliet/PrimeVul post-test
  harnesses (below) that generate new binaries + ground truth at scale.

## 3. Unit tests for the new post-test tooling (lead-owned)
- `tests/test_primevul_build.py` — synthetic JSONL: `_is_plain_c` C vs C++
  discrimination, `validate_split` counts, `sample_vulnerable_c` determinism
  + vulnerability filtering (no network; no gcc required for these pure parts).
- `tests/test_runner_imports.py` — import the 8.2 module graph and assert
  every constructor signature matches `docs/skills/cross-references.md`
  (arg order is THE contract between 8.2 and Phases 2–7).

## 4. Juliet sweep (eval/juliet/run_sweep.py) — DONE mechanically
- Enumerates all `*.c` testcases recursively (flat + `s0X` layout).
- Parses ground-truth `..._bad/good/goodN` function names from source.
- Synthesizes a calling `main` stub, `gcc -O2 -g` + `strip` → stripped binary,
  records ground truth to `sweep_manifest.jsonl`.
- Windows-only testcases (e.g. CWE114 `HMODULE`) fail on Linux → recorded
  skips, never regressions.
- `--with-reaper`: gated on Binja availability, invokes `reaper.run` per binary.

### Juliet unit-test plan (lead, part of Tier 1)
- `parse_ground_truth` on a synthetic snippet; only `_bad`/`_goodN` names.
- `build_stub` generates declarations + a `main` that calls each in order.
- End-to-end compile on a tiny hand-written testcase (gcc present) — verifies
  stripped binary + manifest entry; if gcc missing skip via `shutil.which`.

## 5. PrimeVul post-testing (eval/primevul/*) — DONE mechanically
- `build_dataset.py`: GDrive download (gdown, optional) → validate → sample.
  Schema verified on the real files (v0: project/commit_id/target/func/cwe/
  big_vul_idx/idx/hash).
- `--require-compile`: samples ONLY functions that compile standalone (shim
  generator + tolerance flags in `_compile.py`). This is the honest gate:
  PrimeVul functions frequently depend on project headers/undefined libcalls;
  whatever compiles is a real binary with original identifiers as ground truth.
- `post_test.py`: per-sample compile+strip, original-identifier extraction
  (function name exact-match headline metric + set-based P/R/F1), `--recovered-dir`
  to score pipeline output that isn't produced yet, `--with-reaper` gate.
- Ground truth needs NO DWARF: original identifiers survived in the source text.

### PrimeVul post-testing/unit-test plan
- Pure: `extract_func_name`, `extract_original_identifiers`, `set_metrics`
  (perfect overlap → 1.0; empty → defined behavior), `build_shim` on a snippet
  mentioning a PascalCase opaque type.
- Compile gate: a self-contained function (pure math) must compile; a snippet
  referencing an undefined symbol must be skipped (recorded, not an error).

## 6. Volumetrics from the real dataset (9/26)
- train 184,427 rows (5,574 vuln); test 25,911 (695 vuln); valid 25,430 (699).
- C-only vulnerable-filtered manifest (ungated): 38/60; gated `--require-compile`
  yield measured on the real data → recorded in `continue.md`.

## 7. cJSON end-to-end smoke (8.1/8.2/8.3 integration)
Once Binja and the vLLM endpoint are up:
1. `eval/cjson/build.sh` → stripped `cjson_test` + ground truth (teammate-owned).
2. `python -m reaper.run --binary eval/cjson/cjson_test --run-id cjson_001`.
3. `python eval/evaluate.py --ground-truth eval/cjson/ground_truth.json
   --recovered <resynth output>` → 6-metric report.

## 8. Release criteria (Gate for calling 8.x done)
- Tier 1 fully green (incl. the new post-test unit tests above).
- `run.py --help` works; import graph loads with only optional-Binja warnings.
- Juliet sweep compiles a portable subset with 100% ground-truth capture.
- PrimeVul manifest regenerated with `--require-compile`; post_test produces a
  report with `compiled > 0` and model-graded identifier scores recorded.
- All 8 phases checked in `docs/skills/progress.md` with real evidence.

## 9. Known risks & mitigations
- PrimeVul compile yield may be modest: accepted; a project-build-mode
  (real headers per project) is the long-term unblocker.
- No Binja in CI: all Binja-touching modules shell out only behind
  `_compat.BINJA_AVAILABLE`; tests inject `fake_binja` so Tier 1 never blocks.
- vLLM `:8010` unverified: evaluators/agents take an injectable LLM client;
  `FakeVLLMServer` covers offline paths.