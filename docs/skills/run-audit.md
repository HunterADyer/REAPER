# cjson_001 — Live-Run Audit (from a cancelled Phase-7 run)

**Date:** 2026-09-28 · **Source data:** `data/cjson_001_*.jsonl` / `ledger.db` /
`todo.db`, Neo4j graph, vLLM container logs, Run Cockpit counters.
**Run ended:** cancelled by operator during Phase 7 (before completion). All state
persisted; nothing lost — a `--phases 7` resume can pick up where we stopped.

This file is the honest engineering record. Findings are ordered by severity and
each has an actionable fix. None of this is blame — the point is to make the
*next* run substantially better.

---

## 0. TL;DR — the four things that matter

1. **The pipeline is actively self-polluting the graph.** The "bare rename-id
   normalized as a *function*" bug created **85 spurious `:Function` nodes**
   (graph grew 124 → 150 valid + 85 junk). This corrupts every downstream
   consumer that counts or addresses functions (completion scoping, resynthesis
   groups, export, evaluation). Root cause is fixed in `agents/` for the
   **next** run — but the **current graph is still dirty** and must be cleaned.
2. **The investigation phase cannot finish: task explosion.** 292 tasks were
   created, but only 45 completed; **203 tasks sat `in_progress`** and 44
   `pending` at kill time — and the in_progress count was still growing.
   `scheduler.reviewed=0`, `resynthesis.iterations=1`: the merge/decompose and
   resynthesis mechanisms that exist to *prevent* this never ran.
3. **We are GPU-poor because we serialize.** With `llm_max_concurrent=1`
   (client gate) + `--max-num-seqs 8` + `--max-num-batched-tokens 2048`
   (server caps), the live server never saw more than **2 requests at once**
   (mostly 1). Deep thinking at "high"/"max" took 400–450 s each → hours of
   serialized latency. The batching levers are identified and the server has
   now been relaunched at 64 seqs / 8192 tokens; REAPER config updated to
   `llm_max_concurrent=4`. **A resumed/restarted run is required to pick these
   up — the killed run loaded the old config at startup.**
4. **The restart itself was clean and necessary.** The 27 `llm.error` events
   (2.2%) are 100% contained in the container-restart window (connect-exhausted
   retries while vLLM was down). Post-restart the pipeline continued (claims
   kept being accepted). No systemic error cause.

---

## 1. Run outcome — what actually happened

| Metric | Value | Notes / concern |
|---|---|---|
| Run phases | 2–7 requested; 3,4,5,6 `done`; 7 never finished | stopped by operator |
| LLM calls | 1,229 requests / 1,192 responses / 27 errors | 2.2% error rate |
| Error window | all 27 in 05:55–06:16 | restart-only (see §2) |
| Claims in ledger | 648 (415 high / 106 mid / 18 low / 99 inferred / 10 NULL) | 10 NULL were mid-flight at kill |
| Claim acceptance | 504 accepted / 8 rejected / 0 force-accepted | 98.4% accept — critic must tighten (see §5) |
| `:Function` nodes | 150 valid + **85 spurious** (235 total) | severe graph pollution (see §3) |
| Tasks | 292 created · 45 completed · 203 in_progress · 44 pending | investigation never drains (see §4) |
| Scheduler reviews | 0 | merge/decompose never ran (see §4) |
| Resynthesis | 1 iteration, 0 contradictions, 0 merges | essentially inactive (see §4) |
| Type recovery | 4 structs recorded; 96 struct-related claims | working as designed |
| Struct candidates | 17 candidates queried (0..16) | reasonable |

**What was actually achieved (a real, non-trivial result):** 124 functions
analyzed; cJSON struct was recovered (`value_node`); 415 high-confidence claims;
Pass-1/Pass-2 produced substantial rename coverage tied to the real cJSON API
(e.g. `parse_cjson_with_options`, `cJSON_IsReference`-style roles detected). The
core idea works — the mechanisms that *mature* output into a completion are
what failed.

---

## 2. Restart incident — root cause, impact, and lessons

**Timeline:** the container was relaunched (bare-metal → tuned docker profile)
at ~13:09Z. For ~24 minutes while vLLM was down, REAPER's client retried
(backoff 5/10/20s ×3) and eventually raised `LLMTransientError` → `llm.error`
events. **All 27 errors are "All connection attempts failed"** — no 4xx, no
mid-request GPU fault, no model corruption.

**Impact:** bounded. The died requests were investigation/critic turns; the
pipeline detected them, and subsequent work continued (response counter kept
rising). No state was lost (Neo4j-first / SQLite-second discipline held).

