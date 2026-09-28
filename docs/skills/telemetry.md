# Telemetry / Observability & Instrumentation Design (agents, read before coding)

> **Audience:** any agent adding, changing, or debugging pipeline code.
> **Goal:** a complete, uniform, *non-negotiable* instrumentation discipline so
> that (a) every meaningful action the pipeline takes is recorded, (b) live
> monitoring ("Run Cockpit") and (c) RL/tuning corpus capture all work without
> per-feature hacks, and (d) debugging any live failure is fast.
> **Rule of thumb:** *if a new feature can do I/O — an LLM call, a graph query,
> an SQL write, a tool access, a loop step, a claim mutation — it MUST emit
> before it does the thing and after it resolves, or it is not done.*

---

## 1. Why this exists (and what we learned the hard way)

REAPER's first validated run against real dependencies (2026-09-27) exposed a
recurring failure class: **code that only ran against mocks silently misbehaved
against the real stack** (Binja API drift, invalid Cypher, non-expression HLIL
operands). We had *no* unified live telemetry to see this happening. This doc
is the contract that prevents that. Everything below is **already implemented** —
this is the design + the checklist for extending it, not a wishlist.

The instrumentation is intentionally **layered**, so future RL/tuning work
plugs in as a *subscriber* and never touches the pipeline:

```
pipeline (run.py, harness, agents, tools)
   │  emit() / emit_sync()                         ← hard rule: fire-and-forget
   ▼
EventBus  (harness/events.py — process-local async pub/sub, bounded)
   ├──► GUI Run Cockpit (reaper.gui)  : WS broadcast + REST {/api/state, /api/events?after=…}
   ├──► DebugLogWriter (harness/debugtrace.py) : data/<run>_events.jsonl  ← big-big log, every event
   └──► RLTraceWriter (harness/rltrace.py)      : data/<run>_rltrace.jsonl ← clean LLM turns for training
```

**Critical implication:** the bus is *process-local*, therefore **the GUI hosts
the run in-process**. `reaper.run` (headless CLI) and `reaper.gui.main`
(monitoring host) share the exact same pipeline code; both emit.

---

## 2. The unbreakable rules (these ARE the contract)

1. **Telemetry never breaks the pipeline.** `emit()` never raises. Any consumer
   (GUI/RL/debug writer) faulting does not disturb a run. There is no
   "instrumentation exception" that should ever propagate.
2. **Event payloads are JSON-serializable** (dict of str/int/float/bool/list/
   dict/None). `emit()` raises `TypeError` on a non-dict payload — fix the
   caller, never work around it.
3. **Never block on a slow subscriber.** `publish()` uses `put_nowait`; a full
   subscriber queue drops its oldest event, not the pipeline.
4. **Emit through choke points, not random spots.** One LLM client, the tracer
   mirror, one traced Neo4j driver, SQLite trace-callbacks, and the extractor
   access helper are the ONLY instrumentation seams. Do not add ad-hoc logging
   strewn through agent code; go through the existing emit paths.
5. **Both async and sync emitters exist.** Use `await emit(...)` when you are
   already async. Use `emit_sync(...)` at sync boundaries (SQLite trace
   callbacks, hot CPU readers) — it no-ops without a running loop.
6. **One LLM request at a time (GPU stability).** `ReaperLLMClient` serializes
   everything through an `asyncio.Semaphore` (`config [limits]
   llm_max_concurrent`, justified cap — see §7). Agent fan-out only overlaps
   CPU work. Never "helpfully" open a second concurrent request.
7. **Thinking traces are captured, always.** The LLM client reads the model's
   reasoning (`message.reasoning` / `reasoning_content` tolerant parse) and
   ships it in `llm.response`; the RL writer persists it. Do NOT strip it.
8. **Thinking depth is budget-driven and per-stage.** `configs/default.toml
   [thinking_levels]` maps each stage to a TOTAL `max_completion_tokens`
   budget (minimal=1k … max=41k, **xhigh=82k**). DeepSeek reasons harder with
   more token allowance; `xhigh` additionally sends
   `chat_template_kwargs.reasoning_effort=high` (vLLM honors it for DeepSeek).
   Don't creep budgets up globally — escalate only stages that are
   quality-gated or load-bearing, and never bypass the one-at-a-time gate.
