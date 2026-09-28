# REAPER — Implementation Progress

Check off deliverables as completed. Add implementation notes in the
`<!-- NOTES -->` sections — anything surprising, design decisions made,
or deviations from the design doc.

## Implementation Order

```
Phase 1 (parallel):     1.1 | 1.2 | 1.3 | 1.4 | 1.5 | 1.6
Phase 2 (sequential):   2.1 → 2.2 → 2.3 → 2.4 → 2.5
Phase 3:                3.1 → then 3.2 | 3.3 | 3.4 | 3.5 in parallel
Phase 4 (sequential):   4.1 → 4.2 → 4.3
Phase 5:                5.1 → 5.2
Phase 6:                6.1 → 6.2 → 6.3
Phase 7:                7.1 | 7.2 → 7.3 → 7.4 → 7.5
Phase 8:                8.1 → 8.2 → 8.3
```

---

## Phase 1: Infrastructure

- [x] **1.1** Project Skeleton + Dependencies
  <!-- NOTES: pyproject.toml (entry point reaper.run:main) — repo root IS the
       `reaper` package, so [tool.setuptools.package-dir] maps "reaper" = "."
       for `pip install -e .` to expose reaper.harness/tools/agents/infra.
       requirements.txt matches design doc (binaryninja deliberately absent).
       configs/default.toml = exact TOML from design doc. __init__.py files
       created for reaper/harness/tools/agents/infra. agents/prompts/ created
       (holds .gitkeep so it is tracked). Placeholder harness/llm_client.py +
       run.py stubs so the documented 1.1 test (`from reaper.harness import
       llm_client`) and the `reaper` console script work now; 1.3 / 8.2
       replace them. Installed into ~/reaper-venv (pip install -e .). -->


- [x] **1.2** Neo4j Instance + Schema
  <!-- NOTES: docker-compose.yml, schema.cypher, init_db.py.
       Verified live: init_schema() runs idempotently (MERGE/CREATE CONSTRAINT
       IF NOT EXISTS), bolt://localhost:7687 (neo4j/reaper), all 5 constraints
       present (Argument/Call/Function/StringRef/Variable UNIQUENESS).
       Docker container + venv per continue.md prerequisites. -->

- [x] **1.3** vLLM Client Wrapper
  <!-- NOTES: ReaperLLMClient, LLMTransientError, session management, retry logic.
       Implemented in harness/llm_client.py (backoff 1s/2s/4s, retries 5xx/timeouts
       only; 4xx fatal; history only recorded on success). Structured output via
       OpenAI response_format json_schema (verified live 2026-09-27 on :8035;
       extra_body structured_outputs / guided_json are silently ignored). Added
       async context manager (__aenter__/__aexit__) as a
       convenience beyond the design interface. Tests in tests/test_llm_client.py
       run against a REAL threaded fake-vLLM HTTP server (tests/conftest.py); all
       10 tests green (happy path, 503→retry→success, retry exhaustion raises
       LLMTransientError, structured_output response_format json_schema verified,
       bad thinking_level
       → ValueError, missing session → KeyError, session isolation, context mgr).
       NOTE: local env runs `python3` (no `python` alias). -->

- [x] **1.4** Logging / Trace Infrastructure
  <!-- NOTES: Tracer, JSONL format, asyncio.Lock on file handle.
       Implemented in harness/tracer.py: <log_dir>/trace.jsonl, per-write
       asyncio.Lock, async close(). UNSUPPORTED event types are now rejected with
       ValueError (validated against _ALLOWED_EVENT_TYPES) — this behavior is not
       stated in the design but is enforced by tests (3 green). -->

- [x] **1.5** Function Ledger Store
  <!-- NOTES: Ledger, 3 tables, async init pattern, write lock, foreign keys.
       Implemented in harness/ledger.py with WAL + FK pragmas, per-instance
       asyncio.Lock for writes, lock-free reads, INSERT OR IGNORE everywhere,
       add_claim inserts with truth_level NULL (critic sets it later via
       set_truth_level). all_functions_have_claims() requires EVERY :Function in
       Neo4j to have ≥1 evaluated (truth_level NOT NULL) claim. 6 tests green. -->

