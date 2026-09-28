# REAPER

**R**everse-**E**ngineering **A**gent **P**ipeline for **E**ntity **R**ecovery

> A fully-async harness that reverse-engineers *stripped* binaries: Binary Ninja
> decompiles to HLIL, we materialize a labeled dataflow graph in Neo4j, and a
> team of LLM agents — all one local model via vLLM, differentiated by role — 
> rename functions/variables, infer data types, write evidenced claims, and run
> adversarial critic + resynthesis loops until an evidence-coverage criterion is
> met. Output is a serialized "draft CPG" plus a mutated `.bndb`, scored offline
> against ground truth.

---

## Why "REAPER"

Strip the symbols, and a binary becomes a graveyard of `sub_401000` and `var_28`.
REAPER goes back over that ground and **harvests** meaning — names, types,
claims, and call structure — function by function, until it can approach the
original intent. Reversing is the domain; reaping the entities back out of it
is the trade.

## What it does

```
 stripped binary
      │  Binary Ninja 6.0 headless (HLIL decompilation)
      ▼
 [tools/hlil_extract]  ⇒  readable HLIL text + vars/params/string-refs + .bndb
      │  async Cypher (MERGE only, idempotent)
 [Neo4j master graph]  Function | Variable | Argument | Call | StringRef | Struct
      │  edge types: CONTAINS, DATAFLOW_*, CALL, RETURN, REFS_STRING, FIELD_OF
      ▼  agents (single base URL/model; roles by prompt, quality by thinking level)
 Phase 4 type recovery   → StructAccessDetector → TypeRecoveryAgent → GraphRebuilder
 Phase 5 Pass 1 sweep    → RenameVariableAgent, bottom-up, minimal thinking
 Phase 6 Pass 2 deep rev → ReviewAgent → CriticEvaluator → MergeAgent (shadow-diff)
 Phase 7 investigate     → Scheduler → InvestigationAgent → InvestigationLoop
 Phase 7 resynthesize    → ResynthesisAgent → ResynthesisLoop → is_re_complete
      ▼
 [SQLite ledger+todo] [traces/*.jsonl] [data/<run>_reaper_output.json]  → eval
```

Run phases are **resumable**: all state persists in Neo4j + SQLite, so a run can
be stopped and continued (`--phases N`) without losing work.

## Repository layout

The repo root *is* the `reaper` package (see `pyproject.toml`). Older docs that
say `reaper/tools/...` mean these top-level dirs.

| Path | Role |
|---|---|
| `run.py` | End-to-end pipeline runner (the only top-level entry point) |
| `configs/default.toml` | All runtime config (neo4j, vllm, thinking levels, limits, paths) |
| `infra/` | Neo4j docker-compose, `schema.cypher`, `init_db.py` |
| `harness/` | LLM client, tracer, ledger, todo, context assembler, shadow, merge, dispatchers, loops |
| `agents/` | The LLM agent classes + `prompts/*.txt` system prompts |
| `tools/` | Binja compat shim, HLIL extractor, Neo4j graph builders, BNDB writer, struct detector/rebuild |
| `eval/` | Ground-truth extraction, evaluation metrics, modular LLM-judge **scoring** engine, corpora |
| `scripts/` | Binja install/gate scripts |
| `tests/` | Offline unit suite (fake Binja / Neo4j / vLLM doubles) — no live services needed |
| `gui/` | "Run Cockpit" — FastAPI+WebSocket monitoring + run control |
| `docs/` | Design spec, module API surface, skills/patterns/progress |

## Quickstart

```bash
# 1. Offline unit gate (needs NO Binja/Neo4j/vLLM)
python3 -m pytest -q

# 2. Neo4j up
(cd infra && docker compose up -d)

# 3. Binary Ninja headless (Linux) — see scripts/setup_binja.sh + docs/binja-module.md
bash scripts/setup_binja.sh /path/to/binja-headless-6.0-linux.tar.gz
PYTHONPATH=$HOME/binja/python python3 scripts/smoke_binja.py   # exit 0 = ready

# 4. Ground truth (needs Binja; extracted from the unstripped twin of the target)
PYTHONPATH=$HOME/binja/python python3 eval/cjson/extract_ground_truth.py

# 5. Full pipeline (2=graph, 3=init, 4=type recovery, 5=pass1, 6=pass2, 7=invest+resynth+export)
PYTHONPATH=$HOME/binja/python python3 -m reaper.run \
    --binary eval/cjson/cjson_test --config configs/default.toml \
    --run-id cjson_001 --phases 2,3,4,5,6,7

# 6. Evaluate (6 metrics) + LLM-judge scoring (N independent zero-context runs)
python3 eval/evaluate.py --ground-truth eval/cjson/ground_truth.json \
    --reaper-output data/cjson_001_reaper_output.json --llm
python3 -m reaper.eval.scoring --ground-truth eval/cjson/ground_truth.json \
    --reaper-output data/cjson_001_reaper_output.json --n-runs 5
```

### Live run monitoring (Run Cockpit)

The GUI hosts the run in-process (the event bus is process-local) and can run
detached:

```bash
python -m reaper.gui.main --detach                       # background daemon on :8000
curl -s http://127.0.0.1:8000/api/state                  # run status / phases / files
curl -s "http://127.0.0.1:8000/api/events?after=0"       # live event feed
curl -s http://127.0.0.1:8000/api/loops                  # counters vs caps
python -m reaper.gui.main --status | --stop
```

## Evaluation

- **Ground truth** (`eval/evaluate.py`): 6 metrics — function-name exact,
  semantic (optional LLM judge), variable-name, claim coverage, type-recovery
  accuracy (offset+size), and false-confidence rate.
- **LLM-judge scoring** (`eval/scoring/`): a *modular* rubric engine that
  scores recovered function names, variable names, and recovered datatypes on a
  0-10 rubric in **N independent, zero-context LLM runs** (each run is a fresh
  query with no history), aggregated to smooth judge noise. Rubrics are a
  registry — adding a scoring dimension is a one-place change.

## Test tiers

1. **Tier 1 — offline unit gate (the release gate):** `pytest tests/ -q`
   (no Binja/Neo4j/vLLM). Uses fakes mirroring every production contract.
2. **Tier 2 — integration smoke:** `python -m reaper.run …` on the cJSON target
   (needs Binja + Neo4j + vLLM).
3. **Tier 3 — ground-truth eval:** metrics + LLM-judge scoring of real runs.

## Design

The authoritative spec is `docs/design-re-stage.md`; implementation discipline
and hard rules live in `.clinerules`, per-deliverable evidence in
`docs/skills/progress.md`, and the engineering audit + session handoff in
`continue.md`. Cross-component constructor contracts are enforced via the table
in `docs/skills/cross-references.md`.

---

MIT licensed — see `LICENSE` (if present) or the project metadata in
`pyproject.toml`.
