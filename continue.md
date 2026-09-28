# REAPER — Engineering Audit & Session Handoff

**Status:** Implementation COMPLETE — all 28 deliverables across phases 1–8 are implemented and its
unit suite is green (**204 passed**). Live facts 2026-09-27: Binja 6.0.10601 IS installed (valid
license) and the full stack now runs against real Binja + Neo4j + vLLM (see handoff below).
**Rev date:** 2026-09-27.

---

## ⚡ SESSION HANDOFF — 2026-09-27 (telemetry + GUI + live run — READ FIRST)

**Short version for resuming:** a live `cjson_001` run is IN PROGRESS right now, hosted by a DETACHED
Run Cockpit on http://127.0.0.1:8000. Do NOT start a second run; monitor/fix/tune the live one. All
work is designed to be stop/resume-safe (state persists in Neo4j + SQLite + JSONL; the GUI daemon can
be stopped/restarted without losing the run if needed).

**How to check on the run (no browser needed):**
```bash
curl -s http://127.0.0.1:8000/api/state     # run status / phases / error
curl -s "http://127.0.0.1:8000/api/events?after=0&limit=20"   # recent events
curl -s http://127.0.0.1:8000/api/loops     # live counters vs caps
tail -f data/cjson_001_events.jsonl         # debug firehose
tail -f data/cjson_001_rltrace.jsonl        # LLM turns incl. reasoning (RL corpus)
```
GUI daemon control: `python -m reaper.gui.main --status | --stop` (start again with `--detach`).

**What this session built/changed (all green, 204 tests):**
1. **Fixed 5 live-blocking bugs** (repo was previously unrunnable against real deps; mocks had
   diverged): Binja enum drift + `open_view`→`load` (tools/_compat.py, hlil_extract, GT extractor,
   smoke_binja); non-expression HLIL operands guard (walk_expr/_children); invalid Neo4j 5 GQL
   `WHERE NOT`-pattern (graph_analysis._LEAF_QUERY); inverted Call-leaf validation. Phase 2 verified
   live: real graph (124 fn, dataflow/call edges, traversal order). Ground truth regenerated (101 fn).
2. **Telemetry layer (new):** `harness/events.py` (EventBus + emit/emit_sync), `rltrace.py`
   (per-LLM-turn RL corpus incl. **reasoning/thinking**), `debugtrace.py` (full event firehose),
   `tracedriver.py` (every Neo4j query), SQLite trace callbacks (ledger/todo), extractor access
   records, Tracer→bus mirror, LLM client captures `message.reasoning` + per-level timeouts (120s→
   up to 900s; THE fix for the earlier type-recovery ReadTimeout). Design doc: docs/skills/telemetry.md.
3. **GUI "Run Cockpit" (new, gui/):** FastAPI+WebSocket, hosts the run in-process, detached daemon
   CLI (--detach/--stop/--status), REST pull APIs (/api/state, /api/events, /api/functions, /api/edges,
   /api/claims, /api/tasks, /api/loops, /api/run/start|stop), static frontend (Rename Board, Active
   Dialogues incl. reasoning, side drawers: Claims&Tasks / Call Graph / LLM Health / Trace Feed /
   Loops & Limits). Run: `PYTHONPATH=$HOME/binja/binaryninja/python ~/reaper-venv/bin/python -m reaper.gui.main --detach`.
4. **Scoring harness (new):** `eval/llm_score.py` — LLM judge with explicit rubric scoring ORIGINAL↔
   recovered names, averaged over N independent runs (default 5), resumable cache, strict one request
   at a time. Unit-tested (tests/test_llm_score.py). NOT YET RUN (waiting on the live run to produce
   reaper_output). Command in telemetry.md §10.
5. **GPU guard:** `llm_max_concurrent=1` — strictly ONE LLM request at a time (user requirement;
   justified in telemetry.md §7 limits register).
6. **opencode.json** added with TWO purposes:
   - Permission rules: auto-approves routine dev commands (Python/pytest/git/curl/docker edge), still
     asks on rm/kill. Requires opencode RESTART to take effect.
   - DeepSeek reasoning variants: per-model `options.reasoningEffort` + `variants` minimal→xhigh for
     provider `deepseek`/model `deepseek` (defaults to xhigh). Also requires restart. If provider id
     differs in the user's setup, rename the key.
7. **REAPER "xhigh" reasoning tier (new):** added to harness — `_THINKING_BUDGETS["xhigh"]=81920`,
   timeout 1500s, and sends `chat_template_kwargs={"reasoning_effort":"high"}` (verified accepted by
   the live :8035 vLLM). `configs/default.toml [thinking_levels] recovery_followup = "xhigh"`. This is
   the harness-side lever; opencode's own model is a separate consumer (see #6).