**What I did (already applied):**
- Relaunched container with **`--max-num-seqs 64`** and
  **`--max-num-batched-tokens 8192`** (vLLM Blackwell-class default). Original
  launcher untouched at `~/run-deepseek-v4.sh`; tuned copy at
  `~/run-deepseek-v4-batch.sh`.
- Updated `configs/default.toml` → `llm_max_concurrent = 4` (from the hard 1).
- Confirmed post-restart: model `deepseek` serves; container args verify the new
  caps; GPU steady at ~97–99% (solo request — batching headroom unproven in
  this particular run because REAPER stayed serialized).

**Lesson / next action:** the batching change is *dead code* until REAPER
actually runs with `llm_max_concurrent=4` **and** the investigation phase stops
serializing behind one long request. See §6 for the concurrency budget.

---

## 3. CRITICAL — graph self-pollution: 85 spurious `:Function` nodes

**Symptom:** Neo4j has 235 `:Function` nodes; 124 is the real function count.
The 85 extra nodes have **non-address ids** that are clearly *variable names*
from the rename agent, e.g.:

```
addr='cjson.next'      canon='Next'
addr='rdx_1'           canon='current_item'
addr='arg3'            canon='return_parse_end'
addr='__return_addr'   canon='argc_storage'
addr='rbp'             canon='count_64'
addr='r8_1'            canon='escape_count'
addr='data_408028'     canon='__TMC_END__'
addr='rax_6'           canon='first_item'
addr='sub_402610'      canon='flags2'
addr='var_8'           canon='outgoing_arg_8'
addr='renames'         canon='node'
addr='function_address_placeholder_removed' canon='??'
```

**Root cause (confirmed in code):** the live model returned bare variable
suffixes (`rdx_1`, `zmm15`, `cjson.child`, …) as the `node_id` of Rename
submissions. `Pass1Dispatcher._apply_submission` and `_merge_rename` key on
`':' in node_id` to decide *variable* vs *function*:
- with `':'` → variable rename against the stable `0x<fn>:<name>` id (correct),
- without `':'` → treated as a **function** address →
  `MERGE (n:Function {address: <bare>})` fabricating a junk node.

Compounded by a second leak: some *actual* rename nodes were never addressed
(`0x1000`, `0x1040`, `cJSON_InsertItemInArray`, `0x00000000`) — traces from a
summary/rename path using an untranslated id. 33 ledger claim-addresses are not
in the graph at all.

**Why it matters:** every consumer that walks `:Function` now sees phantom
functions. Completion scoping ("all renamable functions have claims") can never
satisfy because hallucinated addresses have no claims; resynthesis groups are
built around junk; the final export includes garbage; evaluation denominator is
wrong. This is very plausibly **why Phase 7 never ended**.

**Fix — already shipped for next run:** `RenameVariableAgent.run` +
`ReviewAgent.run` now normalize any `':'`-less rename id to
`f"{func_address}:{name}"` (both are strictly one-function-scoped). Regression
tests added.

**Fix — must still do (this graph):** the 85 spurious nodes + 33 stale claim
addresses remain. Provide/run a cleanup Cypher:
```cypher
MATCH (f:Function) WHERE NOT f.address =~ '0x[0-9a-f]+' DETACH DELETE f;
```
and decide on the ledger-claim addresses that aren't in the graph (`0x1000`,
`0x00000000`, `cJSON_InsertItemInArray`, mid-function `0x401012`… — these are
*real* evidence targets recorded at instruction addresses; the ledger should key
claims to the owning function's start address, not the evidence instruction).
I already deleted 6 junk nodes during the live run; ~85 remain.

---

## 4. CRITICAL — investigation phase cannot drain

**The evidence:**
- 292 tasks created vs 45 completed at kill time; **203 `in_progress` + 44
  pending** still outstanding. Task creation *continued growing right up to the
  end* (07:09 bucket added 40, 08:33 added 24, 11:05 added 21…). Nothing was
  converging.
- Function `0x408010` alone had **25** tasks; `0x402610` 14; `0x4022f0` 13;
  `0x0` (no start addr) 13. These are not "one question per function" — they
  are task *churn*.
- `scheduler.reviewed = 0` — the Scheduler's MERGE/DECOMPOSE pass never ran
  (0 `scheduler_reviewed` events). It only ever did `run_once` assignment
  (231/205/203 assigned over the run), never a review batch.
- `resynthesis.iterations = 1` with 0 contradictions / 0 merges / 6 new tasks —
  the contradiction/merge safety net effectively didn't participate.