9. **Live logs never rely on `print`.** `print` in the pipeline is for humans
   at the console; everything else that could matter is an event first.

---

## 3. Event vocabulary (authoritative registry)

Every event an agent can emit is listed below. **New event types MUST be added
to `Tracer._ALLOWED_EVENT_TYPES`** (harness/tracer.py) — unknown types are
warned-and-dropped, so an unregistered type silently vanishes from the trace
file (and, at the GUI, from type-filtering). The list intentionally covers the
*reserved* vocabulary too (documented future events).

**Pipeline / run lifecycle** (emitted by run.py, session `"runner"`):
`run.started`, `run.phase` (`{phase, status: start|done}`), `run.complete`,
`run.error`, `pipeline_phases_done`, `pipeline_complete`.

**LLM / dialogue (the heart of RL capture):**
- `llm.request` — emitted BEFORE the gate; `{run_id, thinking_level,
  max_completion_tokens, timeout_s, message_chars, n_messages}`.
- `llm.response` — emitted on success INSIDE the gate; **full** `{messages
  (as sent), content, reasoning (thinking trace), usage, duration_s,
  n_retries, timeout_s}`. This single event is the self-contained RL record.
- `llm.error` — exhausted retries: `{error, thinking_level, …}`.

**Access records (the "what did it touch" log):**
- `graph.query` — every Neo4j `session.run` via `traced_driver()`:
  `{label, query, params, duration_s, (ok, error)}`.
- `sql.access` — every ledger/todo SQL statement via `set_trace_callback`:
  `{sql}` (volumetric; filtered out of the debug *file* by default, kept in bus).
- `tool.access` — Binja extractor reads via `HLILExtractor._emit_access`:
  `{method, (address)}`.

**Agent / execution events** (mirrored from Tracer — full set in tracer.py):
`rename_agent`, `review_agent`, `claim_accepted`, `claim_rejected`,
`claim_force_accepted`, `pass1_*`, `pass2_*` (`pass2_review_retry` is the
critic retry loop), `scheduler_*`, `investigation_*` (`investigation_stuck`,
`investigation_agent`, `investigation_iteration_cap`), `resynthesis_iteration`,
`completion_coverage`, plus reserved `merge_*`, `critic_feedback`, `rename`,
`task_*`, `graph_mutation`, `llm_request/response`.

### How to add a NEW event type (4 steps, no shortcuts)
1. Add the name to `_ALLOWED_EVENT_TYPES` in `harness/tracer.py`.
2. Emit it at the correct seam (`await emit(...)` or `emit_sync(...)`).
3. Add a `test_*_emits_<event>` to `tests/test_tracer.py`
   (parametrized over the emitted set — it already enumerates all event types).
4. If the GUI should surface it (dialogue vs loop vs access vs claim), add a
   render branch in `gui/static/app.js` **and** a counter in
   `gui/main.py::/api/loops` only if it is loop-relevant.

---

## 4. The three durable sinks (what gets written where, and why)

| Sink | Path | Contents | Purpose |
|---|---|---|---|
| **RL trace** | `data/<run_id>_rltrace.jsonl` | one clean JSON line per LLM turn: run_id, session_id, thinking_level, max_tokens, timeout, messages, response, **reasoning**, usage, duration, retries, status | SFT / preference / RL training corpus; self-contained per record, no joins |
| **Debug firehose** | `data/<run_id>_events.jsonl` | **every** event, full payloads, strictly ordered (`seq`) | "big-big logging" for debugging; the ground truth of what happened |
| **Tracer** | `data/traces/<run_id>/trace.jsonl` | the legacy structured event log (subset, JSONL) | backwards-compatible pipeline trace |

The **EventBus history** (in-memory, bounded) is NOT a durable sink — it only
serves pull monitoring (`/api/events?after=`). The durable debug/rl files are
the source of truth after a crash.

`ReaperLLMClient` refuses to lose RL data: the RL record is written from the
*response event* which contains the full prompt — never reconstruct prompts
from history later.

