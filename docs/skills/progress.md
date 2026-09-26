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


- [ ] **1.2** Neo4j Instance + Schema
  <!-- NOTES: docker-compose.yml, schema.cypher, init_db.py -->

- [ ] **1.3** vLLM Client Wrapper
  <!-- NOTES: ReaperLLMClient, LLMTransientError, session management, retry logic -->

- [ ] **1.4** Logging / Trace Infrastructure
  <!-- NOTES: Tracer, JSONL format, asyncio.Lock on file handle -->

- [ ] **1.5** Function Ledger Store
  <!-- NOTES: Ledger, 3 tables, async init pattern, write lock, foreign keys -->

- [ ] **1.6** TODO Ledger
  <!-- NOTES: TodoLedger, task dependencies, rejection flow (no 'rejected' status) -->

## Phase 2: HLIL Extraction + Graph Construction

- [ ] **2.1** HLIL Text Extractor
  <!-- NOTES: HLILExtractor, Binja headless, BNDB save, func.hlil.instructions -->

- [ ] **2.2** Graph Node Builder
  <!-- NOTES: build_nodes(), Function/Variable/Argument/Call/StringRef + CONTAINS edges -->

- [ ] **2.3** Graph Edge Builder (Dataflow)
  <!-- NOTES: build_edges(), instruction type → edge mapping, REFS_STRING linking -->

- [ ] **2.4** Symbol Preservation (Pass -1)
  <!-- NOTES: pin_symbols(), import/debug/library symbols, StringRef pinning -->

- [ ] **2.5** Leaf Validation + SCC Detection + Traversal Order
  <!-- NOTES: validate_and_order(), Tarjan's in Python (not GDS), condensed DAG topo sort -->

## Phase 3: Context Assembly + Harness Core

- [ ] **3.1** Context Assembler
  <!-- NOTES: ContextAssembler, 6 methods, truncation strategy for for_subgraph -->

- [ ] **3.2** Shadow Copy Manager
  <!-- NOTES: ShadowCopyManager, checkout/diff/apply, monotonic version counter -->

- [ ] **3.3** Submission Protocol
  <!-- NOTES: All Pydantic models, get_schema(), parse_response() -->

- [ ] **3.4** BNDB Writeback
  <!-- NOTES: BNDBWriter, _rename_map for tracking Binja's current var names -->

- [ ] **3.5** Merge Agent
  <!-- NOTES: MergeAgent, conflict resolution via LLM, BNDB writeback on apply -->

## Phase 4: Type Recovery (Pass 0)

- [ ] **4.1** Struct Access Pattern Detector
  <!-- NOTES: StructAccessDetector, StructCandidate/FieldAccess models, grouping strategy -->

- [ ] **4.2** Type Recovery Agent
  <!-- NOTES: TypeRecoveryAgent, StructDefinition output, claims at 'inferred' for v1 -->

- [ ] **4.3** Graph Rebuild on Type Recovery
  <!-- NOTES: GraphRebuilder, apply struct → delete old nodes → create new → revalidate.
       Caller handles ledger.register_functions_from_graph() after all structs applied -->

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
