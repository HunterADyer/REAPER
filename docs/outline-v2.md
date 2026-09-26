---
status: draft
version: 3
updated: 2026-09-25
---

# REAPER — RE Stage Outline

## 0. Constraints & Assumptions
- All inference is local (unlimited tokens, no API cost concern)
- Pipeline runs continuously in the background as needed
- Cost concern is wall-clock time and GPU scheduling, not per-token billing
- **One model** — best available locally (currently DeepSeek-V4-Flash 0731 Q4). All agents (workers, critics, merge) use the same model, differentiated by prompt/role only
- **Thinking level is the quality/speed knob** — Pass 1 runs minimal thinking for fast coverage, Pass 2 and critic run max thinking for depth. Configurable per-stage and per-task
- **True concurrency via vLLM** — multiple agent sessions run simultaneously against the vLLM server. Context and conversation history managed CPU-side by the harness. Agents are real parallel workers, not logical turns on a serial queue
- **One binary at a time** — cross-binary interactions are a future TODO
- **Log everything** — all agent interactions, submissions, critic feedback, merges, renames are logged as traces. Enables reinforcement learning, dashboards, and post-hoc debugging. Observability details built as we go

## 1. Foundation: HLIL Dataflow Graph
- Built automatically from Binja headless API against each target binary
- **Stored in Neo4j** — all graph state lives here, queryable via Cypher
- Edges are labeled (dataflow-assign, dataflow-arg, call, return, etc.) so agents and tooling can query by edge type
- Directed graph where:
  - Nodes = variables, function arguments, function calls
  - Edges = dataflow relationships (assignment, passed-as-argument, return-to-variable)