- [x] **1.6** TODO Ledger
  <!-- NOTES: TodoLedger, task dependencies, rejection flow (no 'rejected' status).
       Implemented in harness/todo.py; reject_task() returns task to 'pending',
       sets critic_feedback, increments rejection_count (returns the new count).
       get_ready_tasks() = pending tasks whose dependencies are ALL completed.
       Dependency-gating, reject→pending cycle, and demoed with a terminating
       accept path. 5 tests green. -->

## Phase 2: HLIL Extraction + Graph Construction

- [x] **2.1** HLIL Text Extractor
  <!-- NOTES: HLILExtractor — Binja access guarded via reaper.tools._compat so
       the module imports cleanly without Binja (unit-tested against
       tests/fake_binja.py). BNDB save on first open, func.hlil.instructions,
       string refs via HighLevelILConstPtr (NOT HighLevelILConstData), len-1
       HLIL-range evidence blocks for APIs. 7 tests green. -->

- [x] **2.2** Graph Node Builder
  <!-- NOTES: build_nodes() module-level async fn — Function/Variable/Argument/
       Call/StringRef nodes + structural :CONTAINS edges, every write MERGE
       (idempotent re-runs). Stable node IDs = func_addr + ORIGINAL Binja
       var/arg names (renames never change identity — design hard rule).
       tests/test_graph_nodes.py green. -->

- [x] **2.3** Graph Edge Builder (Dataflow)
  <!-- NOTES: build_edges() module-level async fn — instruction-type → edge
       mapping per design §2.3 table (DATAFLOW_ASSIGN/DATAFLOW_ARG/CALL/
       REFS_STRING). Direct call targets only; ambiguous indirect calls stay
       without :CALL edge (flagged ambiguous). tests/test_graph_edges.py green. -->

- [x] **2.4** Symbol Preservation (Pass -1)
  <!-- NOTES: pin_symbols() module-level async fn — import/debug/library symbols
       pinned (pinned=true; dll_exports), StringRef nodes pinned; stripped/
       unknown names left unpinned for agents. tests/test_pin_symbols.py green. -->

- [x] **2.5** Leaf Validation + SCC Detection + Traversal Order
  <!-- NOTES: validate_and_order() module-level async fn — (1) leaf validation:
       non-ambiguous Call leaves + unexpected labels raise ValueError; isolated
       Functions marked isolated=true; entry-point Functions allowed. (2) Tarjan
       SCC in PYTHON (no GDS); scc_id = scc:<lowest addr>; one back-edge
       (highest->lowest addr) relabelled :DEFERRED_BACK per SCC. (3) bottom-up
       traversal_order (leaf=0, sinks-first over condensed DAG; SCC members share
       a level; pinned/imported always 0 — written through the SAME $order param
       for write uniformity). 9 tests green after two fixture fixes (see
       Discovered Issues). -->

## Phase 3: Context Assembly + Harness Core

- [x] **3.1** Context Assembler
  <!-- NOTES: ContextAssembler — 6 public methods (for_function, for_variable,
       for_evidence, for_struct_candidate, for_task, for_subgraph) with the
       3-step truncation ladder in for_subgraph (drop sub-mid claims →
       summarize HLIL → drop least-connected). Graceful degradation: any
       Neo4j/ledger read failure logs and omits that section only.
       7 tests green (ScriptedExtractor string/text contract + responder
       driver + stub ledger). -->

- [x] **3.2** Shadow Copy Manager
  <!-- NOTES: ShadowCopyManager — checkout (N-hop via :CALL/:CONTAINS, claims for
       function-like nodes), diff (master_version > checkout_version AND master
       value changed from what the agent saw => CONFLICT; claims never conflict),
       apply (Neo4j-first via MERGE, ledger second, version counter). Verified
       with stateful in-memory doubles including the design's two-checkout
       conflict scenario. 6 tests green. -->

