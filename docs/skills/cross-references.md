# REAPER — Cross-Reference Verification

Check this BEFORE writing any constructor, import, or config access.
Source of truth: pipeline runner in `docs/design-re-stage.md` § 8.2.

---

## Constructor Signatures

Every constructor MUST match this table exactly (arg order matters).

| Class | File | Args (in order) |
|---|---|---|
| ReaperLLMClient | harness/llm_client.py | base_url, model, max_retries=3 |
| Tracer | harness/tracer.py | log_dir |
| Ledger | harness/ledger.py | db_path, neo4j_driver |
| TodoLedger | harness/todo.py | db_path |
| ContextAssembler | harness/context.py | extractor, neo4j_driver, ledger, config |
| ShadowCopyManager | harness/shadow.py | neo4j_driver, ledger, config |
| MergeAgent | harness/merge_agent.py | llm_client, shadow_mgr, bndb_writer, ledger, todo, config |
| Pass1Dispatcher | harness/pass1_dispatcher.py | llm_client, neo4j_driver, context_asm, bndb_writer, ledger, tracer, config |
| Pass2Dispatcher | harness/pass2_dispatcher.py | llm_client, neo4j_driver, context_asm, shadow_mgr, merge_agent, bndb_writer, ledger, todo, tracer, config |
| Scheduler | harness/scheduler.py | llm_client, todo, context_asm, tracer, config |
| InvestigationLoop | harness/investigation_loop.py | scheduler, inv_agent, llm_client, context_asm, todo, ledger, tracer, config |
| ResynthesisLoop | harness/completion.py | resynth_agent, inv_loop, todo, ledger, neo4j_driver, context_asm, tracer, config |
| HLILExtractor | tools/hlil_extract.py | binary_path, data_dir |
| BNDBWriter | tools/bndb_writer.py | extractor |
| StructAccessDetector | tools/struct_detector.py | extractor |
| GraphRebuilder | tools/graph_rebuild.py | extractor, bndb_writer, neo4j_driver |
| TypeRecoveryAgent | agents/type_recovery.py | llm_client, context_asm, config |
| RenameVariableAgent | agents/rename_variable.py | llm_client, context_asm, tracer, config |
| InvestigationAgent | agents/investigation_agent.py | llm_client, context_asm, ledger, todo, tracer, config |
| ResynthesisAgent | agents/resynthesis_agent.py | llm_client, context_asm, ledger, todo, tracer, config |
| CriticEvaluator | agents/critic_agent.py | llm_client, context_asm, ledger, tracer, config |

---

## Module-Level Functions (NOT classes)

| Function | File | Signature |
|---|---|---|
| build_nodes | tools/graph_nodes.py | `async def build_nodes(extractor, neo4j_driver) -> None` |
| build_edges | tools/graph_edges.py | `async def build_edges(extractor, neo4j_driver) -> None` |
| pin_symbols | tools/pin_symbols.py | `async def pin_symbols(extractor, neo4j_driver) -> None` |
| validate_and_order | tools/graph_analysis.py | `async def validate_and_order(neo4j_driver) -> None` |
| compute_resynthesis_groups | tools/graph_analysis.py | `async def compute_resynthesis_groups(neo4j_driver) -> list[list[str]]` |
| init_schema | infra/init_db.py | `async def init_schema(neo4j_driver)` |
| is_re_complete | harness/completion.py | `async def is_re_complete(ledger, todo) -> bool` |
| get_schema | harness/submission.py | `def get_schema(model_class) -> dict` |
| parse_response | harness/submission.py | `def parse_response(model_class, text) -> BaseModel` |

---

## Pipeline Runner Imports (§ 8.2)

Verify each import resolves after implementing its deliverable.

<!-- FILL: Check off as you implement each deliverable -->