---

## 5. GUI + pull-monitoring surface (how you/we watch a run)

- `python -m reaper.gui.main --detach` → background daemon (pidfile/gui.log);
  `--stop`, `--status`. Foreground with `--serve`.
- Browser: single static page — Rename Board, Active Dialogues (incl. thinking),
  and side drawers (Claims & Tasks, Call Graph, LLM Health, Trace Feed,
  Loops & Limits).
- **Pull APIs for scripted/CI monitoring (no browser):**
  - `GET /api/state` — run status, phase map, coverage, limits, file paths.
  - `GET /api/events?after=<seq>&limit=&types=` — event stream cursor.
  - `GET /api/functions`, `/api/edges`, `/api/claims`, `/api/tasks`,
    `/api/loops` (counters vs configured caps), `/api/health`.
  - `POST /api/run/start` / `/api/run/stop`.
- The GUI never mutates pipeline state — it only reads stores/subscribes.

---

## 6. Checklist for ANY new feature (fill this in before PR/commit)

**Instrumentation**
- [ ] Does it perform LLM I/O? → covered by the client; nothing to do (but do
      NOT bypass `ReaperLLMClient.send`).
- [ ] Does it write to Neo4j? → via `neo4j_driver` constructed in run.py (it is
      already `traced_driver`-wrapped). If you create your OWN driver, wrap it:
      `traced_driver(driver, label=..., run_id=...)`.
- [ ] Does it read/write SQLite (ledger/todo)? → touched via existing
      `Ledger`/`TodoLedger` handles (trace callbacks active). If you open a NEW
      aiosqlite connection, call `await conn.set_trace_callback(...)`.
- [ ] Does it read the binary view (Binja)? → via `HLILExtractor` methods
      (they self-emit `tool.access`). Instantiate a NEW extractor? call
      `extractor._emit_access("your_method", addr)` at the top.
- [ ] Does it introduce a new event type? → registry + test + GUI branch (§3).
- [ ] Is any payload non-JSON-serializable? → coerce to str/int.

**RL discipline**
- [ ] Does the LLM's thinking matter here? → already captured in `llm.response`
      `reasoning`. Do not strip; do not store only `content`.
- [ ] The RL record must remain *self-contained* — never rely on out-of-band
      context in the trainer.

**Termination (no arbitrary limits)**
- [ ] Any new loop: is it *provably* terminating, or does it need a JUSTIFIED
      cap (documented in §7)? Never add a cap "to be safe".
- [ ] Surface the loop's live counters in the GUI Loops & Limits drawer if it
      could reasonably spin.

**Tests**
- [ ] `tests/test_tracer.py` parametrized event vocabulary still green.
- [ ] `pytest tests/ -q` runs green (mock layer ⇎ real dependencies — see §8).
- [ ] If it owns a new event, the debug/rl writers were exercised by a tiny
      run (`--phases 3,4` on cJSON) and the JSONL files contain the event.

---

## 7. Limits register — every cap justified (potentially no caps needed)

The project intentionally runs *unbounded by default*; caps exist only when
there is a demonstrated reason, and each is config-driven in `configs/default
.toml` `[limits]` **and** surfaced live in the Loops & Limits drawer so they
can be tuned with evidence, never blindly.

| Cap | Value | Justification (why this is NOT arbitrary) |
|---|---|---|
| `llm_max_concurrent` | 1 | GPU stability on the shared :8035 vLLM (user requirement); burst concurrency risked dropping the GPU worker. Enforced at the client gate. |
| `max_critic_rejections` | 3 | Rejection→delete→retry loop must not livelock; after N genuine failures the claim is force-accepted at `speculation` (an explicit, principled escape — never silently skipped). |
| `max_review_retries` | 2 | Pass-2 review→critic loop: bounded regression budget, then the review's renames still land via merge (claims excluded), so no work is lost. |
| `max_resynthesis_iterations` | 5 | Investigation↔resynthesis feedback loop stops on *stability* (0 new tasks), so the cap is only a livelock guard, not a quality cut. |
| `max_investigation_iterations` | 200 | Drain loop guard; deadlock-free by design (one-way deps, stale reset), 200 is far beyond any legit drain and exists to never hang a run. |
| `max_type_recovery_rounds` | 5 | Fixed-point loop stops early on *no structural change*; cap guards pathological rebuild thrash. |
| `task_timeout_seconds` | 600 | Stale in_progress reset clock; unparsable timestamps are treated stale (conservative). |