- [x] **3.3** Submission Protocol
  <!-- NOTES: All Pydantic models, get_schema(), parse_response().
       Implemented in harness/submission.py (Rename, EvidenceLink, Claim,
       Submission, CriticVerdict, CriticOutcome, TaskContextSpec, TaskSpec,
       ReviewOutput, InvestigationResult, Contradiction, MergedClaim,
       StructField, StructDefinition, FunctionSummary, ResynthesisResult).
       parse_response() tolerates fenced ```json blocks AND embedded JSON
       objects; input with no JSON object at all yields a pydantic
       ValidationError (via validating the raw text), not a bare
       JSONDecodeError. CriticVerdict.truth_level kept REQUIRED (no default)
       per the literal design spec. 16 tests green (round-trip for every model,
       schema validity, truth-level + missing-feedback validation, all
       parse_response paths). -->

- [x] **3.4** BNDB Writeback
  <!-- NOTES: BNDBWriter — rename_function/rename_variable (with _rename_map so
       SECOND+ renames of the same stable node id still resolve), set_type,
       save() (require_binja RuntimeError without Binja; issues bv.save() when
       present), async close() hook. 10 tests green (Binja absent: fake view
       flows; simulated Binja: real save path). -->

- [x] **3.5** Merge Agent
  <!-- NOTES: MergeAgent — diff-vs-baseline, no-conflict apply path, LLM
       resolve/reject (MergeDecision), follow-up TaskSpec tasks on reject,
       BNDB writeback on successful apply, no-LLM auto-accept path. 4 tests
       green with stateful doubles (applied/resolved/rejected/auto-accept). -->

## Phase 4: Type Recovery (Pass 0)

- [x] **4.1** Struct Access Pattern Detector
  <!-- NOTES: implemented — StructAccessDetector walks HLIL with a
       context-aware (read/write) walker, detecting *(base+N) deref-of-add,
       STRUCT_FIELD/DEREF_FIELD, and plain-deref patterns; within-function
       grouping by base variable; cross-function grouping ONLY on a meaningful
       Binja type (unknown/void/$unknown bases stay separate — false-positive
       grouping avoided). FieldAccess size from C type string mapping.
       4 tests green (fake_binja): multiple offsets [0,8,16,24], cross-function
       merge on 'struct cJSON*', unknown-type isolation, read/write flag,
       single-instruction address report. Caught + fixed an inverted
       const/non-const side selection in _deref_access during review. -->

- [x] **4.2** Type Recovery Agent
  <!-- NOTES: TypeRecoveryAgent(llm_client, context_asm, config) — assembles
       per-candidate context, requests the StructDefinition structured schema,
       returns None on empty fields (no struct). Records one claim per involved
       function at truth_level='inferred' with evidence = access instruction
       addresses (v1; critic may revisit in Pass 2). Uses context_asm.ledger.
       3 tests green (schema request, inferred claims, empty->None).
       AUDIT 2026-09-27: run(candidate, follow_up=False) selects
       pass0_type_recovery ("high" = load-bearing; feeds metric 5 + field
       renames); follow_up=True selects recovery_followup ("max" = xhigh) for
       refinement rounds. run.py Phase 4 drives a fixed-point loop capped by
       max_type_recovery_rounds; each candidate processed at most once.
       Regression test asserts the high/max routing incl. defaults. -->

- [x] **4.3** Graph Rebuild on Type Recovery
  <!-- NOTES: GraphRebuilder(extractor, bndb_writer, neo4j_driver) — builds a C
       struct string, defines the type in Binja (BNDBWriter.set_struct_type
       added, best-effort), MERGEs a :Struct node, re-runs the struct detector,
       creates explicit per-field :Variable nodes + :CONTAINS/:FIELD_OF edges,
       prunes obsolete @0x-offset placeholder Variables, re-runs
       validate_and_order. apply_struct(struct_def, function_addresses=None)
       falls back to extractor functions. 4 tests green (field graph,
       fallback, noop, unmatched-offset skip). -->

## Phase 5: Initial Sweep (Pass 1)

- [x] **5.1** Variable Rename Agent (Leaf Level)
  <!-- NOTES: RenameVariableAgent(llm_client, context_asm, tracer, config) —
       for_variable context (>>> target <<< highlight), Submission schema,
       minimal thinking budget, tracer event. 3 tests green. -->

- [x] **5.2** Bottom-Up Traversal Dispatcher (Pass 1)
  <!-- NOTES: Pass1Dispatcher(llm_client, neo4j_driver, context_asm,
       bndb_writer, ledger, tracer, config) — walks traversal_order ascending,
       group by level, parallel per-level (Semaphore); within a function
       SEQUENTIAL vars→args→summary; renames applied to Neo4j + BNDB
       immediately (no critic); SCC second pass re-runs args+summary only;
       bndb_writer.save(). Node queries guarded against .format() brace
       collision. 2 integration tests green (main sweep + SCC second pass). -->

## Phase 6: Deep Review (Pass 2) + Critic Loop

- [x] **6.1** Critic Agent
  <!-- NOTES: CriticEvaluator(llm_client, context_asm, ledger, tracer, config)
       — shared harness class; _rejection_counts[(func_addr, agent_role)];
       accepted→set_truth_level; rejected+below-max→delete_claim + feedback;
       rejected+at-max→force-accept 'speculation' + reset. One evaluator per
       dispatcher (never shared). 3 tests green. -->

- [x] **6.2** Review Agent
  <!-- NOTES: ReviewAgent — one structured ReviewOutput call (renames + claims
       with HLIL evidence + rich TaskSpecs) on for_function(include_callees +
       include_claims) context. 2 tests green. -->

- [x] **6.3** Pass 2 Dispatcher
  <!-- NOTES: Pass2Dispatcher — review→critic→task-critic→merge per function,
       level-parallel. Claims inserted to ledger (truth_level NULL) then
       critic; rejected claims deleted; tasks critically gated before todo;
       renames merged ONLY when the review passed (no claims OR an accepted
       claim), claims EXCLUDED from merge submission to avoid duplicate rows.
       3 tests green (accepted / all-rejected-drops-renames / no-claims). -->

## Phase 7: Investigation & Resynthesis

- [x] **7.1** Scheduler Agent
  <!-- NOTES: Scheduler(llm_client, todo, context_asm, tracer, config) —
       review_pending_tasks (20-task batches; merge/decompose SchedulerPlan
       schema), run_once (stale reset → assign ready → return WITHOUT
       executing), stale in_progress reset. Added TodoLedger.get_pending_tasks
       + reset_stale_in_progress + requeue_task. 3 scheduler tests + 2 todo
       tests green. -->

- [x] **7.2** Investigation Agent
  <!-- NOTES: InvestigationAgent — executes one task → InvestigationResult
       (answer + claims + rich TaskSpec subtasks). 2 tests green. -->

- [x] **7.3** Investigation Loop Runner
  <!-- NOTES: InvestigationLoop — own CriticEvaluator; claim→ledger→critic;
       subtasks created independently + parent requeued (deadlock-free
       deviation from the literal mutual-dependency spec — documented inline);
       complete only when no subtasks and (claims accepted OR no claims);
       stuck detection returns False. 3 tests green (drain / subtask-requeue /
       stuck). -->

- [x] **7.4** Resynthesis Agent
  <!-- NOTES: ResynthesisAgent — single LLM call on for_subgraph context →
       ResynthesisResult (contradictions + merged_claims + new_tasks).
       compute_resynthesis_groups already implemented in graph_analysis (2.5).
       2 tests green. -->

- [x] **7.5** Resynthesis Loop + Completion Check
  <!-- NOTES: ResynthesisLoop — capped at max_resynthesis_iterations; per group
       create tasks / merge claims; break on stable iteration; then
       register_functions_from_graph + is_re_complete (claims on ALL functions
       AND todo empty). 3 tests green + 1 is_re_complete gate test. -->

## Phase 8: Test Target + Integration

- [x] **8.1** Build Stripped cJSON Test Binary
  <!-- NOTES: eval/cjson/build.sh (cJSON v1.7.18, dynamic linking) + 
       extract_ground_truth.py (Binja headless; degrades to EMPTY ground_truth
       without Binja). VERIFIED locally: build succeeds, cjson_test has 0 text
       symbols (nm-clean), binary parses JSON at runtime; ground_truth.json
       stub committed; real extraction needs Binja on PYTHONPATH. -->

- [x] **8.2** End-to-End Pipeline Runner
  <!-- NOTES: run.py already written against the full architecture. Verified
       ALL previously-missing imports (pass1/pass2 dispatchers, scheduler,
       investigation_loop, completion, investigation/resynthesis agents) now
       resolve; `import reaper.run` is clean. -->

- [x] **8.3** Ground Truth Evaluator
  <!-- NOTES: eval/evaluate.py — 6 metrics (function exact/semantic name,
       variable exact/semantic, claim coverage, type-recovery by offset+size,
       false-confidence rate) + optional LLMJudge ({"equivalent": bool}
       structured schema, minimal thinking). Non-function keys (e.g. 'structs')
       excluded from function metrics. 6 tests green + CLI smoke on the built
       binary. -->

- [x] **8.3b (2026-09-27) Modular LLM-judge scoring engine** — `eval/scoring/`
  <!-- NOTES: rubric REGISTRY (function-name / variable-name / datatype —
       one-place change to add a dimension), JudgeEngine enforcing the
       "independent = fresh zero-context query per run" contract (session
       created immediately before each run, destroyed after; NEVER reused),
       resumable JSONL cache per (rubric, item, run), shared aggregation math
       (reporting.py), deterministic alignment (align.py, addr/ordinal for
       vars, name for structs). ScoreScheduler = the orchestrator the design
       asked for: it takes the evidence (recovered fn/var names + datatypes)
       and just scores it with the rubric N independent times — no BNDB
       parsing, no tool-calling. `llm_score.py` is now a thin compat shim
       (Scorer/_pairs/RUBRIC API preserved; tests unchanged). CLI:
       `python -m reaper.eval.scoring --ground-truth ... --reaper-output ...
       --n-runs 5`. -->

- [x] **8.3c (2026-09-27) Heavy per-mechanism dynamic testing** — `eval/heavy/`
  <!-- NOTES: per-mechanism HeavyCase framework. Each case builds a REAL
       harness (SQLite ledger/todo, fake graph/extractor) and drives the ACTUAL
       production class (CriticEvaluator, RenameVariableAgent, ReviewAgent,
       TypeRecoveryAgent, ResynthesisAgent, Scheduler, InvestigationLoop),
       seeding a KNOWN scenario, then (live mode) scoring the mechanism's
       output with the modular rubric engine over N independent zero-context
       runs. Offline mode = scripted StubLLM, deterministic CI gate
       (tests/test_heavy_cases.py — 8 cases, all green, no network). Runner:
       `python -m reaper.eval.heavy.runner [--case X] [--mode live|offline]`.
       Covers: critic loop, renaming, claim labelling/promotion, logic/
       conclusion (review), type recovery, resynthesis, scheduler,
       investigation. -->

---

## Discovered Issues

<!-- FILL: Document any design doc ambiguities, Binja API surprises,
     runtime errors, or implementation decisions made during development.
     Format: [deliverable] issue description + resolution -->

- [1.1] Design doc `.gitignore` rules are prefixed with `reaper/` (e.g.
  `reaper/data/`, `reaper/eval/cjson/...`) but the repo root IS the `reaper`
  directory, so those prefixes would never match. Resolution: rewrote
  `.gitignore` for the actual repo root (`data/`, `eval/cjson/...`, plus
  standard Python/packaging entries). Intent preserved.
- [1.1] Packaging: `pip install -e .` cannot expose the repo root as the
  `reaper` package without help. Resolution: `[tool.setuptools.package-dir]`
  `"reaper" = "."` + explicit `packages = [...]` list. Verified working with
  setuptools 80.10.2 / PEP 660 editable install in ~/reaper-venv.
- [1.1] The deliverable's Test references `reaper.harness.llm_client` and the
  entry point `reaper.run:main`, but 1.3/8.2 own those modules. Resolution:
  thin placeholder modules created so the 1.1 test is green now; both clearly
  marked to be replaced by their owning deliverables.
- [1.3] Local dev environment: no `python` alias (only `python3`), and
  aiosqlite/pytest-asyncio were not installed system-wide. Resolution: installed
  with `python3 -m pip install --break-system-packages aiosqlite pytest-asyncio`
  (PEP 668 externally-managed); the repo must be `pip install -e .` first — the
  root IS the `reaper` package via [tool.setuptools.package-dir], so
  `import reaper.harness.*` fails without the editable install.
- [1.3] Retry tests: `_BACKOFF_SECONDS` is a module global referenced at call
  time, so tests monkeypatch it to [0.0,0.0,0.0] — no 7s sleeps. Fake vLLM
  server is a real threaded HTTP server (not a mock) so the retry loop,
  transport, and structured_output response_format json_schema are exercised
  end-to-end (request paths are recorded to guard against base_url path
  doubling, finding #9).
- [1.5] FakeNeo4jDriver `_FakeAsyncResult` must mirror the REAL neo4j
  AsyncResult contract: it must support BOTH `await session.run(...)` (returning
  itself) AND `async for` iteration. The first version lacked `__await__`, so
  every Neo4j read in Ledger silently failed (TypeError caught by the method's
  try/except → all_functions_have_claims() wrongly returned False). Resolution:
  added `__await__` returning self; all coverage tests now pass.
- [3.3] parse_response() with text containing NO braces previously re-raised
  JSONDecodeError, contradicting the documented "raises ValidationError"
  contract. Resolution: on total garbage it now validates the raw text against
  the model so callers always get a single, catchable ValidationError type.
- [2.5] test_compute_resynthesis_groups_dedupes_overlapping_scopes never
  exercised the struct-consumer path: its fake responder matched the fragment
  "FIELD_OF->(s)", but the real Cypher is "[:FIELD_OF]->(s)" — the ']' before
  '->' is mandatory syntax, so the fragment never matched and 0x3000 was
  dropped. Resolution: fixed the TEST fragment to "FIELD_OF]->(s)" (code was
  correct; assertion unchanged — still requires all 4 addresses). Lesson: fake
  responders must mirror the EXACT Cypher emitted, brackets included.
- [2.5] test_traversal_order_assigned_to_every_function raised KeyError 'order':
  the pinned-function write hardcoded "SET f.traversal_order = 0" (no $order
  param), but the test reads q[1]["order"] from every query containing
  "f.traversal_order". Resolution: implementation now writes pinned order via
  the SAME $order parameter (uniform writes; behavior identical). Per the
  "fix the implementation, don't weaken the test" rule.
- [5.2] Cypher map literal `{address: $fa}` collided with Python
  str.format() in the Variable/Argument queries (KeyError 'address').
  Resolution: string concatenation for the node label instead of .format() —
  NEVER .format() a query containing `{...}` map syntax.
- [7.1] TodoLedger lacked a "list pending" query and a stale-task reset that
  the Scheduler (design § 7.1) needs. Resolution: added
  get_pending_tasks() / reset_stale_in_progress(timeout) — documented,
  tested; design intent preserved.
- [7.3] The literal "subtask depends on current task AND current depends on
  its subtasks" is a MUTUAL DEADLOCK for get_ready_tasks (both blocked forever).
  Resolution: subtasks are created independent, and the parent is requeued
  to pending so it is retried — no cycle, still terminates (documented in the
  module docstring; intentionally deviating from the literal spec).
- [8.1] extract_ground_truth.py needs Binja headless; without it the committed
  ground_truth.json is the empty `{}` stub (all 8.3 metrics report 0 by
  design). Real extraction requires PYTHONPATH=$HOME/binja-headless/python and
  re-running the extractor — build.sh + binary are committed/ignorable.
- [8.3] The evaluator's ground-truth/reaper-output dicts may carry a non-
  function `"structs"` key (metric 5). Resolution: every function metric loop
  guards on `"function_name" in gt_fn` so struct entries are never miscounted
  as functions (caught by test_claim_coverage_and_type_recovery...).
- [8.3] eval/evaluate.py `compute_metrics` originally lost its `return m`
  during a multi-part file edit (editor replaced rather than anchored);
  caught by tests. Lesson: when splitting large files, never use a 2-word
  anchor like `return m` for an append.