**LATE-SESSION UPDATE (same day, READ):**
8. **Modular scoring engine (expanded from #4):** `eval/scoring/` replaces the single-file
   `eval/llm_score.py` (now a compat shim). Rubric REGISTRY (function-name / variable-name /
   datatype), JudgeEngine enforcing the **independence contract**: each scoring run is ONE fresh,
   zero-context query (session created per (item,run), destroyed after — never reused; unit-tested).
   Resumable JSONL cache per (rubric,item,run). ScoreScheduler = the "scheduler" the design asked for:
   takes the evidence (fn+var names, datatypes) and just scores it via the rubric N independent runs —
   no BNDB parsing/tool-calling. Multi-dimension CLI:
   `python -m reaper.eval.scoring --ground-truth ... --reaper-output ... --n-runs 5`.
9. **Heavy per-mechanism dynamic tests (new):** `eval/heavy/` — 8 HeavyCase registrations (critic
   loop, renaming, claim labelling/promotion, logic/conclusion [ReviewAgent], type recovery,
   resynthesis, scheduler, investigation). Each builds a REAL harness (SQLite ledger/todo, fake
   graph/extractor), seeds a known scenario, drives the ACTUAL production class, asserts invariants,
   and (live mode) scores the mechanism's output with the rubric engine over N independent runs.
   Offline = scripted StubLLM, deterministic CI gate (tests/test_heavy_cases.py). Runner:
   `python -m reaper.eval.heavy.runner [--case X] [--mode offline|live]`.
10. **Live-found bug, fixed + regressed:** heavy renaming (live) exposed that the model returns bare
    variable suffixes (`zmm15`) instead of stable node ids (`0x1400:zmm15`); Pass1Dispatcher's ':'
    check mis-read those as FUNCTION addresses → dropped renames + 6 spurious `:Function` nodes in the
    live graph. Fix: RenameVariableAgent + ReviewAgent normalize bare rename ids to `<func>:<name>`
    (both are strictly function-scoped calls). 6 spurious nodes cleaned from live Neo4j. Regression
    tests added. **Post-fix agents must be restarted to take effect on a resumed/future run.**

**Recommended next actions (order):**
1. While run continues: watch counters; if Phase 4+ degrade, tune via configs/default.toml (all caps
   justified + live-observable). Do NOT remove llm_max_concurrent guard.
2. **ETA:** live run is in Phase 5 (Pass 1 renames). Graph has ~3,449 Variables + 186 Arguments and
   renames run strictly one-at-a-time at "minimal" (~13s each live) → Phase 5 is a ~13h sweep; Phases
   6–7 follow. Do NOT restart; let it run. It is stop/resume-safe if needed.
3. When status == complete: `python -m reaper.eval.scoring` (5 runs, multi-dimension) +
   `eval/evaluate.py` (6 metrics) on `data/cjson_001_reaper_output.json`; document scores in §10
   telemetry doc + this file. Optionally run `python -m reaper.eval.heavy.runner --mode live` to
   exercise the mechanisms against the real model.
4. Update docs/skills/progress.md with live-run evidence; commit the working tree (many uncommitted
   changes above, including new gui/ + harness telemetry + docs + eval/scoring + eval/heavy).

---

> This file REPLACES the original *build-time* continue.md. The old per-deliverable build order and
> anti-pattern warnings are preserved in condensed form in §9 and remain authoritative in
> `docs/design-re-stage.md` (the spec), `docs/skills/progress.md` (per-deliverable evidence + issue log),
> and `.clinerules` (hard rules).

---

## 0. TL;DR — current state (verified 2026-09-27)

| Item | Verified state |
|---|---|
| Code / deliverables | All 8 phases implemented; `docs/skills/progress.md` boxes all checked |
| Unit tests | **160 passed** (`python3 -m pytest -q` from repo root, 7.3s, Binja/Neo4j/vLLM NOT required) |
| Neo4j | LIVE: bolt://localhost:7687, neo4j/reaper (docker `reaper-neo4j`, image neo4j:5.26). Schema + 5 UNIQUE constraints verified. Graph **EMPTY** (0 `:Function` — no binary ingested yet) |
| vLLM | LIVE on :8035 — `GET /v1/models` returns exactly one model: `deepseek` (max_model_len 524288). `configs/default.toml` [vllm] updated to match |
| Binary Ninja | NOT installed. `~/binja` exists but is EMPTY (no `python/binaryninja`). License **present** at `~/.binaryninja/license.dat` (1102 B). The only archive on disk, `/tmp/binja.6.zip` (550 MB), is the **Windows NSIS installer** — confirmed by `MZ` PE header — unusable on Linux |
| Artifacts | No `.bndb`, no graph data, no traces yet. `eval/cjson/ground_truth.json` is the empty `{}` placeholder (nothing extracted until Binja is up) |
| LLM spend | ≈ zero — only `GET /v1/models` calls made to date |

### 0.1 The ONE blocker and how to unblock
Get the **Binary Ninja 6.0 Linux headless** tarball from `portal.binary.ninja` (NOT the Windows `.zip` in
`/tmp`), then:

```bash
bash scripts/setup_binja.sh /path/to/binja-headless-6.0-linux.tar.gz   # installs to ~/binja/python
PYTHONPATH=$HOME/binja/python python3 scripts/smoke_binja.py            # exit 0 == Phase 2 + GT ready
```

`setup_binja.sh` handles `.zip`/`.tar.gz`/`.tgz`, auto-scans `~/Downloads`, `/tmp`, `$HOME` for an archive if
no argv is given, symlinks the extracted `python/` dir to `$HOME/binja/python`, and reports whether the
license file is present. `smoke_binja.py` reproduces the exact binja-critical path (open_view → full analysis
→ HLILExtractor reads → sym/stripped address-alignment check → struct-GT probe) and exits non-zero with a
message on any failure.

After the smoke test passes:

```bash
# ground truth then full pipeline
PYTHONPATH=$HOME/binja/python python3 eval/cjson/extract_ground_truth.py
PYTHONPATH=$HOME/binja/python python3 -m reaper.run \
    --binary eval/cjson/cjson_test --config configs/default.toml --run-id cjson_001
```

---

## 1. What REAPER is

REAPER RE Stage = a fully-async Python harness that reverse-engineers a **stripped binary**: Binary Ninja
6.0 headless decompiles it to HLIL; the repo materializes a labeled dataflow graph in **Neo4j**; a team of
**LLM agents** (all one local model via vLLM, differentiated by role/prompt) renames functions/variables,
writes evidenced claims with discrete truth values, runs an adversarial critic and deep-review (Pass 2),
then investigation + resynthesis loops until an evidence-coverage completion criterion is met. Output is a
serialized "draft CPG" (`<data_dir>/<run_id>_reaper_output.json`) plus a mutated `.bndb`, which are scored
offline against ground truth by `eval/evaluate.py` (6 metrics); the CPG is the intended substrate for the
(not-yet-built) VR stage.

Design assumptions (docs/outline-v2.md, still authoritative):
- One local model for all agents; quality is tuned by per-stage *thinking level*, not model choice.
- True concurrency via vLLM + asyncio; context/history managed CPU-side by the harness.
- One binary at a time (cross-binary interactions are future work).
- Log everything to structured traces (for dashboarding / later RL).

---

## 2. Repository map — the root IS the package

`pyproject.toml` maps package `reaper` onto the repo root
(`[tool.setuptools.package-dir] "reaper" = "."`), so there is no physical `reaper/` subdir in git.
`reaper.harness` == `harness/`, `reaper.tools` == `tools/`, etc. Older docs that say `reaper/tools/...` mean
these top-level dirs. Installed into `~/reaper-venv` (Python 3.12.3) via `pip install -e .`; system
`/usr/bin/python3` (3.12.3) also has pytest and runs the suite fine.

| Path | Role |
|---|---|
| `run.py` | End-to-end pipeline runner (Deliverable 8.2 CLI). Only top-level entry point |
| `configs/default.toml` | All runtime config (neo4j, vllm, thinking levels, limits, paths) |
| `infra/` | Neo4j docker-compose, `schema.cypher`, `init_db.py` (`init_schema`) |
| `harness/` | LLM client, tracer, ledger, todo, context assembler, shadow copies, merge, dispatchers, scheduler, investigation + resynthesis loops, submission protocol |
| `agents/` | The 6 LLM agent classes + `prompts/*.txt` system prompts |
| `tools/` | Binja compat shim, HLIL extractor, Neo4j graph builders/analysis, BNDB writer, struct detector/rebuild, report exporter |
| `eval/` | Ground-truth evaluation (6 metrics) + cJSON test target + Juliet/PrimeVul corpora |
| `scripts/` | `setup_binja.sh`, `smoke_binja.py` (Binja install/gate — added 2026-09-27) |
| `tests/` | 26 modules + fake Binja/Neo4j/vLLM doubles |
| `docs/` | `design-re-stage.md` (spec, ~1786 lines), `binja-module.md` (API surface), `skills/*` (patterns/cross-refs/progress), `testing-plan.md`, `outline-v2.md`, `open-questions.md` |
| `data/` | Intended runtime outputs (corpora/results/targets) — see audit finding #2 |

Module line-count scale for orientation: `harness/context.py` (587), `harness/ledger.py` (441),
`harness/todo.py` (356), `tools/_compat.py` (341), `tools/struct_detector.py` (278), elsewhere 60–280.

---

## 3. Architecture & data flow

```
 stripped binary
      │  binja headless 6.0 (open_view, full analysis)    ← BLOCKED until installed
      ▼
 [tools/hlil_extract HLILExtractor]  ⇒  readable HLIL text + vars/params/string-refs + .bndb (target.bndb)
      │
      ▼  async Cypher (MERGE only, idempotent)
 [Neo4j master graph]  Function* | Variable* | Argument* | Call | StringRef | Struct
      │  edge types: CONTAINS, DATAFLOW_ASSIGN, DATAFLOW_ARG, CALL, RETURN, REFS_STRING,
      │             FIELD_OF, DEFERRED_BACK  (*=stable node ids derived from ORIGINAL names)
      │  + pin_symbols (pinned=true) + validate_and_order (leaf check, Tarjan SCC, traversal_order)
      │
      ▼ agents (single base URL/model; roles by prompt):
 Phase 4 type recovery  → StructAccessDetector → TypeRecoveryAgent → GraphRebuilder (mutates Binja types!)
 Phase 5 Pass1 sweep    → RenameVariableAgent / FunctionSummary, bottom-up by traversal level, minimal thinking
 Phase 6 Pass2 deep rev → ReviewAgent → CriticEvaluator (retry w/ feedback) → MergeAgent (shadow-diff/conflict)
 Phase 7 investigate    → Scheduler → InvestigationAgent → CriticEvaluator → InvestigationLoop (deadlock-free)
 Phase 7 resynthesize   → ResynthesisAgent (per SCC/struct/call groups) → ResynthesisLoop (capped) → is_re_complete
      │
      ▼
 [SQLite ledger <run>_ledger.db]   claims + truth values + structs   (Neo4j first, SQLite second, no rollback)
 [SQLite todo   <run>_todo.db]     task queue/deps                       (writes under asyncio.Lock, WAL)
 [traces/<run>/trace.jsonl]         structured event log                  (Tracer, per-write lock)
 [data_dir/<run>_reaper_output.json]  final evaluation input (tools/export_report + BNDB/ledger/master-state)
```

Run phases (run.py `--phases`, default `2,3,4,5,6,7`; resumable because state persists in Neo4j+SQLite):

- **2 graph build** — HLIL extract + build_nodes/build_edges/pin_symbols/validate_and_order.
- **3 harness init** — LLM client, ledger, todo, context assembler, shadow manager, BNDB writer, merge agent. Implied by any later phase.
- **4 type recovery** — struct candidate scan → LLM struct inference → graph rebuild + ledger.record_struct.
- **5 Pass 1** — bottom-up variable/argument renames + function summaries (minimal thinking, no critic).
- **6 Pass 2** — per-function deep review → critic (retry-with-feedback) → task critic → merge (shadow-conflict-aware).
- **7 investigation + resynthesis + export** — scheduler/todo drain, resynthesis loop, completion gate, final export.

Write discipline (hard rules): ALL Neo4j writes are `MERGE` (idempotent by design); Neo4j is written FIRST,
SQLite second; a SQLite failure after a successful Neo4j write is logged, never rolled back; every aiosqlite
connection uses `PRAGMA journal_mode=WAL` + `PRAGMA foreign_keys=ON` and all writes go through a per-instance
`asyncio.Lock`. Structured LLM output uses the OpenAI-standard `response_format` json_schema —
verified live 2026-09-27 on :8035; `extra_body.structured_outputs` and `guided_json` are silently
ignored by this vLLM build (see §10.0 finding #10).

---

## 4. Component audit

Each entry: purpose • design choices • interface contract • dependencies • gaps/risks.

### 4.1 Orchestrator — `run.py` (D8.2)
- **Purpose:** single entry point wiring every component in exact §8.2 order.
- **Design:** phase-gate with resumption; `_compat.BINJA_AVAILABLE` warning when Binja is missing; final hygiene
  block (sweep NULL claims → claim-coverage stats → export reaper_output → bind pipeline_complete trace).
- **Contract:** `python -m reaper.run --binary <path> --config <toml> --run-id <id> [--phases 2,3,4,5,6,7]`; console
  script `reaper` maps to `reaper.run:main` (the argparse wrapper defined at module bottom).
- **Gaps:** (a) unguarded `tracer.log("pipeline_phases_done", …)` raises ValueError today → audit finding #1;
  (b) `main` name is shadowed (async `main(...)` at top, sync wrapper `main()` below) — confusing but functional;
  (c) NOT covered by any unit test (finding #8); (d) no `--dry-run`/JSON-log mode.

### 4.2 `infra/` — Neo4j bootstrap (D1.2)
- **Purpose:** idempotent schema install (`init_schema`) + `docker-compose.yml` for the graph store.
- **Design:** `schema.cypher` uses `IF NOT EXISTS`; constraints verified afterward (Function.address,
  Variable.id, Argument.id, Call.id, StringRef.id); indexes on scc_id/traversal_order/address. Neo4j 5.26 with
  password-length floor relaxed to 4 to permit the 6-char `reaper`.
- **Gaps:** `verify_schema()` swallows all exceptions (health-check only). Constraint set is intentionally
  minimal (no Struct/edge constraints).

### 4.3 Harness — `harness/`

**`submission.py` (D3.3) — the shared I/O protocol.**
- All Pydantic models live here and double as vLLM structured-output schemas (`model_json_schema()`): Rename,
  EvidenceLink, Claim, Submission, CriticVerdict/CriticOutcome, TaskContextSpec, TaskSpec, ReviewOutput,
  InvestigationResult, Contradiction, MergedClaim, StructField, StructDefinition, FunctionSummary,
  ResynthesisResult, MergeResult, MergeDecision. Truth levels: speculation/inferred/low/mid/high_confidence.
- Helpers: `get_schema`, `parse_response` (tolerates fenced ```json, scans for first `{…}`), `send_structured`
  (auto-re-prompts once on ValidationError).
- Exceptions to the rule are documented: StructCandidate/FieldAccess live in tools/struct_detector.py;
  LLMTransientError in harness/llm_client.py.
- **Gap:** no `total_get_claims_by_runid` style aggregate; get_claims is per-function only.

**`llm_client.py` (D1.3) — vLLM client.**
- Per-session in-memory convo history + lock; retry-with-backoff (1/2/4s) on 5xx/timeouts only (4xx fatal);
  thinking_level → TOTAL `max_completion_tokens` budget {minimal 1024 … max 40960}; structured output via
  OpenAI `response_format` json_schema (see §10.0 finding #10 — `extra_body`/`guided_json` are ignored by
  the live :8035 build). History appended only on success. base_url normalized to server root (found #9).
- **Gap:** connection is per-instance `httpx.AsyncClient`; no auth/token or /v1/models negotiation. v1
  assumptions hold for a single-model endpoint (config model = `deepseek`).

**`tracer.py` (D1.4) — structured JSONL trace.**
- One locked `trace.jsonl` per (traces_dir/run_id); STRICT event-type allowlist (12 fixed types) → raises
  ValueError on unknown type. **This strictness is the root of finding #1** — the pipeline emits
  `pipeline_phases_done`, `resynthesis_iteration`, `completion_coverage`, `scheduler_*`, `pass1_*`, `pass2_*`,
  `investigation_*`, `claim_accepted/…`, `review_agent`, etc., which are NOT in the allowlist. Harness callers
  guard with try/except (silently dropped); run.py does not.

**`ledger.py` (D1.5) — function notepad (claims/evidence/structs) in SQLite.**
- Tables: functions, claims (truth_level NULL until critic), evidence_links, structs, struct_fields.
- Write lock + WAL; Neo4j-sync helper `register_functions_from_graph` (INSERT OR IGNORE from `:Function`
  addresses); claim coverage stats; `sweep_null_claims()` deletes unevaluated claims before export;
  `record_struct`/`get_structs` power metric 5 export.
- **Gap:** no summary index / full-text; all claim reads are by function address.

**`todo.py` (D1.6) — task queue.**
- No `rejected` status (reject → pending + critic_feedback + rejection_count); dependency join table;
  `get_ready_tasks()` requires all deps completed (missing/depleted deps are treated as satisfied →
  deadlock-avoidance); `reset_stale_in_progress` by `started_at` (assignment time).

**`context.py` (D3.1) — the context assembler (largest harness module, 587 lines).**
- Public methods: for_function, for_variable (>>> highlight <<<), for_evidence, for_struct_candidate,
  for_task, for_subgraph. Truncation via `_finalize` (chars/4 token estimate vs max_context_tokens 32768);
  for_subgraph implements the design's 3-step ladder (drop low-confidence claims → summarize HLIL to
  first/last 10 lines → drop least-connected funcs). Degrades gracefully on any read failure (section omitted).
- **Gap:** `_highlight` word-boundary regex is the rename-agent contract; if Binja renamed the variable live
  the highlight falls back to a plain annotation (documented safety behavior).

**`shadow.py` (D3.2) — checkout/diff/apply for agent working state.**
- N-hop neighborhood (default 2) via :CALL/:CONTAINS; in-memory monotonic version counter; conflict iff
  master_version > checkout_version AND master value ≠ value seen at checkout. Claims never conflict.
- Neo4j-first/SQLite-second apply; property writes whitelisted (`_ALLOWED_FIELDS`) to keep Cypher injection-safe.
- **Gap:** version counter is in-memory only (resets each run — acceptable: conflicts reproduce within a run).

**`merge_agent.py` (D3.5) — conflict resolution.**
- diff vs baseline (auto-checkout if missing) → applied | resolved (LLM MergeDecision.resolve per conflict)
  | rejected (creates TODO follow-up tasks). BNDB kept in sync on every successful apply.
- **Gap:** resolution values are taken on faith from LLM (no re-parse/schema coercion).

**`pass1_dispatcher.py` (D5.2) / `pass2_dispatcher.py` (D6.3).**
- Pass1: level-gated by traversal_order (levels awaited), variables→arguments→summary per function,
  semaphore-scoped (max_concurrent_agents 8), DIRECTLY applies (no critic), then SCC argument+summary
  second pass, then bndb save.
- Pass2: same level gating; per function: ReviewAgent → claims→ledger→critic (delete-on-reject, retry up to
  max_review_retries with feedback) → task light-critic → rename merge. Renames stay decoupled from claim
  acceptance by design (a rejected review still contributes merge-verified renames; claims excluded from the
  merge Submission to avoid double-insertion).

**`scheduler.py` (D7.1) / `investigation_loop.py` (D7.3) / `completion.py` (D7.5).**
- Scheduler: batch (≤20) pending task review for MERGE/DECOMPOSE recommendations; assigns ready tasks.
- InvestigationLoop: owns its OWN CriticEvaluator (never shared); claim→critic; subtasks created independently
  then parent made to depend on them (one-way, deadlock-free); requeue-with-feedback on all-reject; stuck
  detection logs `investigation_stuck`; bounded by max_investigation_iterations (200).
- ResynthesisLoop: per SCC/struct/call-group resynthesis; stops when an iteration creates 0 new tasks;
  `is_re_complete` = every RENAMABLE (non-pinned) function has ≥1 evaluated claim AND todo empty.

### 4.4 Agents — `agents/` (single model, role-differentiated by `prompts/*.txt`)

- **rename_variable.py (D5.1)** — one rename per call from `for_variable` context; minimal thinking; Submission schema.
- **type_recovery.py (D4.2)** — StructCandidate → StructDefinition; returns None when fields empty; records
  one claim per involved function at truth_level `inferred` (v1: critic does not revisit).
- **review_agent.py (D6.2)** — read-only deep review → ReviewOutput{renames, claims, tasks}; accepts
  `prior_feedback` for critic-retry.
- **critic_agent.py (D6.1)** — `CriticEvaluator` (harness-shared class): evaluates a claim already in the ledger;
  rejection counts tracked per (func, role) IN MEMORY; policy = accept→set level → reject&count<limit→DELETE
  claim (clean retry) → count≥limit→force-accept at `speculation`.
- **investigation_agent.py (D7.2)** — one task → InvestigationResult{answer, claims, subtasks}.
- **resynthesis_agent.py (D7.4)** — one call over for_subgraph context → ResynthesisResult.
- **Prompts (`agents/prompts/*.txt`):** rename_variable, function_summary, type_recovery, critic_agent,
  review_agent, merge_agent, investigation_agent, resynthesis_agent — each < 50 lines, written and tested.

### 4.5 Tools — `tools/` (the graph + Binja-touching layer)

- **`_compat.py` (not a deliverable)** — guards EVERY `binaryninja` import; `BINJA_AVAILABLE` flag; fallback
  `Op` enum mirroring HighLevelILOperation so pure logic + fake-HLIL tests run with/without Binja; `walk_expr`
  structural tree walker; `require_binja()` raises a clear, actionable RuntimeError only when a live view is
  actually needed. **This is the linchpin that keeps the whole package importable + testable without Binja.**
- **`hlil_extract.py` (D2.1)** — `HLILExtractor(binary_path, data_dir)` opens the view, creates `target.bndb`,
  exposes serialized text contract (get_function_hlil/signature/variables/parameters/string_refs,
  get_hlil_range). String refs deliberately detect HighLevelILConstPtr only (NOT ConstData) — per binja-module.md.
- **`graph_nodes.py` (D2.2)** — Function/Variable/Argument/Call/StringRef MERGEs + :CONTAINS. Stable ids:
  Variable `0x<fn>:<orig_name>`, Argument `0x<fn>:arg<N>`, Call `0x<fn>:call_0x<addr>`, StringRef `str:0x<addr>`.
- **`graph_edges.py` (D2.3)** — dataflow edges (DATAFLOW_ASSIGN/ARG, CALL, RETURN, REFS_STRING); params resolve
  to :Argument ids so parameter edges are never silently dropped; control-flow recursed for hidden calls.
- **`graph_analysis.py` (D2.5, 7.4)** — (1) leaf validation (leaves must be Variable/Argument/StringRef),
  (2) Tarjan SCC in pure Python (no GDS), back-edge relabel `:DEFERRED_BACK`, (3) traversal_order (async),
  (4) `compute_resynthesis_groups` (SCC ∪ struct-consumers ∪ call-neighborhoods, union-find dedup).
- **`pin_symbols.py` (D2.4)** — imports/library/named (non-`sub_`) functions → pinned=true + canonical name;
  StringRefs pinned. Runs after 2.2; pinned functions excluded from rename passes and completion scoping.
- **`bndb_writer.py` (D3.4)** — writes renames/types into the live view; `_rename_map` (node_id→current Binja
  name) handles second+ renames where Binja's single name ≠ original node-id suffix; llm_name stored as a user
  tag; `save()` calls require_binja (Duck-typed apply methods work against fakes).
- **`struct_detector.py` (D4.1)** — pure HLIL pattern walker: `*(base+off)` / `base->field` / `*base`;
  conservative grouping (within-function by base var; cross-function ONLY when Binja gives a meaningful shared
  type; unknown types never bridge). `_type_size` is x86-64-biased (pointers→8) — MUST stay consistent with
  `extract_ground_truth._struct_layouts` (mirrors it).
- **`graph_rebuild.py` (D4.3)** — after type recovery: build C struct text → `set_struct_type` on Binja → MERGE
  :Struct node → re-detect accesses → materialize per-field :Variable nodes with :FIELD_OF → prune old
  `@0x…` placeholder variables → re-run validate_and_order. Idempotent (MERGE only).
- **`export_report.py` (D8.4)** — serializes master graph + ledger + extractor into the `evaluate.py` JSON
  shape (function names from Neo4j master, vars by stable ordinal, claims + evidence, `structs` for metric 5).

### 4.6 Evaluation — `eval/`

- **`cjson/` (D8.1):** `build.sh` pins cJSON v1.7.18, builds `cjson_test_symbols` (unstripped, DWARF) +
  `cjson_test` (STRIPPED, dynamic linking so imports exist for Pass -1). `extract_ground_truth.py` compares
  sym vs stripped by address (only symbols that SURVIVE stripping count), and now emits `ground_truth["structs"]`
  = {name: [{offset,size,name,type_str}]} from the unstripped DWARF view (libc/`_`-prefixed/ALLCAPS omitted).
  Without Binja it writes `{}` (the CURRENT on-disk state).
- **`evaluate.py` (D8.3):** 6 metrics — (1) function exact, (2) semantic (exact + optional LLMJudge),
  (3) variable accuracy matched by address+position (exact + optional judge), (4) claim coverage (% fns with
  ≥1 mid_confidence+ claim), (5) type recovery accuracy (% GT struct fields matched by (offset,size) — name
  NOT required), (6) false-high-confidence rate. Pure except optional judge. CLI adds `--base-url`/`--model`
  (DEFAULTS ARE STALE — see finding #3) and writes `evaluation_report.json`.
- **`juliet/`, PrimeVul tooling:** documented in docs/testing-plan.md; out of scope for next run.

### 4.7 Scripts — `scripts/` (added 2026-09-27)

- `setup_binja.sh` — install/verify headless drop-in; see §0.1.
- `smoke_binja.py` — 5-step gate (import/version, open_view, extractor reads, sym↔stripped alignment,
  struct-GT probe), exit 0 = Phase-2 ready. Hardcoded to the cJSON binaries by design.

---

## 5. Cross-cutting design decisions (and why)

1. **Repo root IS the package** — avoids a `reaper/` nesting layer; doc paths map 1:1. Cost: relative
   config/data paths resolve against CWD (finding #2).
2. **Stable node ids from ORIGINAL names** — renaming never changes a node's identity; BNDBWriter's
   `_rename_map` exists precisely because Binja keeps only ONE name (the original is lost after first rename).
3. **MERGE-only writes** — idempotent re-runs after crashes; matches the async, no-transaction reality.
4. **Neo4j-first, SQLite-second, log-don't-rollback** — accepted non-atomicity; Neo4j is the master.
5. **Binja behind `_compat`** — everything importable/tests green without Binja; only view-requiring ops raise.
6. **One model, role/prompt differentiation + per-stage thinking level** — DeepSeek-V4-Flash class model;
   cost knob is thinking budget, not model tier (outline §3).
7. **`structured_outputs` via extra_body, never guided_json** — vLLM silently ignores guided_json; verified.
8. **No shared critic instances; per-dispatcher/per-loop evaluators** — isolation it avoids cross-talk.
9. **Critic DELETES rejected claims** (with per-func/role rejection counts) — forces genuinely cleaner
   resubmission, then force-accepts at `speculation` (never infinite-loops) and sweep_null_claims before export.
10. **Completion scoped to RENAMABLE functions** — pinned symbols (imports/library) are given, so they never
    block completion (claims never required for them).
11. **Deadlock-free tasking** — subtasks do NOT depend on the parent (one-way parent→subtask); stale
    in_progress tasks reset on assignment-timeout; missing deps treated satisfied.
12. **Truncation ladders + graceful degradation** — context assembler must never crash the pipeline; every
    read is best-effort with a logged omission.
13. **Address-position variable matching** in evaluation (not by name) — works on stripped targets where
    original identifiers are gone.
14. **retry-with-same-session** for structured-output parse slippage — cheaper than full resubmit; history
    carries the prior attempt.

---

## 6. Data contracts & invariants (do not break)

**Node id formats** (used by shadow/merge/BNDBWriter/export — change only alongside all consumers):
`Variable: "0x<fn>:<orig_var_name>"` · `Argument: "0x<fn>:arg<index>"` · `Call: "0x<fn>:call_0x<addr>"`
· `StringRef: "str:0x<addr>"` · `Struct: "0x<addr>:struct:<name>"` (graph_rebuild) · offset placeholders
`@0x…` (pre-type-recovery, pruned by graph_rebuild).

**Neo4j node props:** every Function carries name/address/pinned/ambiguous/imported + llm_name/canon_name
(set by renames) + scc_id/traversal_order/isolated. Edge types: CONTAINS, DATAFLOW_ASSIGN, DATAFLOW_ARG,
CALL, RETURN, REFS_STRING, FIELD_OF, DEFERRED_BACK. Only `_ALLOWED_FIELDS` (shadow) are mutable.

**Claim lifecycle:** insert truth_level=NULL → critic → accepted(level set) | rejected(delete, retry)
| force-accepted(speculation) | swept (sweep_null_claims before export).

**Completion gate:** `is_re_complete` = (all RENAMABLE functions have ≥1 evaluated claim) AND (todo empty);
coverage stats give partial credit; pinned functions excluded throughout.

**Config keys (configs/default.toml):** `[neo4j] uri/user/password` · `[vllm] base_url/model` ·
`[thinking_levels]` (pass0_type_recovery, recovery_followup, pass1_rename, pass2_review, critic, investigation, merge,
resynthesis, scheduler) · `[limits]` (max_critic_rejections, max_review_retries, max_resynthesis_iterations,
max_investigation_iterations, max_type_recovery_rounds, shadow_copy_hops, task_timeout_seconds, max_concurrent_agents,
max_context_tokens) · `[paths] data_dir, traces_dir`. NEVER add binaryninja to requirements.txt.

**Thinking levels audited 2026-09-27** (policy: quality-gated / load-bearing → max/high, else minimal):
`pass2_review = "max"` · `critic = "high"` · `investigation = "high"` (claim-producing / gate stages;
investigation claims are critic-gated, task requeued on total rejection). `merge = "max"` and
`resynthesis = "max"` per the 2026-09-27 decision: merge-resolution correctness is final for renames
(no later gate) and resynthesis edits the ledger directly (merged/deleted claims) — both treated as
load-bearing. `pass0_type_recovery = "high"` and `recovery_followup = "max"` per the 2026-09-27
decision: struct layouts feed metric-5 + every later field rename (load-bearing, high for the pass-0
round), and ANY follow-up recovery round (new access patterns exposed after a struct application)
gets the maximum budget (xhigh). Only genuinely ungated/procedural stages stay minimal: `pass1_rename`,
`scheduler`. The Phase-4 fixed-point loop is capped by `max_type_recovery_rounds`.

**Constructor signatures are THE contract** between run.py and every component — full table in
`docs/skills/cross-references.md`; module-level async fns (not classes) for build_nodes/build_edges/
pin_symbols/validate_and_order/compute_resynthesis_groups/init_schema/is_re_complete.

---

## 7. Environment & prerequisites (current, verified 2026-09-27)

| Component | Value | Notes |
|---|---|---|
| OS / shell | Linux; run as `police`; HOME=/home/police | |
| Python | 3.12.3 (`/usr/bin/python3`); `~/reaper-venv` (3.12.3) holding `pip install -e .` | pytest runs under system python3 too |
| Neo4j | bolt://localhost:7687 neo4j/reaper | docker compose `infra/docker-compose.yml`; container `reaper-neo4j`; empty graph |
| vLLM | http://localhost:8035/v1, model `deepseek` | shared with other jobs — ask before LLM-heavy phases 5–7 |
| Binja | NOT installed; license at ~/.binaryninja/license.dat | only `/tmp/binja.6.zip` (Windows NSIS, MZ) on disk |
| gcc / strip | present | cJSON binaries already built |
| Binja venv path convention | `$HOME/binja/python` (or `$HOME/binja-headless/python`) on PYTHONPATH | per .clinerules/docs; setup_binja.sh normalizes |

Note: config `[paths] data_dir = "reaper/data"`, `traces_dir = "reaper/data/traces"` — relative to CWD this
creates a literal `reaper/data` subdir (finding #2). The root `data/` dir (corpora/results/targets) is the
intended home.

---

## 8. Test architecture

Three-tier strategy (docs/testing-plan.md):
- **Tier 1 — unit, offline (the release gate):** `pytest tests/ -q` → currently **160 passed**; needs NO
  Binja/Neo4j/vLLM. Uses `tests/fake_binja.py` (fake HLIL structs + FakeExtractor/FakeBinaryView),
  `tests/fake_harness.py` (ScriptedExtractor honoring the REAL string/text 2.1 contract, StubLLM,
  RecordingTracer), `tests/fake_neo4j.py` (MemoryNeo4jDriver — Cypher-subset in-memory) and
  `tests/conftest.py` (canned FakeNeo4jDriver + a REAL threaded fake-vLLM HTTP server for retry tests).
- **Tier 2 — integration smoke (needs Binja+Neo4j+vLLM):** `python -m reaper.run …` on cJSON (see §11).
- **Tier 3 — ground-truth eval:** `eval/evaluate.py` (6 metrics) against `ground_truth.json`.
- **Notable gap:** there is NO `tests/test_run.py` — the run.py orchestration (and the tracer-under-
  allowlist path) is unexercised by the suite. Adding a run.py phase-orchestration unit test that reaches
  the end-of-run tracer calls would have caught finding #1.
- All 26 test modules mirror production names (test_context, test_ledger, …) and are watched by the
  cross-references.md import table.

---

## 9. Deliverable map (implementation complete) + hard rules

All 8 phases / 28 deliverables are implemented (progress.md all boxes checked). Deliverable → module:

| D | Module | D | Module | D | Module |
|---|---|---|---|---|---|---|
| 1.1 skeleton | root/*, pyproject, configs | 2.3 edges | tools/graph_edges | 5.1 rename | agents/rename_variable |
| 1.2 neo4j | infra/*, init_db | 2.4 pin | tools/pin_symbols | 5.2 pass1 | harness/pass1_dispatcher |
| 1.3 llm | harness/llm_client | 2.5 analysis | tools/graph_analysis | 6.1 critic | agents/critic_agent |
| 1.4 trace | harness/tracer | 3.1 context | harness/context | 6.2 review | agents/review_agent |
| 1.5 ledger | harness/ledger | 3.2 shadow | harness/shadow | 6.3 pass2 | harness/pass2_dispatcher |
| 1.6 todo | harness/todo | 3.3 protocol | harness/submission | 7.1 sched | harness/scheduler |
| 2.1 extract | tools/hlil_extract | 3.4 bndb | tools/bndb_writer | 7.2 invest | agents/investigation_agent |
| 2.2 nodes | tools/graph_nodes | 3.5 merge | harness/merge_agent | 7.3 loop | harness/investigation_loop |
| 4.1 detector | tools/struct_detector | 4.2 type agent | agents/type_recovery | 7.4 resynth | agents/resynthesis_agent |
| 4.3 rebuild | tools/graph_rebuild | 8.1 cjson GT | eval/cjson/* | 7.5/8.2/8.3 | completion/run/evaluate |

Non-negotiable rules (full text in `.clinerules`): ALL async I/O; httpx/neo4j.AsyncDriver/aiosqlite only;
structured output via `extra_body` (never guided_json); MERGE never CREATE; WAL+FK+asyncio.Lock on SQLite;
`PYTHONPATH=$HOME/binja/python` (never pip-install binja); `func.hlil.instructions` not `hlil.root`;
string refs via HighLevelILConstPtr not ConstData; module-level async fns for graph builders; stable ids
never change; constructors must match cross-references.md; `tomllib` never tomli.

---

## 10. Audit findings — confirmed issues + recommended fixes (ranked)

**STATUS (2026-09-27, bug-fix pass):** findings **#1, #2, #3, #7, #8 are FIXED** and verified — 186 tests
green, including new regression tests in `tests/test_tracer.py` (all 24 emitted event types accepted) and
`tests/test_run.py` (end-to-end Phase-3 smoke + relative-path resolution through the real `run_pipeline`).
Finding **#4** is addressed by the checkpoint commit made at the end of this session. Findings **#5 and #6**
remain open caveats (no code change needed; just re-run ground-truth extraction after Binja is installed).

### 10.0 Fix log — applied & verified 2026-09-27

1. **#1 HIGH — tracer crash.** `_ALLOWED_EVENT_TYPES` now holds the complete vocabulary: every event emitted
   by run.py + dispatchers + loops + agents (24 types) plus the preserved reserved set. `Tracer.log` no
   longer raises on unknown types — it warns and drops the record, so tracing can never take down a live run.
   Regression guards: `tests/test_tracer.py::test_all_emitted_event_types_are_accepted` (parametrized over
   all 24) and `test_unknown_event_type_warned_and_dropped`.
2. **#2 MED — data paths.** `configs/default.toml [paths]` now `data_dir="data"`, `traces_dir="data/traces"`.
   `run_pipeline` additionally resolves relative paths against the config file's repo root (NOT the CWD) and
   pre-creates both directories, so a run from any directory lands artifacts in the intended root `data/`
   (never a literal `reaper/data`). Guard: `tests/test_run.py::test_run_pipeline_relative_paths_resolve_against_config_root`.
3. **#3 MED — eval defaults.** `eval/evaluate.py` `--base-url` default → `http://localhost:8035/v1`,
   `--model` default → `deepseek` (matching the live shared vLLM and configs/default.toml).
4. **#8 coverage hole (mitigated).** `tests/test_run.py` added — drives the real `run_pipeline` (real Tracer,
   SQLite ledger/todo, real report export) against the existing fakes, so the end-of-run trace calls are
   exercised in CI-style tests.
5. **#7 MINOR — naming.** Async orchestrator renamed `main` → `run_pipeline`; the console-script wrapper
   `def main()` is unchanged (still targeted by `reaper.run:main`).
6. **#9 (NEW, live-found 2026-09-27) — every LLM call 404'd via doubled `/v1` path.** `configs/default.toml`
   `base_url` carries the OpenAI-SDK `/v1` suffix (`http://localhost:8035/v1`), and httpx *appends* the
   request path (`/v1/chat/completions`) onto the base path — so the client was POSTing
   `http://localhost:8035/v1/v1/chat/completions` (404). `ReaperLLMClient.__init__` now normalizes base_url to
   the server root (strips a trailing `/v1`) so the explicit `/v1/...` path is never doubled. Guards:
   `test_request_path_not_doubled_when_base_url_has_v1` + `test_base_url_normalization_with_and_without_v1_suffix`
   (fake server now records request paths). Discovered by `scripts/smoke_llm_endpoint.py` (live, strictly
   serialized against the shared :8035 endpoint).
7. **#10 (NEW, live-found 2026-09-27) — structured-output mechanism silently ignored by live vLLM.** The
   design assumed `extra_body={"structured_outputs": ...}` (never `guided_json`). Live serialized probes
   proved the shared :8035 build honors ONLY the OpenAI-standard
   `response_format={"type":"json_schema","json_schema":{...}}`; `extra_body.structured_outputs`,
   `guided_json`, and `response_format.json_object` were all silently ignored (structured outputs came back
   as plain text / wrong JSON keys → unparseable agent submissions). `ReaperLLMClient.send()` now ships
   `response_format` json_schema (name derived from schema `title`, fallback `response`); a real live response
   was schema-validated. Guard: `test_structured_output_uses_response_format_json_schema`.

---

1. **[HIGH — will crash a live run] `Tracer` allowlist rejects events run.py emits.**
   `_ALLOWED_EVENT_TYPES` (harness/tracer.py) has only 12 types; `run.py` calls
   `tracer.log("pipeline_phases_done", …)` UNGUARDED in both the harness path and the graph-only path.
   Reproduced: `Tracer.log('pipeline_phases_done')` → `ValueError: unknown trace event type`. Because it sits
   inside the big try block, the exception aborts BEFORE the claim-sweep / `reaper_output.json` export, so a
   live run would finish all agent work and then crash + not produce its evaluation input. The many other
   emitted types (`pass1_*`, `pass2_*`, `scheduler_*`, `investigation_*`, `resynthesis_iteration`,
   `completion_coverage`, `claim_accepted`, `review_agent`, …) are silently dropped by the try/except wrappers
   (tracer lines never land on disk).
   **Fix (choose one, ≈1 line):** (a) extend `_ALLOWED_EVENT_TYPES` with every type emitted in run.py/harness
   (grep `tracer.log("`); or (b) make `Tracer.log` warn-and-return on unknown types instead of raising — the
   allowlist was only meant to catch typos, not to be a hard gate. Prefer (b) + broadening (a). Then ALSO add
   a `tests/test_run.py` that runs the phase orchestration with fakes so the end-of-run trace calls are
   exercised.

2. **[MED — wrong output location] `configs/default.toml [paths]` uses `reaper/data`.**
   Relative to CWD this creates a literal `reaper/data` subdir (does not exist yet) instead of the existing
   root `data/` (corpora/results/targets). The config strings came from the design doc's `reaper/...` paths
   leaking into runtime settings.
   **Fix:** set `data_dir = "data"`, `traces_dir = "data/traces"` (or resolve absolutely against the package
   root). Do this BEFORE the first live run so artifacts land in the intended place.

3. **[MED — stale endpoints in eval tooling] `eval/evaluate.py` CLI defaults are out of date.**
   `--base-url default http://localhost:8010/v1`, `--model deepseek-ai/DeepSeek-V4-Flash-0731`; the live vLLM
   is `:8035` model `deepseek` (config updated 2026-09-27, agents already use config). Any `--llm` evaluation
   will hit the wrong endpoint unless flags are passed.
   **Fix:** update the argparse defaults to `http://localhost:8035/v1` / `deepseek`, or better, read
   configs/default.toml.

4. **[LOW — hygiene] Last session's work is largely UNCOMMITTED.**
   `git status` shows 23 modified files (run.py, harness/*, agents/*, tools/*, configs, tests/*) + 4 untracked
   (`scripts/`, `tools/export_report.py`, `tests/test_export_report.py`, `tests/test_ground_truth_structures.py`).
   Commit the audit-input state as a checkpoint before the Binja install so the tracer/export fixes and any
   env changes are diffable.

5. **[CAVEAT — scoring] Metric 5 (type recovery) compares exact (offset,size) pairs.**
   On a stripped binary this can score artificially low when inferred sizes/types differ by a constant (e.g.
   pointers→8 assumption) or when Binja cannot recover sizes. If real numbers are poor, the documented
   fallback is offset-only matching — a one-line change in `eval/evaluate.py` metric-5 block.

6. **[CAVEAT — correctness of GT] `ground_truth.json` is currently the empty `{}` placeholder.**
   (Binja absent; extractor writes `{}` and prints the PYTHONPATH hint). Re-run `extract_ground_truth.py`
   after Binja is installed — do NOT skip it or evaluation silently reports 0 across every metric.

7. **[MINOR — naming] `run.py` shadows `main`** (`async def main(...)` then `def main() -> None` CLI wrapper
   at the bottom). Functionally fine (the wrapper wins; console script targets it) but confusing; rename the
   async one to `run_pipeline`/`run_phases`.

8. **[NOTE — coverage hole] No unit test exercises run.py** — root cause of how finding #1 survived 160
   green tests. Adding Tier-1 phase-orchestration tests with the existing fakes is cheap insurance.

---

## 11. Next-session runbook

1. **Obtain the Linux headless Binja 6.0 tarball** (portal.binary.ninja). If you have a `.tar.gz` path, run
   `bash scripts/setup_binja.sh <that-path>`. Otherwise place it in `~/Downloads` and run
   `bash scripts/setup_binja.sh` (it auto-scans). Reject Windows NSIS `.zip` artifacts such as
   `/tmp/binja.6.zip`.
2. **Gate:** run `PYTHONPATH=$HOME/binja/python python3 scripts/smoke_binja.py` → exit 0 = READY. If it
   fails, read the message (license / layout / API mismatch vs docs/binja-module.md).
3. **(LLM-free) Ground truth:** run
   `PYTHONPATH=$HOME/binja/python python3 eval/cjson/extract_ground_truth.py` and confirm the printed
   `n functions + n structs` and that `ground_truth.json` is non-empty.
4. **Fixes are ALREADY in place (2026-09-27):** findings #1/#2/#3/#7/#8 applied + verified — 186 tests green,
   including `tests/test_run.py` and the updated `tests/test_tracer.py` (see §10.0). Nothing to patch before
   the live run; just run it.
5. **Pause before LLM-heavy phases:** vLLM on `:8035` is shared with other jobs. Get explicit go-ahead, then
   run the graph-only phase first (LLM-free, seconds), verify Neo4j now has `:Function` nodes, then run the
   full suite: `PYTHONPATH=$HOME/binja/python python3 -m reaper.run --binary eval/cjson/cjson_test --config configs/default.toml --run-id cjson_001 --phases 2,3,4,5,6,7`.
6. **Monitor:** traces land at the configured traces_dir for run `cjson_001` (post-fix #2), ledger/todo
   SQLite files in data_dir, and the claim-coverage summary printed at end of run (`--phases 3` later
   re-exports only).
7. **Evaluate:** `python3 eval/evaluate.py --ground-truth eval/cjson/ground_truth.json --reaper-output <data_dir>/cjson_001_reaper_output.json --llm --base-url http://localhost:8035/v1 --model deepseek`.
   If metric 5 under-achieves, apply the offset-only fallback in §10 finding #5.
8. **Resume/crash handling:** no new work needed — `--phases <n>` resumes from persisted Neo4j+SQLite state.
9. **Checkpoint committed** (finding #4 — done 2026-09-27 with this bug-fix pass). After the live run, update
   progress.md / patterns.md with any surprises.

---

## 12. Command quick-reference

```bash
# offline unit gate (Tier 1)
python3 -m pytest -q

# binja install + gate
bash scripts/setup_binja.sh [/path/to/binja-headless-6.0-linux.tar.gz]
PYTHONPATH=$HOME/binja/python python3 scripts/smoke_binja.py

# rebuild cJSON target if ever needed (regenerates main.c)
(cd eval/cjson; bash build.sh)
# extract ground truth (requires Binja; overwrites ground_truth.json)
PYTHONPATH=$HOME/binja/python python3 eval/cjson/extract_ground_truth.py

# full pipeline / resume / re-export only
PYTHONPATH=$HOME/binja/python python3 -m reaper.run --binary eval/cjson/cjson_test --config configs/default.toml --run-id cjson_001 --phases 2,3,4,5,6,7
PYTHONPATH=$HOME/binja/python python3 -m reaper.run --binary eval/cjson/cjson_test --config configs/default.toml --run-id cjson_001 --phases 7     # resume later phases
PYTHONPATH=$HOME/binja/python python3 -m reaper.run --binary eval/cjson/cjson_test --config configs/default.toml --run-id cjson_001 --phases 3     # re-export report only

# evaluate (6 metrics; CLI defaults are stale so pass base-url/model explicitly)
python3 eval/evaluate.py --ground-truth eval/cjson/ground_truth.json --reaper-output reaper/data/cjson_001_reaper_output.json --llm --base-url http://localhost:8035/v1 --model deepseek

# pass0 (type-recovery) mini-corpus — ground truth WITHOUT Binja (pyelftools/DWARF)
(cd eval/pass0 && bash build.sh && python3 extract_ground_truth.py)
python3 -m pytest tests/test_pass0_eval.py -q   # offline corpus validation
# score metric 5 after a live pass0 run:
python3 eval/evaluate.py --ground-truth eval/pass0/ground_truth.json --reaper-output reaper/data/pass0_001_reaper_output.json

# infra
(cd infra; docker compose up -d)      # Neo4j 5.26 on 7687/7474, auth neo4j/reaper
# reset the graph for a fresh run (manual):
#   cypher-shell -a bolt://localhost:7687 -u neo4j -p reaper 'MATCH (n) DETACH DELETE n'
```

---

*End of audit/handoff. The build-phase guidance this file replaces is condensed into §9 and remains fully in
`.clinerules`, `docs/skills/progress.md` (per-deliverable evidence + Discovered Issues), and
`docs/design-re-stage.md` (the spec).*