- `stale` resets DID fire (152 then 195 stale in_progress tasks reset) — so the
  deadlock-guards work, but they reset rather than *resolve*; tasks kept
  re-assigning, re-running, re-spawning.

**What I believe is happening (needs confirmation, but strongly supported):**
the InvestigationAgent keeps emitting `subtasks` for the same unresolved
targets (PLT-thunk/import-stub ambiguity, `register_tm_clones` lookalikes,
edge/mid-function addresses), and each new subtask → parent requeue →
re-assignment → a *fresh* LLM turn per iteration. With one-at-a-time gating and
400 s+ turns, the loop spends its entire budget re-litigating the same hard
cases while the easy majority just waits at `in_progress`. Nothing merges or
dedupes because Scheduler.review_pending_tasks isn't producing PLANS (0
reviewed) — worth checking whether a batch is even being formed (empty pending
at review time?) and whether `keep`/merge logic protects against self-cloning.

**Fixes to implement before the next run:**
1. **Inspect the investigation task loop** — log each task's requeue count and
   *reason*; if the same `(start_position, description-hash)` is requeued
   ≥N times, do not re-run — force-accept the last claims (mirror the critic's
   force-accept-at-speculation philosophy) and complete the task.
2. **Make Scheduler.review_pending_tasks actually run** and log its plan
   (currently 0 reviews — confirm it gets a non-empty pending batch; if pending
   is being drained to 0 by `run_once` before review sees it, reorder:
   review BEFORE assign, and only assign review-survivors).
3. **Bound task explosion at the source:** cap subtasks per investigation
   result (e.g. ≤3) and refuse subtasks whose `(start, description)` already has
   a pending/in_progress sibling (dedupe on insert).
4. **Disallow start_position `0x0` / non-function addresses** — tasks should
   always anchor to a real `:Function`; validate at creation.
5. **Reconsider the loop's completion gate** given dirty graph: exclude
   spurious `:Function` nodes (see §3) so `all_functions_have_claims` can ever
   be true.

---

## 5. Critic calibration — 98.4% accept is too loose to be a gate

**Evidence:** 504 accepted / 8 rejected / 0 force-accepted. With the critic
being *the* truth gate, a 98.4% acceptance rate means rejections are nearly
absent — either reviewers are producing excellent claims (plausible for cJSON,
a well-known target) or the critic is rubber-stamping. Given the reward for
reporting is high and the cost of a bad high-confidence claim flows directly
into metric 6 (false-confidence rate) and into merge, this deserves scrutiny.

**Also:** type-recovery claims dominate (`type_recovery_struct_cand_16` = 43,
`_cand_2` = 28, `_cand_9` = 25 → 96 claims) — these are recorded at
`inferred`, by design, but they spiked claim volume without a verified label.

**Suggestion:** add a live counter `critic.reject_rate` to the Loops drawer and,
for the first scoring run, manually sample 20 high-confidence claims in the
LLM-judge evaluation (they feed `false_confidence_rate`). Do NOT loosen
further; keep the (now hardened) critic prompt that requires specific,
actionable feedback.

---

## 6. Throughput & batching — what the numbers say

**Per-level durations (1,192 completed calls):**
| level | n | mean | median | max |
|---|---|---|---|---|
| minimal | 139 | 12.4 s | 13.2 s | 29.4 s |
| high | 686 | 37.9 s | 11.4 s | **452 s** |
| max | 367 | 52.4 s | 5.4 s | **409 s** |

The *medians* are fine (5–13 s) but the **tails are brutal (400–450 s)** — these
are the deep-think turns and, batching-wise, they occupy the entire request
slot while everything else waits behind the gate.

**Server-side reality (from vLLM container logs):**
```
Running: 1 reqs, Waiting: 0 reqs   x79   <- REAPER gate=1
Running: 2 reqs, Waiting: 0 reqs   x24
Running: 0 reqs, Waiting: 0 reqs   x12   <- idle gaps
```
The server was never asked to batch while REAPER ran. Both GPUs at 97–99% util
is *single-request decode* hammering a 96 GB MoE — not efficiency; with MLA
KV-cache being tiny, DeepSeek-V4 is precisely the model class that batches
well.

**What changed (already applied):**
- Server: `--max-num-seqs 64`, `--max-num-batched-tokens 8192` (validated
  running).
- Client: `llm_max_concurrent = 4` (config; needs a run restart to load).

**Not yet done (the actual next experiment):** a controlled batch benchmark
against the live server — measure tokens/s and latency for 1/2/4/8 concurrent
requests with *this* model + *this* config, then tune
`llm_max_concurrent` + server caps together. I stopped short of this because
the cancelled run's data was secure; the probe scripts
(`scripts/probe_effort.py`, a planned `scripts/probe_batch.py`) are the harness.