**Policy for future caps:** add one ONLY with (a) a concrete failure/scenario
that can't self-terminate, (b) an accompanying live counter so we can observe
it, and (c) a note here. Observed non-termination hazards to watch:
long-thinking LLM requests (mitigated by per-level timeouts, see
`llm_client._TIMEOUT_BY_LEVEL`) and genuine livelock between investigation and
resynthesis (mitigated by the stability stop, not a race).

---

## 8. Test philosophy reminder (mock layer ⇎ reality)

Mock-based tests (fake Binja / fake Neo4j / fake vLLM) are the Tier-1 gate but
have repeatedly diverged from the real stack. Any new real-dependency surface
must be **smoke-validated live** before being trusted:
`PYTHONPATH=$HOME/binja/binaryninja/python python3 scripts/smoke_binja.py`
(covers open_view→load, HLIL extraction, sym/stripped alignment) and a
`--phases 3,4` mini run through the GUI's `/api/run/start`.

---

## 9. Authoritative pointers (do not reinvent)

- `harness/events.py` — `EventBus`, `bus`, `emit()`, `emit_sync()`.
- `harness/rltrace.py` — `RLTraceWriter` (LLM-turn corpus; never edit its schema
  without a migration note).
- `harness/debugtrace.py` — `DebugLogWriter` (firehose).
- `harness/tracedriver.py` — `traced_driver()` (graph access gate).
- `harness/llm_client.py` — the ONLY LLM seam; timeout-by-level table; gate.
- `harness/tracer.py` — event-type registry + file trace + bus mirror.
- `gui/main.py`, `gui/static/*` — consumers. `eval/llm_score.py` — LLM-judge,
  rubric-based, N-run-averaged name scoring (see §10).

---

## 10. Scoring (original vs recovered, LLM-judge + rubric, avg of N)

**Status 2026-09-27:** scoring is now the MODULAR engine (`eval/scoring/`),
`eval/llm_score.py` is a thin compat shim. The independence contract is
enforced and unit-tested:

> A "scoring run" is ONE fresh request to the vLLM endpoint with NO context and
> NO history — each (item, run) gets a brand-new session created right before
> the call and destroyed immediately after. Nothing leaks between runs.

1. **Rubric registry** (`eval/scoring/rubrics.py`) — one entity per scored
   dimension: function-name, variable-name, datatype (struct layout). Adding a
   dimension is a one-place change; never edit bands in place (rubrics are
   versioned; cache keys carry the id INCLUDING version).
2. **JudgeEngine** (`eval/scoring/judge.py`) — scores items against a rubric
   over N independent zero-context runs (default N=5), resumable JSONL cache
   keyed by (rubric, item, run), strictly one request at a time, "minimal"
   thinking. THESE are the "5 independent scoring runs" the design requires.
3. **ScoreScheduler** (`eval/scoring/scheduler.py`) — takes the evidence
   (recovered fn/var names + datatypes from align.py) and just schedules every
   item through the rubric scorer N independent times, then aggregates
   (`reporting.py`: mean, std, >=8 equivalent rate, >=5 related rate, failed/
   no-rename rate).
4. **Dimensions** — functionwise = function names AND variable names (aligned
   by address / address+ordinal); datatype = recovered struct layouts scored
   against the ground-truth layout (offsets + names + types in the prompt).

Run (multi-dimension CLI):
`python -m reaper.eval.scoring --ground-truth eval/cjson/ground_truth.json
--reaper-output data/cjson_001_reaper_output.json --n-runs 5`

The same scorer is reused by `eval/heavy/` (per-mechanism heavy tests score
the mechanism's output, not the full pipeline) — the "make the scoring
harness/rubric thing modular" requirement.

*End of telemetry/observability design. Extend this doc alongside any new
instrumentation — it is the checklist agents are expected to follow.*