- [ ] `reaper.harness.llm_client` → ReaperLLMClient (1.3)
- [ ] `reaper.harness.tracer` → Tracer (1.4)
- [ ] `reaper.harness.ledger` → Ledger (1.5)
- [ ] `reaper.harness.todo` → TodoLedger (1.6)
- [ ] `reaper.harness.context` → ContextAssembler (3.1)
- [ ] `reaper.harness.shadow` → ShadowCopyManager (3.2)
- [ ] `reaper.harness.submission` → get_schema, parse_response, all models (3.3)
- [ ] `reaper.harness.merge_agent` → MergeAgent (3.5)
- [ ] `reaper.harness.pass1_dispatcher` → Pass1Dispatcher (5.2)
- [ ] `reaper.harness.pass2_dispatcher` → Pass2Dispatcher (6.3)
- [ ] `reaper.harness.scheduler` → Scheduler (7.1)
- [ ] `reaper.harness.investigation_loop` → InvestigationLoop (7.3)
- [ ] `reaper.harness.completion` → ResynthesisLoop, is_re_complete (7.5)
- [ ] `reaper.tools.hlil_extract` → HLILExtractor (2.1)
- [ ] `reaper.tools.graph_nodes` → build_nodes (2.2)
- [ ] `reaper.tools.graph_edges` → build_edges (2.3)
- [ ] `reaper.tools.pin_symbols` → pin_symbols (2.4)
- [ ] `reaper.tools.graph_analysis` → validate_and_order, compute_resynthesis_groups (2.5)
- [ ] `reaper.tools.bndb_writer` → BNDBWriter (3.4)
- [ ] `reaper.tools.struct_detector` → StructAccessDetector (4.1)
- [ ] `reaper.tools.graph_rebuild` → GraphRebuilder (4.3)
- [ ] `reaper.agents.type_recovery` → TypeRecoveryAgent (4.2)
- [ ] `reaper.agents.investigation_agent` → InvestigationAgent (7.2)
- [ ] `reaper.agents.resynthesis_agent` → ResynthesisAgent (7.4)
- [ ] `reaper.infra.init_db` → init_schema (1.2)

---

## Internal Imports (used inside dispatchers, NOT in pipeline runner)

| Consumer | Imports |
|---|---|
| Pass1Dispatcher (5.2) | `from reaper.agents.rename_variable import RenameVariableAgent` |
| Pass2Dispatcher (6.3) | `from reaper.agents.review_agent import ReviewAgent` |
| Pass2Dispatcher (6.3) | `from reaper.agents.critic_agent import CriticEvaluator` |
| InvestigationLoop (7.3) | `from reaper.agents.critic_agent import CriticEvaluator` |
| ResynthesisLoop (7.5) | `from reaper.tools.graph_analysis import compute_resynthesis_groups` |

---

## Pydantic Models (all in harness/submission.py)

<!-- FILL: Check off after implementing 3.3 — verify all importable -->

- [ ] TRUTH_LEVELS (Literal type)
- [ ] Rename, EvidenceLink, Claim, Submission
- [ ] CriticVerdict, CriticOutcome
- [ ] TaskContextSpec, TaskSpec
- [ ] ReviewOutput, InvestigationResult, ResynthesisResult
- [ ] Contradiction, MergedClaim
- [ ] StructField, StructDefinition
- [ ] FunctionSummary
- [ ] LLMTransientError (in harness/llm_client.py, NOT submission.py)

Also in struct_detector.py (NOT submission.py):
- [ ] StructCandidate, FieldAccess

---

## Config Keys (configs/default.toml)

<!-- FILL: After implementing 1.1, verify all these paths parse from the TOML -->

- [x] `config['neo4j']['uri']`, `['user']`, `['password']`
- [x] `config['vllm']['base_url']`, `['model']`
- [x] `config['thinking_levels']` — keys: pass0_type_recovery, pass1_rename, pass2_review, critic, investigation, merge, resynthesis, scheduler
- [x] `config['limits']` — keys: max_critic_rejections, max_resynthesis_iterations, shadow_copy_hops, task_timeout_seconds, max_concurrent_agents, max_context_tokens
- [x] `config['paths']['data_dir']`, `['traces_dir']`

All verified via tomllib against `configs/default.toml` in Deliverable 1.1.

---

## Prompt Files

<!-- FILL: Check off as you write each prompt -->

- [ ] `agents/prompts/rename_variable.txt` (5.1)
- [ ] `agents/prompts/function_summary.txt` (5.2)
- [ ] `agents/prompts/type_recovery.txt` (4.2)
- [ ] `agents/prompts/critic_agent.txt` (6.1)
- [ ] `agents/prompts/review_agent.txt` (6.2)
- [ ] `agents/prompts/merge_agent.txt` (3.5)
- [ ] `agents/prompts/investigation_agent.txt` (7.2)
- [ ] `agents/prompts/resynthesis_agent.txt` (7.4)