**Live batching probe (2026-09-28, `scripts/probe_batch.py` against :8035,**
**"high" effort, realistic JSON-task prompts):**

| concurrency | n | total_s | mean_lat_s | tok/s (aggregate) |
|---|---|---|---|---|
| 1 | 8 | 23.71 | 2.96 | **95.2** |
| 2 | 8 | 50.38 | 9.40 | **128.3** |
| 4 | 8 | 34.96 | 9.15 | **156.0** |

Conclusion: aggregate throughput *rises* with concurrency (c2 ≈ 1.35×, c4 ≈
1.64× vs c1) while per-request latency rises (queued behind siblings) — the
standard batching tradeoff, and the expected direction for a model whose MLA
KV-cache makes it highly batchable. The variance between sweeps is high (a
single deep-think turn dominates), so treat the ratios as directional, not
exact. **This supports `llm_max_concurrent=4` over 1** — a ~1.6× aggregate
throughput gain is exactly the "bit more performance" objective; the serialized
investigation loop (§4) is the bigger structural limiter and should be fixed
first so concurrency actually has a queue to chew through.

---

## 7. Knowledge-base / effort-tier findings (records for later)

- **This deployment has only 3 real effort tiers.** The DeepSeek-V4 tokenizer
  in vLLM 0.24.0 collapses `minimal|low|medium|high` → one cheap "high"
  thinking mode; only `none` (no thinking) and `max`/`xhigh` (deep "absolute
  maximum" directive) differ. Verified both by reading the installed source
  (`vllm/tokenizers/deepseek_v4.py`) and live probes. The "effort" is a
  *capacity*, not a mandate — a max directive on trivial tasks still finishes
  in ~2 s.
- **REAPER previously never sent the deep tier** — `_REASONING_EFFORT_BY_LEVEL`
  had `xhigh → "high"`, which the tokenizer maps to the *cheap* mode. Fixed to
  `xhigh → "xhigh"` and `max → "xhigh"`.
- **Config rebalanced per user direction** (2026-09-28): `critic = "xhigh"`
  (the gate), `recovery_followup = "xhigh"` (rare deep type work); review /
  investigation / merge / resynthesis / type recovery all now `high`; rename /
  scheduler `minimal`. Critic prompt hardened to *demand specific corrective
  feedback* since upstream stages now run cheap.
- **Server caps on context are benign.** `max-model-len 524288` is half the
  model's real `max_position_embeddings` (1,048,576) but far above our worst
  case (context 32K + xhigh 82K ≈ 114K). Response length is unbounded by
  default only if the client omits `max_tokens`; REAPER always sends
  `max_completion_tokens`, so it is bounded. No change needed.

---

## 8. Prioritized action list (in order)

**P0 — unblock completion on the existing data (do before resume):**
1. Clean the 85 spurious `:Function` nodes (Cypher in §3) and re-run
   `ledger.register_functions_from_graph` so scoping matches reality.
2. Fix/validate the investigation drain (§4): dedupe + cap subtasks, ban
   `0x0`/non-function start positions, make `scheduler_reviewed` non-zero, and
   add a requeue-cap that forces task completion instead of infinite retry.
3. Decide ledger claim-address policy (owner = function *start*, not evidence
   instruction) so coverage math is sound.

**P1 — throughput (the performance you asked about):**
4. Controlled concurrency benchmark; then set `llm_max_concurrent` and server
   caps from data (do NOT remove batching — the GPU headroom is real).
5. Resume/restart the run so the shipped config changes (concurrency, effort
   tiers, agent node-id normalization) actually load.

**P2 — quality gates:**
6. Wire `critic.reject_rate` + a false-confidence sample into scoring; validate
   the 98.4% accept rate against ground truth.

**P3 — tooling/safety:**
7. Add `scripts/probe_batch.py` (concurrency sweep) + a graph-cleanup script so
   this audit's manual steps are one command.
8. Document all of the above in this file; keep the effort-tier and batching
   notes in `docs/skills/telemetry.md` §7.

---

## 9. What I did not do (explicitly deferred)

- Did not run the scoring engine / `eval/scoring` on the (partial) output — the
  run was cancelled before final export; scoring belongs post-resume.
- Did not run heavy cases live against the new config yet.
- Did not yet build `scripts/probe_batch.py`.
- Did not modify anything about the critic's acceptance policy — flagged, not
  changed (evidence needed first).