- Cross-function propagation: renaming a variable propagates both up (callers) and down (callees) through the graph
- This graph is the shared backbone for both RE and VR stages
- The graph defines traversal order: agents start at leaf nodes, work upward
- **Missing edges (unresolved indirects, function pointers, vtables) are noted as ambiguity on the node, not treated as errors** — agents proceed with what's known and flag the gap
- **Cycle breaking**: cycles only appear inter-procedurally through call edges (HLIL within a function is acyclic/SSA-based)
  - Detect SCCs in the call graph (Tarjan's)
  - Variables are always leaves — grounded from local context (assignments, constants, types) regardless of call graph shape
  - Traversal order: variables (leaves) → arguments → function calls → inter-procedural edges
  - Within an SCC: process all functions' variables and local dataflow first, then arguments and calls using grounded variable context
  - Back-edges (the call that closes the cycle) are deferred, then revisited once both sides have initial labels — one extra pass, not unbounded fixpoint

## 1a. CPG Output & OSS Parallel
- **RE produces an initial draft CPG as its output** — reconstructed from binary analysis (HLIL dataflow graph + function ledger + resolved evidence)
- This is the binary-analysis equivalent of what OSS targets get from a CPG tool (Joern, etc.) applied to source code
- For OSS targets: a CPG tool generates this representation directly (deferred to VR design)
- For binary targets: RE stage reconstructs it
- Both paths produce the same graph structure in Neo4j — VR stage consumes a uniform representation regardless of source
- **RE→VR is a hard gate**: RE must complete before VR begins
- **VR refines RE findings** but does not consume them incrementally — it receives the completed draft CPG and builds on it
- Dynamic analysis in VR can add new edges (confirmed call targets, resolved indirects, runtime relationships)

## 2. Function Ledger (per-function notepad)
- Every function gets a structured notepad maintained by the ledger
- **Separate store from Neo4j** (not graph node properties), but with first-class edges into the graph layer
- An agent looking at any location in the binary can immediately see the graph edges to/from that spot — the translation between binary location and graph context is a first-class operation, not a lookup
- Agents write entries when they touch a function, covering:
  - What the function does (purpose/summary)
  - Weird/edge behaviors observed
  - Potential bugs found
  - Cross-binary interactions (calls, shared state, globals)
- Entries are decomposed into claims, each carrying a truth value
- Claims link to **evidence spots** — binary address ranges, presented as HLIL at that range. Address ranges allow context windowing at any granularity
- Links are bidirectional: claims reference graph nodes via address ranges, graph nodes are annotated with claims

## 3. Truth Values & Adversarial Critic
- Every claim has a truth level: **speculation → inferred → low confidence → mid confidence → high confidence**
- All levels are valid — speculation is kept, not discarded. The taxonomy represents hypothesis quality, not a pass/fail gate
- The submitting agent does NOT set the truth level
- **Submission loop**: when an agent submits changes (claims to the ledger, variable/function renames), the submission is per-claim:
  - Agent packages the claim + evidence (graph node references)
  - Critic receives the submission and traverses the evidence nodes on the graph to evaluate
  - Critic returns constructive feedback and questions to the agent
  - Agent can either resubmit a modified package or return to "working" state to build a better submission
  - On rejection: task is requeued with the critic's feedback baked into the job description, ensuring evidentiary gaps are addressed on the next attempt
- Critic evaluates: strength of evidence, consistency with other claims, whether evidence supports the conclusion

## 3a. Agent Working State & Harness-Managed Merge
- Each agent receives a **shadow copy** of the relevant subgraph at checkout time
- Agent works entirely on its snapshot — can query, mutate, iterate freely without affecting master or other agents
- Working state is NOT applied to the master graph/ledger until critic-approved
- On submission, a **merge agent** handles integration into master:
  - Clean merge: apply changes atomically
  - Resolvable conflict: merge agent resolves and applies
  - Irreconcilable conflict: merge agent **declines both sides** and writes new tasks to the TODO ledger to reopen the work — neither side's changes are applied
- The harness owns all writes to the master graph, ledger, and **BNDB** — agents propose, harness applies
- BNDB is read-write: agent renames (both LLM and canonical names) are written back by the harness
- The harness maintains the authoritative source of truth across Neo4j, ledger, and BNDB

## 3b. Pass -1: Symbol Preservation
- Before any LLM pass, preserve all existing symbols from the binary (import tables, debug symbols if present, Binja signature matching)
- These are free ground-truth labels — do not rename or re-evaluate them
- Mark these nodes in the graph as **pinned** — agents can reference them but cannot modify them

## 3c. Naming Convention: Dual-Layer Names
- **LLM names**: verbose, descriptive, pothole_case (lowercase + underscore), can be as detailed as needed. Optimized for agent context propagation (e.g., `json_parse_error_string_buffer`, `config_timeout_seconds_from_global`)
- **Canonical names**: normal variable naming conventions for human consumption and BNDB export (e.g., `parseErrBuf`, `cfgTimeout`)
- Agents provide BOTH names on every rename — the harness writes both back to the BNDB
- The harness is the authoritative source for all names — agents propose, harness writes

## 3d. Pass 0: Type Recovery (before renaming, and ongoing)
- **Type recovery runs first** before any renaming — initial pass builds struct layouts so the HLIL graph has proper field nodes instead of raw offsets
- **Type recovery also runs during evaluation** against the graph — as investigation agents discover struct layout corrections, the graph is rebuilt
- Goal: model struct fields (e.g., `g_config->timeout`) so they appear as proper nodes in the HLIL graph, connecting producers and consumers across functions
- Without type recovery, struct field accesses appear as raw offsets — the graph can't connect function A writing `*(ptr+0x10)` to function B reading the same field
- Higher thinking effort than Pass 1 renaming — types are harder to infer than names
- **Graph reorganization triggers only on struct layout changes** — e.g., discovering one write was actually into 2 fields changes the leaf count and cascades to every variable of that type. This is the exception, not the rule. All other findings are annotation only.
- Built entirely on top of **headless Binary Ninja**

## 4. Pass 1: Initial Sweep (fast, minimal effort)
- **The HLIL graph is primarily for scheduling** — it determines traversal order for the initial stages, not used heavily after that
- **Variables are always the leaves** — the graph must be validated to ensure no functions appear as leaves (explicit check)
- Pass 1 operates bottom-up on the graph:
  - At leaf nodes (variables): LLM infers a name for that variable only, based on local context
  - Walking upward: each node receives the renames from its immediate children as an evidence layer, then renames based on that additional context
  - This continues up the graph — each level has progressively more context from resolved children
- Minimal thinking level for speed
- Goal: coverage over accuracy — first-pass labeling of the entire binary with progressive context accumulation

## 5. Pass 2: Deep Review (max thinking, leaf-to-root traversal)
- Subagents follow the HLIL dataflow graph from leaves upward
- At each function along the traversal:
  - Evaluate Pass 1 labels against HLIL and accumulated callee context
  - Flag anything believed wrong or low-confidence
  - Write detailed findings to the function ledger as claims (submitted to critic)
- Output: **TODO tasks** for the investigation stage (review agents write tasks, not fixes)
- Once an investigation agent completes a task, it notes any outstanding items it still needs:
  - New dependencies get added to the TODO queue, and the parent task goes back to the scheduler with those as dependencies
  - Or the task is successful and closed out
- Leaf-to-root order means reviewers have already-reviewed callee context when evaluating callers

## 6. Investigation Stage: Evidence Refinement & Truth Resolution
- Review agents from Pass 2 decompose unknowns into detailed investigation tasks on the global TODO ledger
- Each task demands a concrete answer (not "look into this" but "determine whether X can be NULL when called from Y")
- Each task specifies:
  - Detailed description of the investigation
  - References to specific nodes/edges in the dataflow graph
  - Initial context the executing agent needs (dictated by the review agent)
  - Starting position in the graph/binary
  - Goal — what a concrete answer looks like
- Tasks must be independent and atomic — this is the task writer's responsibility
- A **critic reviews task creation** to catch hidden dependencies
- A **scheduler agent** periodically reviews the TODO ledger to merge tasks that overlap or decompose tasks that are too large
- Tasks form a dependency hierarchy (DAG): children completed + critic-reviewed before parent unlocks
- **Context management**: a custom compaction/context management layer (not default LLM compaction) builds each agent's context. A dedicated agent with a tuned skill assembles relevant context per-task from the graph and ledger. Modular — can swap/test compaction strategies. Implementation details at build time.
- As tasks complete, the evidence graph and function ledger are refined: claims strengthened, weakened, or resolved; truth values updated by critic

## 7. Resynthesis: Critical Review of Accumulated Evidence
- After investigation tasks resolve, a resynthesis pass reviews the updated evidence landscape
- Purpose:
  - Detect contradictions between resolved claims
  - Identify emergent understanding no single investigation captured
  - Propagate resolved truths back through the dataflow graph
  - Collapse or merge redundant claims
  - Flag new unknowns (which feed back as new TODO tasks → step 6)
- Iterative: resynthesis can trigger new investigations until the ledger stabilizes
- **RE stage complete when**: every variable name and function name has been investigated and carries a claim with a confidence level. This is the handoff point — VR receives the labeled CPG draft as an initial guess and refines from there
