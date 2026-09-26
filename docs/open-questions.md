---
status: active
updated: 2026-09-25
---

# REAPER — Open Questions

## Graph Structure
1. **RESOLVED**: Missing edges (indirect calls, vtables, callbacks) — noted as ambiguity on the node, agents proceed and flag the gap. Dynamic analysis in VR stage can later resolve these and add edges.
2. **RESOLVED**: Graph reorganization happens when type recovery changes struct layout — e.g., one write was actually into 2 fields because Binja misidentified the struct. This changes the leaf count (struct fields are leaves) and affects every variable of that type. Reorganization is the exception, not the rule — it only fires when struct layout changes, which cascades through all variables of that type. Everything else is just annotation.
3. **RESOLVED**: Cycles — variables are always leaves (grounded from local SSA context). Detect SCCs via Tarjan's, process variables first, defer call-level back-edges, revisit once both sides have labels. One extra pass per SCC, not unbounded fixpoint.
4. **RESOLVED**: Evidence spot = binary address range, presented as HLIL at that range. Address ranges allow context to be windowed at any granularity. HLIL is the standardized representation for display/agent consumption.
4a. **DEFERRED (VR design)**: How do the HLIL graph and CPG graph coexist in Neo4j?

## Pass 1 / Rename Propagation
5. **RESOLVED**: No confidence gate — Pass 1 walks bottom-up: variables (always leaves, validated) get renamed from local context only, then each level up receives immediate children's renames as evidence and renames with that additional context. Progressive context accumulation, not blind propagation. Bad renames at leaves are naturally correctable by the next level up which has more context. The HLIL graph is primarily for scheduling traversal order in the initial stages.
6. **DEFERRED (future)**: Same parameter, different semantic meaning at different call sites. Current model is one binary at a time — cross-binary interactions are a future TODO. Within a single binary this is still possible but less common; handle as an ambiguity flag on the node when encountered.

## Critic / Truth Values
7. **RESOLVED**: Truth value scale is discrete: **speculation / inferred / low confidence / mid confidence / high confidence**. All levels are valid claims — speculation is kept, not discarded. The taxonomy represents hypothesis quality, not a pass/fail gate.
8. **RESOLVED**: Critic is a feedback loop (submit → critique → revise/resubmit). Same model for all agents including critic — one model (best available, currently DeepSeek-V4-Flash 0731 Q4), differentiated by prompt/role not model choice. Multiple critic instances can run in parallel since they share the same model and prompts — calibration drift is a prompt engineering concern, not a model selection one.
9. **RESOLVED**: Same model for critic and submitting agents. Differentiation is through role/prompt, not capability tier. One model assumption simplifies everything.
10. **RESOLVED (runtime tuning)**: Critic calibration is a runtime tuning concern. Goal is best-effort output without wild guesses. All hypothesis types (speculation through high confidence) are valuable — the critic's job is accurate classification, not gatekeeping.

## Agent State & Concurrency
20. **RESOLVED**: Shadow copy at checkout time. Agent works on the snapshot. On merge submission, a merge agent handles conflicts. If a conflict is irreconcilable, the merge agent declines both sides and writes new tasks to the TODO ledger to reopen the work.
21. **DEFERRED (impl)**: Merge conflict resolution — hope is that task independence minimizes conflicts; when they happen, an LLM-based resolver in the harness handles it. Design at build time.

## Context & Scale
11. **RESOLVED (impl deferred)**: Context management — custom compaction/context management layer, not relying on default compaction. Modular so we can test different compaction strategies. A dedicated agent with a tuned skill builds context per-task. Implementation details at build time.
12. **RESOLVED**: First test target is cJSON (~30 functions, structs, custom allocator, string parsing). Compile cJSON.c + test.c into one binary, strip it — source is ground truth for evaluating agent output. Build: `gcc -O2 -o cjson_test cJSON.c test.c -lm && strip cjson_test`. Need a stripped copy in eval/.

## Investigation & Resynthesis
13. **RESOLVED**: Task independence is the task writer's responsibility — each investigation must be atomic with no interdependency. A critic reviews task creation, and a scheduler agent periodically reviews the TODO ledger to merge or decompose tasks that have hidden coupling.
14. **RESOLVED**: RE is done when every variable name and function name has been investigated and carries a claim with a confidence level. That's the handoff point to VR — VR treats it as an initial guess and refines from there with bug hunting and dynamic analysis. No need for perfect convergence; RE produces a best-effort labeled CPG draft.

## Not Yet Addressed
15. **RESOLVED**: RE→VR is a hard gate. RE must complete before VR begins. VR refines RE findings but does not consume them incrementally. RE's output is an initial draft CPG (equivalent to what OSS gets from a CPG tool, but reconstructed from binary analysis).
16. **DEFERRED (VR design)**: How do we evaluate against PrimeVul/Juliet? They evaluate VR, not RE.
17. **REQUIREMENT (design-time)**: Function ledger is a separate store from Neo4j but MUST have first-class edges into the graph layer. An agent at any binary location sees graph edges to/from that spot as a native operation, not a lookup. Bridging mechanism (shared keys, cross-layer edges, etc.) to be designed with the planning agent.
18. **DEFERRED (VR design)**: Which OSS CPG tool feeds the VR-stage graph layer?
19. **RESOLVED**: No edge taxonomy for the HLIL initial layer
22. **RESOLVED**: LLM names are pothole_case (lowercase + underscore), as verbose as needed. Canonical names use normal variable naming conventions for humans. Agents provide both on every rename. — it's just structural (dataflow edges for traversal ordering). Edge labels matter starting in Pass 2 when building the relationship and evidence graph (conditionals, etc.). That taxonomy is TBD and may evolve.
