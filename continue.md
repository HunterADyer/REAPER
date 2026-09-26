# REAPER RE Stage — Implementation Kickoff

You are implementing a reverse engineering harness from a detailed design spec.
This document tells you how to start. Read it once, then follow the workflow.

## What Exists

```
reaper/
  .clinerules              ← ALWAYS READ. Hard rules that prevent broken code.
  docs/
    design-re-stage.md     ← THE SPEC. 28 deliverables, ~1800 lines. Never modify.
    binja-module.md        ← Binja 6.0 API reference. Read when touching reaper/tools/.
    skills/
      patterns.md          ← Code templates. Read before implementing. Fill in verified examples.
      cross-references.md  ← Constructor/import/config checklists. Check before writing constructors.
      progress.md          ← Track what's done. Update after each deliverable.
  eval/
    juliet/                ← Juliet test suite (pre-existing, don't touch)
    primevul/              ← PrimeVul eval (pre-existing, don't touch)
```

Everything in `reaper/harness/`, `reaper/tools/`, `reaper/agents/`, `reaper/configs/`,
`reaper/infra/`, and `reaper/scripts/` is empty. You are building from scratch.

## Prerequisites (do these before writing any code)

### 1. Python environment
```bash
python3 -m venv ~/reaper-venv
source ~/reaper-venv/bin/activate
```

### 2. Neo4j
```bash
# After implementing 1.2 (docker-compose.yml):
cd reaper/infra && docker compose up -d
```

### 3. Binja headless
Verify Binja Python is importable:
```bash
PYTHONPATH=$HOME/binja-headless/python python3 -c "import binaryninja; print('ok')"
```
If this fails, find the correct Binja path and update PYTHONPATH.

### 4. vLLM
The vLLM server must be running on :8010 with DeepSeek-V4-Flash.
You don't start this — it's managed separately. Just verify:
```bash
curl -s http://localhost:8010/v1/models | python3 -m json.tool
```

## Implementation Strategy

### Phase 1 — do these first, in this order:

| Order | Deliverable | Why this order |
|---|---|---|
| 1st | **1.1** Project Skeleton | Creates package structure everything else imports from |
| 2nd | **3.3** Submission Protocol | Pydantic models referenced by almost everything |
| 3rd | **1.2** Neo4j Schema | Graph store needed by Phase 2 |
| 4th | **1.3** vLLM Client | LLM interface needed by all agents |
| 5th | **1.4** Tracer | Logging needed by all dispatchers |
| 6th | **1.5** Ledger | Claim store needed by agents and dispatchers |
| 7th | **1.6** TODO Ledger | Task queue needed by investigation loop |

Why 3.3 so early: every agent, dispatcher, and critic imports models from
`submission.py`. Getting these defined first prevents circular dependency issues
and lets you test model schemas early.

### Phase 2 — graph construction (sequential, each builds on the last):
2.1 → 2.2 → 2.3 → 2.4 → 2.5

Build 8.1 (cJSON test binary) alongside or before Phase 2 — you need a
stripped binary to test the extractors against.

### Phase 3 — harness core:
3.1 first (context assembler, used by everything), then 3.2, 3.4, 3.5 in any order.

### Phases 4–8 — follow the design doc order:
4.1 → 4.2 → 4.3 → 5.1 → 5.2 → 6.1 → 6.2 → 6.3 → 7.1 → 7.2 → 7.3 → 7.4 → 7.5 → 8.2 → 8.3

## Per-Deliverable Workflow

Repeat this for every deliverable:

```
1. READ the deliverable section:
   Search design-re-stage.md for "### Deliverable X.Y"
   Read ONLY that section (don't load the whole doc).

2. CHECK cross-references:
   Open docs/skills/cross-references.md
   Verify the constructor signature, imports, and config keys.

3. READ the relevant pattern:
   Open docs/skills/patterns.md
   Find the matching code template.

4. IMPLEMENT:
   Create the file at the path specified in the deliverable.
   Follow the interface exactly — same method names, same arg order.
   Use the pattern template as your starting point.

5. TEST:
   Each deliverable has a "Test:" section — implement those checks.
   Run with: python3 -m pytest or a standalone asyncio.run() script.

6. UPDATE progress:
   Check the box in docs/skills/progress.md
   Add any notes about surprises or decisions in the <!-- NOTES --> section.

7. UPDATE patterns (if applicable):
   If this deliverable has a <!-- FILL --> placeholder in patterns.md,
   paste your verified working code there.
```

## First Session — Do Exactly This

```
1. Read .clinerules (you should have already — it's always loaded)

2. Implement deliverable 1.1:
   - Create pyproject.toml, requirements.txt, all __init__.py files, .gitignore
   - Create configs/default.toml with the exact TOML from the design doc
   - Run: pip install -e .
   - Verify: python3 -c "import reaper"

3. Implement deliverable 3.3 (submission.py):
   - Create reaper/harness/submission.py with ALL Pydantic models
   - Create get_schema() and parse_response() utility functions
   - Test: roundtrip every model through model_dump_json/model_validate_json

4. Implement deliverable 1.2:
   - Create infra/docker-compose.yml and infra/schema.cypher
   - Create infra/init_db.py with async def init_schema()
   - Run: docker compose up -d
   - Test: run init_schema, verify constraints exist

5. Implement deliverable 1.3:
   - Create harness/llm_client.py
   - Test: create session, send a message, verify response

6. Continue with 1.4, 1.5, 1.6, then Phase 2.
```

## What NOT To Do

- **Don't read the entire design doc at once.** It's 1800 lines. Read one deliverable at a time.
- **Don't skip cross-reference checks.** Constructor arg order mismatches are the #1 integration bug.
- **Don't use `guided_json`.** vLLM silently ignores it. Use `structured_outputs`.
- **Don't create classes for build_nodes/build_edges/pin_symbols/validate_and_order.** They are module-level `async def` functions.
- **Don't add `binaryninja` to requirements.txt.** It's not pip-installable.
- **Don't modify anything in docs/.** Those are reference documents.
- **Don't write blocking I/O in async functions.** Everything is async.
- **Don't try to implement multiple phases at once.** Finish and test each deliverable before moving to the next.

## When You Get Stuck

1. Re-read the deliverable section in design-re-stage.md — the answer is usually there.
2. For Binja API questions, read docs/binja-module.md.
3. For async/Neo4j/SQLite patterns, check docs/skills/patterns.md.
4. If the design doc is ambiguous, make a note in progress.md's "Discovered Issues"
   section and make a reasonable choice — don't block on it.
5. If a test fails, fix the implementation — don't weaken the test.
