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
       extra_body={"structured_outputs": schema, "disable_any_whitespace": True} —
       NEVER guided_json. Added async context manager (__aenter__/__aexit__) as a
       convenience beyond the design interface. Tests in tests/test_llm_client.py
       run against a REAL threaded fake-vLLM HTTP server (tests/conftest.py); all
       8 tests green (happy path, 503→retry→success, retry exhaustion raises
       LLMTransientError, structured_output transport verified, bad thinking_level
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
       3 tests green (schema request, inferred claims, empty->None). -->

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

- [ ] **5.1** Variable Rename Agent (Leaf Level)
  <!-- NOTES: RenameVariableAgent, minimal thinking, returns Submission with one Rename -->

- [ ] **5.2** Bottom-Up Traversal Dispatcher (Pass 1)
  <!-- NOTES: Pass1Dispatcher, vars→args→summary per function, SCC second pass,
       function_summary.txt prompt, NO critic in Pass 1 -->

## Phase 6: Deep Review (Pass 2) + Critic Loop

- [ ] **6.1** Critic Agent
  <!-- NOTES: CriticEvaluator shared class, _rejection_counts per (func_addr, agent_role),
       force-accept at 'speculation' after max rejections, delete rejected claim -->

- [ ] **6.2** Review Agent
  <!-- NOTES: ReviewOutput = renames + claims + TaskSpec list -->

- [ ] **6.3** Pass 2 Dispatcher
  <!-- NOTES: Pass2Dispatcher, review → critic → merge flow, shadow checkout for merge -->

## Phase 7: Investigation & Resynthesis

- [ ] **7.1** Scheduler Agent
  <!-- NOTES: Scheduler, review_pending_tasks + run_once, stale task check -->

- [ ] **7.2** Investigation Agent
  <!-- NOTES: InvestigationAgent, InvestigationResult = answer + claims + subtasks -->

- [ ] **7.3** Investigation Loop Runner
  <!-- NOTES: InvestigationLoop, creates internal CriticEvaluator, _handle_result critic flow,
       stuck detection when no tasks dispatched and none in progress -->

- [ ] **7.4** Resynthesis Agent
  <!-- NOTES: ResynthesisAgent, ResynthesisResult = contradictions + merged + new tasks -->

- [ ] **7.5** Resynthesis Loop + Completion Check
  <!-- NOTES: ResynthesisLoop, max_resynthesis_iterations cap, is_re_complete() -->

## Phase 8: Test Target + Integration

- [ ] **8.1** Build Stripped cJSON Test Binary
  <!-- NOTES: build.sh + extract_ground_truth.py, cJSON v1.7.18, dynamic linking -->

- [ ] **8.2** End-to-End Pipeline Runner
  <!-- NOTES: reaper/run.py, verify all imports resolve, verify all constructors match -->

- [ ] **8.3** Ground Truth Evaluator
  <!-- NOTES: 6 metrics, LLM judge for semantic match, ~70 LLM calls at minimal thinking -->

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
  transport, and structured_output extra_body are exercised end-to-end.
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
