---
status: design
version: 4
updated: 2026-09-25
---

# REAPER RE Stage — Implementation Design

Eight phases, 28 deliverables. Each is independently implementable and testable by a local coding agent. Every deliverable has: what to build, where it goes, inputs, outputs, and how to know it works.

**Architectural decisions (cross-cutting):**
- **Fully async.** All I/O-bound operations (LLM calls, Neo4j queries) use `asyncio` + `httpx.AsyncClient` / `neo4j.AsyncDriver`. Dispatchers use `asyncio.Semaphore` for concurrency control. The pipeline runner is `asyncio.run(main())`.
- **Claims are created before critic evaluation.** When an agent produces a claim, the harness inserts it into the ledger with `truth_level=NULL` immediately. The critic then evaluates and sets the truth_level. This allows rejection counting on a real row. `all_functions_have_claims()` checks for claims with truth_level IS NOT NULL.
- **No cross-store atomicity.** Neo4j and SQLite are separate stores. Writes to both are best-effort sequential (Neo4j first, then SQLite). If one fails, log the inconsistency and continue. Acceptable for a research prototype — not a production concern.
- **Idempotent graph construction.** Node and edge builders use `MERGE` (not `CREATE`) in Neo4j. Re-running after a crash is safe — existing nodes/edges are matched, not duplicated.
- **Binja version: 6.0 headless.** All Binja API references target the 6.0 Python API. See `docs/binja-module.md` for the full API surface spec.
- **SQLite concurrency.** Ledger and TodoLedger use `aiosqlite` for async access. All writes are serialized via `asyncio.Lock` per database. WAL mode is enabled (`PRAGMA journal_mode=WAL`) on connection. Reads are concurrent; writes queue behind the lock. Without this, concurrent agents hitting `add_claim()` / `create_task()` will get `SQLITE_BUSY`.
- **Thinking budget = thinking + response.** `max_completion_tokens` is the TOTAL budget for thinking tokens AND the structured JSON response. Values are set high enough that structured output (typically 200-2000 tokens) doesn't crowd out thinking. E.g., "minimal" = 1024 total, not 512.

---

## Phase 1: Infrastructure

Everything else depends on this. No LLM calls yet — pure plumbing.

### Deliverable 1.1: Project Skeleton + Dependencies

**What:** Python package structure, requirements file, and configuration.

**Files:**
- `reaper/pyproject.toml` — package metadata, entry point `reaper.run:main`
- `reaper/requirements.txt`
- `reaper/configs/default.toml`
- `reaper/__init__.py`
- `reaper/harness/__init__.py`
- `reaper/tools/__init__.py`
- `reaper/agents/__init__.py`
- `reaper/agents/prompts/` (directory, empty for now)
- `reaper/infra/__init__.py`
- `.gitignore`

**.gitignore:**
```
__pycache__/
*.pyc
*.bndb
reaper/data/
reaper/eval/cjson/cjson_test
reaper/eval/cjson/cjson_test_symbols
reaper/eval/cjson/cJSON.*
reaper/eval/cjson/main.c
```

**requirements.txt:**
```
neo4j>=5.0
httpx>=0.27
pydantic>=2.0
aiosqlite>=0.20
```

**Note on binaryninja:** Not pip-installable. Binja headless ships its own Python site-packages. The implementing agent must ensure Binja's Python path is on `sys.path` (e.g., `export PYTHONPATH=$HOME/binja-headless/python`). Do NOT add `binaryninja` to requirements.txt.

**Note on tomllib:** Python 3.11+ has `tomllib` in stdlib. Use that, not the `tomli` third-party package.

**default.toml:**
```toml
[neo4j]
uri = "bolt://localhost:7687"
user = "neo4j"
password = "reaper"

[vllm]
base_url = "http://localhost:8035/v1"
model = "deepseek"

[thinking_levels]
# Maps to max_completion_tokens (TOTAL budget: thinking + response).
# Structured JSON responses typically consume 200-2000 tokens, so
# these values leave headroom beyond the thinking portion.
# minimal=1024, low=4096, medium=12288, high=20480, max=40960.
# AUDIT 2026-09-27 — policy: QUALITY-GATED -> high/xhigh; NOT gated -> minimal.
pass0_type_recovery = "minimal"   # ungated (struct claims recorded at "inferred")
pass1_rename = "minimal"          # ungated (merge is conflict-check only)
pass2_review = "max"              # GATED — main claim source + task critic
critic = "high"                   # GATED — the gate itself (sets truth_level)
investigation = "high"            # GATED — claims critic-gated, requeue on reject
merge = "max"                     # conflict-resolution correctness is FINAL for
                                  #   renames (no later gate) — max per 2026-09-27
resynthesis = "max"               # edits ledger directly (merged/deleted claims);
                                  #   load-bearing, no later gate — max per 2026-09-27
scheduler = "minimal"             # ungated task planning

[limits]
max_critic_rejections = 3          # after this, accept at 'speculation' and move on
max_resynthesis_iterations = 5     # prevent investigation<->resynthesis infinite loop
shadow_copy_hops = 2               # N-hop neighborhood for shadow checkouts
task_timeout_seconds = 600         # stale task timeout
max_concurrent_agents = 8          # parallel vLLM sessions
max_context_tokens = 32768         # truncation limit for context assembly

[paths]
data_dir = "data"
traces_dir = "data/traces"
```

**Test:** `pip install -e .` succeeds. `from reaper.harness import llm_client` imports without error. `tomllib.load(open("reaper/configs/default.toml", "rb"))` parses without error.

---

### Deliverable 1.2: Neo4j Instance + Schema

**What:** Docker compose for Neo4j, plus a schema initialization script that creates constraints and indexes.

**Files:**
- `reaper/infra/docker-compose.yml` — Neo4j 5.x container, bolt on :7687, browser on :7474, volume mount for persistence at `reaper/data/neo4j`
- `reaper/infra/schema.cypher` — run on first boot
- `reaper/infra/init_db.py` — `async def init_schema(neo4j_driver)`: connects to Neo4j, runs schema.cypher, verifies constraints

**docker-compose.yml must include:**
```yaml
environment:
  - NEO4J_AUTH=neo4j/reaper
```

**Schema:**
```cypher
// Node constraints
CREATE CONSTRAINT FOR (f:Function) REQUIRE f.address IS UNIQUE;
CREATE CONSTRAINT FOR (v:Variable) REQUIRE v.id IS UNIQUE;
CREATE CONSTRAINT FOR (a:Argument) REQUIRE a.id IS UNIQUE;
CREATE CONSTRAINT FOR (c:Call) REQUIRE c.id IS UNIQUE;
CREATE CONSTRAINT FOR (s:StringRef) REQUIRE s.id IS UNIQUE;

// Node properties (all node types):
//   address         — hex address in binary
//   llm_name        — verbose pothole_case name (null until renamed)
//   canon_name      — human-readable name (null until renamed)
//   pinned          — boolean, true = known symbol, agents cannot rename
//   ambiguous       — boolean, true = unresolved indirect/missing info
//   scc_id          — integer, SCC group ID (null if not in a cycle)
//   traversal_order — integer, topological order for processing (lower = process first)
//   isolated        — boolean, true = dead/empty function (no callers, no vars)

// StringRef properties:
//   value           — the literal string content
//   address         — address where string is referenced

// Edge types (structural, for traversal — no taxonomy beyond type):
//   :DATAFLOW_ASSIGN  — variable = expression
//   :DATAFLOW_ARG     — value passed as argument to call
//   :CALL             — function calls function
//   :RETURN           — return value flows to caller
//   :FIELD_OF         — struct field belongs to struct type
//   :DEFERRED_BACK    — back-edge in SCC, deferred for second pass
//   :REFS_STRING      — variable or argument references a string constant
//   :CONTAINS         — function contains variable/argument/call (structural ownership)

// Indexes
CREATE INDEX FOR (f:Function) ON (f.scc_id);
CREATE INDEX FOR (f:Function) ON (f.traversal_order);
CREATE INDEX FOR (v:Variable) ON (v.address);
CREATE INDEX FOR (a:Argument) ON (a.address);
```

**Test:** `docker compose up -d`, run `init_db.py`, verify constraints exist via Cypher query. Verify auth works with the password from config.

---

### Deliverable 1.3: vLLM Client Wrapper

**What:** Async Python client that talks to the local vLLM server. Manages multiple concurrent sessions, each with its own conversation history.

**Files:**
- `reaper/harness/llm_client.py`

**Interface:**
```python
class LLMTransientError(Exception):
    """Raised after max retries on transient vLLM errors."""
    pass

class ReaperLLMClient:
    def __init__(self, base_url: str, model: str, max_retries: int = 3):
        """Create httpx.AsyncClient pointing at vLLM server."""

    async def create_session(self, session_id: str, system_prompt: str) -> str:
        """Create a new agent session with a system prompt. Returns session_id."""

    async def send(self, session_id: str, message: str,
                   thinking_level: str = "low",
                   structured_output: dict | None = None) -> str:
        """Send a message in a session, return the response text.

        thinking_level: 'minimal', 'low', 'medium', 'high', 'max'
        Maps to max_completion_tokens budget (thinking + response combined):
            minimal=1024, low=4096, medium=12288, high=20480, max=40960
        Passed via extra_body={"max_completion_tokens": N}.
        DeepSeek-V4 uses <think> tags automatically within that budget.
        The budget must cover both thinking AND the JSON response.

        structured_output: Pydantic model_json_schema() dict.
        If provided, sends OpenAI-standard response_format={"type":"json_schema",
        "json_schema":{"name": <schema title | "response">, "schema": schema}}.
        Verified live 2026-09-27 against the shared :8035 vLLM: the formerly
        documented extra_body={"structured_outputs": ...} and guided_json are
        SILENTLY IGNORED by this build — do not use them.

        Retries transient errors (HTTP 5xx, connection refused,
        httpx.TimeoutException) with exponential backoff: 1s, 2s, 4s.
        After max_retries, raises LLMTransientError."""

    def get_history(self, session_id: str) -> list[dict]:
        """Return full conversation history for a session (sync, CPU-only)."""

    def destroy_session(self, session_id: str):
        """Clean up a session's state (sync, CPU-only)."""

    async def close(self):
        """Close the httpx.AsyncClient."""
```

**Details:**
- Conversation history stored CPU-side in memory (dict of lists)
- Each `send()` call ships the full history + new message to vLLM `/v1/chat/completions`
- Per-session asyncio.Lock — prevents concurrent sends on the same session (different sessions run in parallel freely)
- Uses `httpx.AsyncClient` with configurable timeout (default 120s)
- **Most agents are single-turn** (one system prompt + one user message → structured response). Multi-turn history is only used by the critic rejection→resubmit flow (where a new session is created with feedback baked in, not continuing the old session). The session machinery is kept for future extensibility but v1 is overwhelmingly single-turn

**Test:** `asyncio.run()` test: create session, send "What is 2+2?", get response, verify history has 2 entries. Test retry: mock a 503, verify it retries and succeeds on second attempt.

---

### Deliverable 1.4: Logging / Trace Infrastructure

**What:** Structured JSON logging for every agent interaction. Thread-safe via asyncio.Lock on the file handle.

**Files:**
- `reaper/harness/tracer.py`

**Interface:**
```python
class Tracer:
    def __init__(self, log_dir: str):
        """log_dir = reaper/data/traces/<run_id>/
        Creates dir if not exists. Opens trace.jsonl for append.
        Stores self.log_dir = log_dir for later reference."""

    async def log(self, event_type: str, session_id: str, data: dict):
        """Append a JSON line to the trace log. Acquires lock, writes, flushes.
        event_type: 'llm_request', 'llm_response', 'claim_submit',
                    'critic_feedback', 'merge_attempt', 'merge_result',
                    'rename', 'task_create', 'task_complete', 'graph_mutation'
        """
```

**Format:** One JSONL file per run: `reaper/data/traces/<run_id>/trace.jsonl`
```json
{"ts": "2026-09-25T14:30:00Z", "event": "llm_request", "session": "pass1_func_0x1400", "data": {"message": "...", "thinking_level": "minimal"}}
```

**Test:** Log 10 events concurrently (via asyncio.gather), read back the JSONL, verify all 10 are parseable, have timestamps, and none are interleaved/corrupted.

---

### Deliverable 1.5: Function Ledger Store

**What:** Per-function notepad stored as a SQLite database. Claims are created with truth_level=NULL, then evaluated by the critic.

**Files:**
- `reaper/harness/ledger.py`
- Database at `reaper/data/ledger.db` per run

**Schema:**
```sql
CREATE TABLE functions (
    address TEXT PRIMARY KEY,       -- hex address
    llm_name TEXT,
    canon_name TEXT,
    summary TEXT                     -- latest purpose/summary
);

CREATE TABLE claims (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    function_address TEXT NOT NULL,
    claim_text TEXT NOT NULL,
    truth_level TEXT CHECK(truth_level IN
        ('speculation','inferred','low_confidence','mid_confidence','high_confidence')
        OR truth_level IS NULL),        -- NULL = not yet evaluated by critic
    submitted_by TEXT,               -- session_id of submitting agent
    reviewed_by TEXT,                -- session_id of critic (set on evaluation)
    created_at TEXT,
    updated_at TEXT,
    FOREIGN KEY (function_address) REFERENCES functions(address)
);

CREATE TABLE evidence_links (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    claim_id INTEGER NOT NULL,
    address_start TEXT NOT NULL,     -- hex start of evidence range
    address_end TEXT NOT NULL,       -- hex end of evidence range
    description TEXT,                -- what this evidence shows
    FOREIGN KEY (claim_id) REFERENCES claims(id) ON DELETE CASCADE
);
```

**Note:** Enable foreign keys on every connection: `PRAGMA foreign_keys = ON` (SQLite has them off by default).

**Interface:**
```python
class Ledger:
    def __init__(self, db_path: str, neo4j_driver):
        """Stores db_path and neo4j_driver only — NO I/O here.
        Caller MUST call await ledger.init() before any other method.
            ledger = Ledger(path, driver)
            await ledger.init()  # opens connection + creates tables"""

    async def init(self):
        """Open aiosqlite connection with WAL mode + PRAGMA foreign_keys=ON.
        Run CREATE TABLE IF NOT EXISTS for all three tables.
        Create self._write_lock = asyncio.Lock()."""

    async def register_functions_from_graph(self):
        """Query Neo4j for all :Function nodes, INSERT OR IGNORE into
        the functions table. Idempotent — safe to call multiple times
        (e.g., after type recovery creates new graph nodes).
        Acquires _write_lock."""

    async def get_function(self, address: str) -> dict | None
    async def set_function_names(self, address: str, llm_name: str, canon_name: str)
        """Acquires _write_lock."""
    async def set_function_summary(self, address: str, summary: str)
        """Set/update the summary column for a function. Acquires _write_lock."""
    async def add_claim(self, function_address: str, claim_text: str,
                        submitted_by: str, evidence: list[dict]) -> int
        """Insert claim with truth_level=NULL. Returns claim_id.
        Claim exists in DB immediately — critic evaluates it next.
        Acquires _write_lock."""
    async def set_truth_level(self, claim_id: int, truth_level: str, reviewed_by: str)
        """Acquires _write_lock."""
    async def update_claim_text(self, claim_id: int, new_text: str)
        """Update claim_text on an existing claim (used by resynthesis
        merge — keeps the claim ID stable while updating content).
        Acquires _write_lock."""
    async def delete_claim(self, claim_id: int)
        """Delete a superseded claim (truth_level=NULL, agent produced
        a replacement after critic rejection). evidence_links cascade.
        Acquires _write_lock."""
    async def get_claims(self, function_address: str) -> list[dict]
    async def get_evidence(self, claim_id: int) -> list[dict]
    async def all_functions_have_claims(self) -> bool
        """Every :Function in Neo4j has at least one claim with
        truth_level IS NOT NULL in the ledger.
        Queries Neo4j for function list, checks SQLite for coverage."""
```

**Test:** Add a function, add 3 claims (truth_level=NULL), verify `all_functions_have_claims()` returns false. Set truth levels on all 3, verify returns true. Add a function to Neo4j without a ledger entry, verify returns false.

---

### Deliverable 1.6: TODO Ledger

**What:** Task queue stored in SQLite. Tasks have descriptions, dependencies, status, and graph references.

**Files:**
- `reaper/harness/todo.py`
- Database at `reaper/data/todo.db` per run

**Schema:**
```sql
CREATE TABLE tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    description TEXT NOT NULL,
    status TEXT CHECK(status IN ('pending','in_progress','completed')) DEFAULT 'pending',
    context_spec TEXT,               -- JSON: what context to assemble (see below)
    start_position TEXT,             -- hex address or graph node to start at
    goal TEXT,                       -- what "done" looks like
    graph_refs TEXT,                 -- JSON array of node/edge IDs referenced
    assigned_to TEXT,                -- session_id
    critic_feedback TEXT,            -- feedback from last rejection (baked into re-queue)
    rejection_count INTEGER DEFAULT 0,
    created_at TEXT,
    completed_at TEXT
);

-- context_spec JSON keys (used by ContextAssembler.for_task()):
--   "functions": ["0x1400", "0x2000"]  — include these functions' HLIL
--   "include_callees": true/false      — include callee summaries
--   "include_claims": true/false       — include ledger claims
--   "include_neighborhood": true/false — include N-hop graph neighborhood

CREATE TABLE task_dependencies (
    task_id INTEGER NOT NULL,
    depends_on INTEGER NOT NULL,
    PRIMARY KEY (task_id, depends_on),
    FOREIGN KEY (task_id) REFERENCES tasks(id),
    FOREIGN KEY (depends_on) REFERENCES tasks(id)
);
```

**Note on rejection:** There is no `rejected` status. When critic rejects a task, it stays `pending` with `critic_feedback` populated and `rejection_count` incremented. `get_ready_tasks()` picks it up again. After `max_critic_rejections`, the harness force-completes it.

**Interface:**
```python
class TodoLedger:
    def __init__(self, db_path: str):
        """Like Ledger, needs async init:
            todo = TodoLedger(path)
            await todo.init()
        init() opens aiosqlite with WAL mode + PRAGMA foreign_keys=ON,
        runs CREATE TABLE IF NOT EXISTS, creates self._write_lock."""

    async def init(self):
        """Open aiosqlite connection, set PRAGMAs, create tables."""

    async def create_task(self, description: str, context_spec: dict,
                          start_position: str, goal: str,
                          graph_refs: list[str],
                          depends_on: list[int] = None) -> int
        """Acquires _write_lock."""
    async def get_task(self, task_id: int) -> dict | None
    async def get_ready_tasks(self) -> list[dict]    # pending with all deps completed
    async def assign_task(self, task_id: int, session_id: str)
        """Sets status to 'in_progress'. Acquires _write_lock."""
    async def complete_task(self, task_id: int)
        """Sets status to 'completed', records completed_at. Acquires _write_lock."""
    async def reject_task(self, task_id: int, feedback: str) -> int
        """Sets status back to 'pending', sets critic_feedback,
        increments rejection_count. Returns new count. Acquires _write_lock."""
    async def is_empty(self) -> bool
        """All tasks are 'completed'. No pending or in_progress."""
    async def has_in_progress(self) -> bool
        """Any tasks currently in_progress (not stuck, just working)."""
    async def delete_task(self, task_id: int)
        """For scheduler merging — remove a merged-away task. Acquires _write_lock."""
```

**Test:** Create 3 tasks, make task 3 depend on 1 and 2. Verify task 3 not in `get_ready_tasks()` until 1 and 2 completed. Test reject_task: verify it goes back to pending with feedback. Test get_task returns full task dict.

---

## Phase 2: HLIL Extraction + Graph Construction

Build the HLIL dataflow graph from a binary using Binja headless.

### Deliverable 2.1: HLIL Text Extractor

**What:** Utility that opens a binary in Binja 6.0 headless and returns HLIL decompilation as readable text. This is the bridge between binary addresses and what agents actually read.

**Files:**
- `reaper/tools/hlil_extract.py`

**Interface:**
```python
class HLILExtractor:
    def __init__(self, binary_path: str, data_dir: str):
        """Open binary in binaryninja.open_view() headless.
        Store the BinaryView as self.bv.
        Save BNDB to {data_dir}/target.bndb on first open.
        self.bndb_path = path to the saved BNDB.
        NOTE: the BinaryView is mutable. After type recovery
        (Phase 4), HLIL output for affected functions will change
        (raw offsets become named fields). This is correct behavior."""

    def get_function_hlil(self, func_address: str) -> str:
        """Return the full HLIL decompilation of one function as text.
        Iterates func.hlil.instructions (NOT func.hlil.root, which
        does not exist in Binja 6.0).
        Format: one HLIL instruction per line, with hex address prefix.
        Example output:
            0x1400: int64_t var_18
            0x1404: var_18 = malloc(0x100)
            0x140c: if (var_18 == 0)
            0x1410:     return -1
            0x1418: memset(var_18, 0, 0x100)
            0x1424: return var_18
        """

    def get_hlil_range(self, address_start: str, address_end: str) -> str:
        """Return HLIL text for a specific address range.
        Uses bv.get_functions_containing(int(address_start, 16)) to find
        the function. If range spans two functions, return both sections
        separated by a function header.
        Extracts only instructions whose address falls within [start, end].
        Used by critic to retrieve evidence."""

    def get_function_signature(self, func_address: str) -> str:
        """Return the function prototype as Binja renders it.
        Example: 'int64_t sub_1400(int64_t arg1, char* arg2)'"""

    def list_functions(self) -> list[dict]:
        """Return [{address: str, name: str, size: int}, ...] for all
        functions in bv.functions."""

    def get_string_refs(self, func_address: str) -> list[dict]:
        """Return [{address: str, value: str}, ...] for all string
        constant references within a function.
        Walk func.hlil.instructions, find HighLevelILConst/HighLevelILConstPtr
        that reference data sections, read the string at that address.
        (HighLevelILConstPtr = pointer-sized constant, used for addresses
        into the data section where strings live. NOT HighLevelILConstData,
        which is variable-sized inline data.)"""

    def get_variables(self, func_address: str) -> list[dict]:
        """Return [{name: str, type: str, source: str}, ...] for all
        variables in func.vars (Binja 6.0 property)."""

    def get_parameters(self, func_address: str) -> list[dict]:
        """Return [{name: str, type: str, index: int}, ...] for all
        parameters in func.parameter_vars."""
```

**Test:** Open stripped cJSON binary. `list_functions()` returns ~30 entries. `get_function_hlil()` on any function returns non-empty text with address prefixes. `get_hlil_range()` with a valid range returns a subset. `get_string_refs()` on `cJSON_Print`-equivalent returns at least one string.

---

### Deliverable 2.2: Graph Node Builder

**What:** Creates Function, Variable, Argument, Call, and StringRef nodes in Neo4j from Binja HLIL. Also creates structural `:CONTAINS` edges (ownership edges, not dataflow — they establish which function owns which nodes). `:REFS_STRING` edges are deferred to 2.3 (requires instruction walking).

**Files:**
- `reaper/tools/graph_nodes.py`

**Entry point:**
```python
async def build_nodes(extractor: HLILExtractor, neo4j_driver) -> None:
    """Module-level async function (not a class)."""
```

**Input:** `HLILExtractor` instance (from 2.1) + async Neo4j driver.
**Output:** Neo4j populated with nodes + structural ownership edges.

**Logic:**
1. For each function from `extractor.list_functions()`:
   - `MERGE (:Function {address: hex_addr})` with pinned=false, ambiguous=false
   - For each variable from `extractor.get_variables(func_addr)`:
     - `MERGE (:Variable {id: "{func_addr}:{var_name}"})` with address from var source
     - `MERGE` `:CONTAINS` edge from Function to Variable
   - For each parameter from `extractor.get_parameters(func_addr)`:
     - `MERGE (:Argument {id: "{func_addr}:arg{index}"})` with address=func_addr
     - `MERGE` `:CONTAINS` edge from Function to Argument
   - For each string ref from `extractor.get_string_refs(func_addr)`:
     - `MERGE (:StringRef {id: "str:{hex_addr}"})` with value=string_content
   - For each call instruction (walk `func.hlil.instructions`, find `HighLevelILCall`):
     - `MERGE (:Call {id: "{func_addr}:call_{instr_addr}"})` with address=instr_addr
     - If call target is unresolvable: set `ambiguous: true`
     - `MERGE` `:CONTAINS` edge from Function to Call

**Note:** Uses `MERGE` throughout for idempotency — safe to re-run after crash.

**Test:** Run on stripped cJSON. Verify:
- `MATCH (f:Function) RETURN count(f)` ≈ 30
- `MATCH (v:Variable) RETURN count(v)` > 0
- `MATCH (s:StringRef) RETURN count(s)` > 0 (cJSON has string literals)
- Every node has an `address` property
- Every Variable/Argument/Call has a `:CONTAINS` edge from its parent Function

---

### Deliverable 2.3: Graph Edge Builder (Dataflow)

**What:** Walks HLIL instructions and creates dataflow edges between existing nodes. Runs after node builder (2.2). Also creates `:REFS_STRING` edges (deferred from 2.2 because determining which variable references a string requires instruction walking).

**Files:**
- `reaper/tools/graph_edges.py`

**Entry point:**
```python
async def build_edges(extractor: HLILExtractor, neo4j_driver) -> None:
    """Module-level async function (not a class)."""
```

**Input:** `HLILExtractor` instance + populated Neo4j (from 2.2).
**Output:** Dataflow edges added to Neo4j.

**Logic — map each HLIL instruction type to edges:**

| HLIL instruction type | Action |
|---|---|
| `HighLevelILAssign` | `:DATAFLOW_ASSIGN` from source node(s) to destination Variable |
| `HighLevelILCall` | `:CALL` from Call node to target Function (if resolvable). `:DATAFLOW_ARG` from each argument's source to the Call's corresponding Argument |
| `HighLevelILRet` | `:RETURN` from the return expression's source to the Function node |
| `HighLevelILVar` / `HighLevelILConst` | Leaf expressions — no edges created, they ARE the sources |
| `HighLevelILDeref` / `HighLevelILStructField` | `:DATAFLOW_ASSIGN` following the deref chain |
| `HighLevelILAddressOf` | `:DATAFLOW_ASSIGN` — the address flows into whatever consumes it |
| `HighLevelILVarPhi` | `:DATAFLOW_ASSIGN` from each SSA source to the phi target |
| `HighLevelILIf` / `HighLevelILWhile` / `HighLevelILFor` / `HighLevelILDoWhile` / `HighLevelILSwitch` | **Skip** — control flow, no dataflow edges. BUT: recurse into the condition expression and body to find assignments/calls within them |
| `HighLevelILVarInit` | `:DATAFLOW_ASSIGN` from init expression to the declared variable (NOT `VarDeclare`, which has no initializer) |
| `HighLevelILArrayIndex` | `:DATAFLOW_ASSIGN` from base array variable to the indexed access |
| Arithmetic/comparison operators (`HighLevelILAdd`, `HighLevelILSub`, `HighLevelILCmpE`, etc.) | Sub-expressions — walked depth-first as part of the parent instruction. No edges of their own; they contribute to the parent's dataflow |

**Walk strategy:** For each function, iterate `func.hlil.instructions`. For each instruction, `match` on `instr.operation` (the `HighLevelILOperation` enum). Recursively walk operands — `instr.operands` gives child expressions. Use `MERGE` for idempotency.

**StringRef linking:** When walking instructions, if a `HighLevelILConst` or `HighLevelILConstPtr` value matches a known StringRef node address, create `:REFS_STRING` from the containing variable to that StringRef.

**Test:** Run on cJSON after 2.2. Verify:
- `MATCH ()-[e:DATAFLOW_ASSIGN]->() RETURN count(e)` > 0
- `MATCH ()-[e:CALL]->() RETURN count(e)` > 0 (cJSON functions call each other)
- `MATCH (c:Call {ambiguous: true}) RETURN count(c)` — should be small
- Pick `cJSON_Parse` (or its sub_XXXX), manually verify 2-3 edges against its HLIL

---

### Deliverable 2.4: Symbol Preservation (Pass -1)

**What:** Before any LLM pass, identify and pin known symbols.

**Files:**
- `reaper/tools/pin_symbols.py`

**Entry point:**
```python
async def pin_symbols(extractor: HLILExtractor, neo4j_driver) -> None:
    """Module-level async function (not a class)."""
```

**Logic:**
1. From the Binja BinaryView (`extractor.bv`), collect:
   - Import table: `bv.get_symbols_of_type(SymbolType.ImportedFunctionSymbol)`
   - Debug symbols: `SymbolType.FunctionSymbol` with names that don't start with `sub_`
   - Signature library matches: `bv.get_symbols_of_type(SymbolType.LibraryFunctionSymbol)`
2. For each known symbol, update the Neo4j Function node:
   - `SET f.pinned = true, f.llm_name = name, f.canon_name = name`
3. StringRef nodes are inherently pinned — set `pinned: true` on all of them

**Note:** The test binary must be **dynamically linked** so that import symbols exist. `build.sh` (8.1) compiles with dynamic linking by default (no `-static`).

**Test:** Run on stripped cJSON. Verify `malloc`, `free`, `strlen`, `printf` etc. are pinned. `MATCH (n {pinned: true}) RETURN count(n)` > 0.

---

### Deliverable 2.5: Leaf Validation + SCC Detection + Traversal Order

**What:** Post-graph-construction validation and cycle analysis.

**Files:**
- `reaper/tools/graph_analysis.py`

**Entry point:**
```python
async def validate_and_order(neo4j_driver) -> None:
    """Module-level async function (not a class).
    Runs all three steps below. Also exported from this file:
    compute_resynthesis_groups() (see 7.4)."""
```

**Logic:**
1. **Leaf validation:** Query all graph-level leaf nodes (nodes with no incoming DATAFLOW/CALL/RETURN edges — only outgoing or `:CONTAINS`). Assert they are all `:Variable`, `:Argument`, or `:StringRef`, never `:Function` or `:Call`. If a Function has no callers, no internal variables, and no calls (dead/empty function), set `isolated: true` but don't fail.

2. **SCC detection:** Implemented **in Python** (not Cypher — no GDS plugin dependency). Pull all Function addresses and `:CALL` edges into memory as an adjacency list. Run Tarjan's algorithm. For cJSON (~30 functions), this is trivial. For larger binaries (1000+ functions), still fast — Tarjan's is O(V+E). For each SCC with >1 member:
   - Set `scc_id` on all member Function nodes in Neo4j
   - For back-edges: pick the edge from highest to lowest address, relabel as `:DEFERRED_BACK`

3. **Traversal order — function-level only:**
   - Condensed DAG: collapse each SCC to one supernode
   - Topological sort the condensed DAG (reverse postorder)
   - Leaf functions (functions whose `:CALL` edges ALL lead to pinned/imported functions or to functions outside the binary) get `traversal_order = 0`
   - Higher levels get incrementing integers
   - Within a function, the processing order is hardcoded in dispatchers: Variables → Arguments → Function summary. NOT stored in Neo4j

**Test:** On cJSON: all leaf nodes are Variable/Argument/StringRef. `MATCH (f:Function) WHERE f.traversal_order IS NULL RETURN count(f)` = 0. On a test binary with `void a() { b(); } void b() { a(); }`: SCC detected, one `:DEFERRED_BACK` exists, both functions have the same `scc_id`.

---

## Phase 3: Context Assembly + Harness Core

### Deliverable 3.1: Context Assembler

**What:** Takes graph/ledger state and produces formatted text for agent prompts.

**Files:**
- `reaper/harness/context.py`

**Interface:**
```python
class ContextAssembler:
    def __init__(self, extractor: HLILExtractor, neo4j_driver,
                 ledger: Ledger, config: dict):
        pass

    async def for_function(self, func_address: str,
                           include_callees: bool = False,
                           include_callers: bool = False,
                           include_claims: bool = False) -> str:
        """Assemble context for a function. Returns formatted text.
        Always includes: function signature, full HLIL, variables/params,
        and string references (via extractor.get_string_refs()).
        Optional sections controlled by flags."""

    async def for_variable(self, func_address: str, var_id: str,
                           include_callee_renames: bool = False) -> str:
        """Assemble context for renaming a specific variable.
        Returns: function HLIL with the target variable marked as
        >>> var_18 <<< (triple angle brackets around every occurrence).
        Plus string refs and pinned symbol context.
        If include_callee_renames=True: appends a section listing
        callee functions' current llm_name/canon_name from Neo4j
        (used for non-leaf renames where callee context matters)."""

    async def for_evidence(self, evidence_links: list[dict]) -> str:
        """Retrieve HLIL text for evidence address ranges.
        Each evidence_link has address_start, address_end, description.
        Returns formatted sections."""

    async def for_struct_candidate(self, candidate) -> str:
        """Assemble context for type recovery.
        Uses self.extractor to retrieve HLIL for each FieldAccess
        in the candidate (single instruction, not a range).
        Groups by function for readability."""

    async def for_task(self, task: dict) -> str:
        """Assemble context for an investigation task.
        Reads task['context_spec'] JSON (keys documented in 1.6 schema).
        Combines function HLIL, callees, claims as specified."""

    async def for_subgraph(self, function_addresses: list[str],
                           include_claims: bool = True) -> str:
        """Assemble context for a group of related functions (resynthesis).
        Returns all functions' HLIL + all claims.
        Claims are formatted with their IDs so the resynthesis agent
        can reference them in Contradiction/MergedClaim output:
            [claim_id=1] This function parses JSON. (mid_confidence)
            [claim_id=2] First argument is null-terminated. (high_confidence)
        If the assembled context exceeds max_context_tokens (from config,
        default 32768), truncate by: (1) dropping claims below mid_confidence,
        (2) summarizing HLIL to signature + first/last 10 lines per function,
        (3) if still over, drop the least-connected functions from the group.
        Log a warning on any truncation."""
```

**Variable highlighting format (for `for_variable`):**
```
HLIL of function sub_1400:
  0x1400: int64_t >>> var_18 <<<
  0x1404: >>> var_18 <<< = malloc(0x100)
  0x140c: if (>>> var_18 <<< == 0)
  0x1410:     return -1
  0x1418: memset(>>> var_18 <<<, 0, 0x100)
  0x1424: return >>> var_18 <<<

TARGET: Rename the variable `var_18` highlighted with >>> <<< above.
```

**Test:** Populate Neo4j with cJSON functions + some renames + some claims. Call `for_function()` with all flags — verify output contains HLIL, names, claims, callee info. Call `for_variable()` — verify target variable is highlighted with `>>>` markers. Call `for_struct_candidate()` — verify HLIL snippets for each access.

---

### Deliverable 3.2: Shadow Copy Manager

**What:** Checkout/checkin mechanism for agent working state.

**Files:**
- `reaper/harness/shadow.py`

**Interface:**
```python
class ShadowCopyManager:
    def __init__(self, neo4j_driver, ledger: Ledger, config: dict):
        """config provides shadow_copy_hops (default 2).
        Maintains an internal monotonic version counter (in-memory int,
        incremented on every apply())."""

    async def checkout(self, func_address: str, session_id: str) -> dict:
        """Checkout function + N-hop neighborhood via `:CALL`/`:CONTAINS` edges.
        Returns snapshot dict with nodes, edges, claims, checkout_version."""

    async def diff(self, session_id: str, modified: dict) -> dict:
        """Compare modified snapshot against current master state.
        Queries Neo4j for current master node properties.
        Returns {mutations: [...], conflicts: [...]}.
        Conflict = master version > checkout version AND master value
        differs from checkout value for a field the agent changed."""

    async def apply(self, session_id: str, mutations: list[dict]):
        """Apply mutations to Neo4j first, then SQLite ledger.
        Increments version counter.
        If Neo4j write succeeds but SQLite fails: log the inconsistency,
        do NOT rollback Neo4j (accepted limitation — see arch decisions)."""

    def discard(self, session_id: str):
        """Discard a checkout."""
```

**Test:** Checkout function, rename variable in copy, `diff()` shows rename, `apply()` writes to master. Then: two checkouts of same function, both rename same variable, first applies, second `diff()` shows conflict.

---

### Deliverable 3.3: Submission Protocol

**What:** Pydantic models for all agent I/O. These double as vLLM structured output schemas.

**Files:**
- `reaper/harness/submission.py`

**Models:**
```python
from pydantic import BaseModel
from typing import Literal

TRUTH_LEVELS = Literal['speculation', 'inferred', 'low_confidence',
                        'mid_confidence', 'high_confidence']

class Rename(BaseModel):
    node_id: str              # Neo4j node id (e.g., "0x1400:var_18")
    llm_name: str             # verbose pothole_case
    canon_name: str           # human-readable
    justification: str        # free-text reasoning

class EvidenceLink(BaseModel):
    address_start: str        # hex
    address_end: str          # hex
    description: str          # what this range shows

class Claim(BaseModel):
    function_address: str
    claim_text: str
    evidence: list[EvidenceLink]

class Submission(BaseModel):
    renames: list[Rename] = []
    claims: list[Claim] = []

class CriticVerdict(BaseModel):
    truth_level: TRUTH_LEVELS  # only used when accepted=True; ignored on rejection
    accepted: bool
    feedback: str

class TaskContextSpec(BaseModel):
    """Typed context specification — avoids bare dict in structured output."""
    functions: list[str] = []           # hex addresses to include
    include_callees: bool = False
    include_claims: bool = False
    include_neighborhood: bool = False

class TaskSpec(BaseModel):
    """Used by both review agent and investigation agent to create tasks.
    Maps directly to TodoLedger.create_task() parameters."""
    description: str
    start_position: str       # hex address
    goal: str
    graph_refs: list[str] = []
    context_spec: TaskContextSpec = TaskContextSpec()

class ReviewOutput(BaseModel):
    """Review agent output — renames + claims + tasks in one structured response."""
    renames: list[Rename] = []
    claims: list[Claim] = []
    tasks: list[TaskSpec] = []

class InvestigationResult(BaseModel):
    answer: str
    claims: list[Claim] = []
    subtasks: list[TaskSpec] = []  # rich task specs, not bare strings

class Contradiction(BaseModel):
    claim_id_a: int
    claim_id_b: int
    explanation: str

class MergedClaim(BaseModel):
    keep_id: int
    remove_ids: list[int]
    merged_text: str

class StructField(BaseModel):
    offset: int
    name: str
    type_str: str             # C type string, e.g. "struct cJSON*"
    size: int
    confidence: TRUTH_LEVELS

class StructDefinition(BaseModel):
    struct_name: str
    fields: list[StructField]

class CriticOutcome(BaseModel):
    """Returned by harness evaluate_claim() to caller (not an LLM output)."""
    accepted: bool
    feedback: str = ""

class FunctionSummary(BaseModel):
    """Pass 1 function-level output — name + summary."""
    llm_name: str
    canon_name: str
    summary: str              # one-paragraph purpose description

class ResynthesisResult(BaseModel):
    contradictions: list[Contradiction] = []
    merged_claims: list[MergedClaim] = []
    new_tasks: list[TaskSpec] = []
```

**Interface:**
```python
def get_schema(model_class) -> dict:
    """Return model_class.model_json_schema() for vLLM structured_output."""

def parse_response(model_class, text: str):
    """Parse JSON text into the given Pydantic model.
    Raises ValidationError if malformed."""
```

**Test:** Roundtrip every model: construct, `model_dump_json()`, `model_validate_json()`. Verify `CriticVerdict` rejects `truth_level="banana"`. Verify `get_schema(Submission)` is valid JSON Schema.

---

### Deliverable 3.4: BNDB Writeback

**What:** Write renames and types back to the Binja BNDB file.

**Files:**
- `reaper/tools/bndb_writer.py`

**Interface:**
```python
class BNDBWriter:
    def __init__(self, extractor: HLILExtractor):
        """Reuse the HLILExtractor's BinaryView — don't reopen.
        Creates self._rename_map: dict[str, str] — maps node_id to
        the name Binja CURRENTLY has for that variable. Initialized
        lazily on first lookup (from node_id suffix = original name).
        Updated on every successful rename."""

    def rename_function(self, address: int, llm_name: str, canon_name: str):
        """func.name = canon_name.
        LLM name stored via tag: bv.create_tag_type('reaper_llm', '🏷')
        then func.create_user_address_tag(addr, tag_type, llm_name)."""

    def rename_variable(self, func_address: int, var_node_id: str,
                        llm_name: str, canon_name: str):
        """Identify variable by node_id (e.g., '0x1400:var_18').
        Lookup chain:
          1. Check _rename_map[var_node_id] — if present, that's the name
             Binja currently has (may differ from node_id suffix after
             a prior rename).
          2. If not in map, parse original name from node_id suffix.
        Look up in func.vars by that name. Set var.name = canon_name.
        Update _rename_map[var_node_id] = canon_name.
        Store llm_name as tag.
        NOTE: node_id is STABLE (never changes). _rename_map tracks
        what Binja currently calls each variable, so second and
        subsequent renames of the same variable work correctly."""

    def set_type(self, address: int, type_str: str):
        """bv.parse_type_string(type_str) then apply to the variable
        or function signature at that address."""

    def save(self):
        """bv.save(bndb_path). Call after batch operations."""
```

**Key:** Node IDs in Neo4j are stable (based on original var names from initial graph construction). Renames change `canon_name` and `llm_name` properties on the node but NOT the node's `id`. The BNDB writer maintains `_rename_map` to track what Binja currently calls each variable — this is essential because after a first rename, the original name from the node_id suffix no longer matches Binja's state. Without the map, a second rename of the same variable would fail to find it.

**Test:** Open stripped cJSON, rename a function and variable, save, reopen with fresh HLILExtractor, verify names persisted.

---

### Deliverable 3.5: Merge Agent

**What:** Handles concurrent submission conflicts via LLM-based resolution.

**Files:**
- `reaper/harness/merge_agent.py`
- `reaper/agents/prompts/merge_agent.txt`

**Interface:**
```python
class MergeAgent:
    def __init__(self, llm_client, shadow_mgr, bndb_writer, ledger, todo, config):
        pass

    async def attempt_merge(self, session_id: str,
                            submission: Submission) -> MergeResult:
        """Merge only the ACCEPTED subset of a submission (renames with
        critic-approved claims). Rejected claims are not included.
        1. shadow_mgr.diff() on the accepted mutations
        2. No conflicts → apply → MergeResult(status='applied')
        3. Conflicts → LLM resolves or rejects → new TODO tasks
        On successful apply: for each rename in the submission,
        call bndb_writer.rename_function() or rename_variable()
        so the BinaryView stays in sync with Neo4j."""
```

**MergeResult:**
```python
class MergeResult(BaseModel):
    status: Literal['applied', 'resolved', 'rejected']
    conflicts_resolved: list[dict] = []
    new_tasks: list[int] = []
```

**Test:** Two sequential checkouts of same function, different renames. First applies clean. Second detects conflict and triggers LLM resolution. (Test uses sequential simulation — no actual concurrency needed.)

---

## Phase 4: Type Recovery (Pass 0)

### Deliverable 4.1: Struct Access Pattern Detector

**What:** Scan HLIL for pointer+offset patterns that indicate struct field accesses. Pure analysis — no LLM.

**Files:**
- `reaper/tools/struct_detector.py`

**Interface:**
```python
class StructAccessDetector:
    def __init__(self, extractor: HLILExtractor):
        pass

    def find_struct_accesses(self) -> list[StructCandidate]:
        """Walk all functions' HLIL for patterns:
        - *(base + offset)  — HighLevelILDeref of HighLevelILAdd
        - base->field       — HighLevelILStructField
        - *(base + N) where N is a constant
        Grouping strategy:
          WITHIN a function: group by base variable (same base = same struct)
          ACROSS functions: group by Binja's type inference on the base
            pointer. If Binja assigns the same type to arg1 of func_A and
            arg1 of func_B, their accesses are in the same candidate.
            If Binja can't determine the type, DON'T cross-function group —
            leave them as separate candidates. False negatives are acceptable;
            false positives (wrong grouping) are worse.
        """

class StructCandidate(BaseModel):
    """Defined in struct_detector.py — imported by type_recovery.py and context.py."""
    candidate_id: str             # unique identifier
    base_type_hint: str           # Binja's current type guess
    accesses: list[FieldAccess]
    functions_involved: list[str] # hex addresses

class FieldAccess(BaseModel):
    """Defined in struct_detector.py alongside StructCandidate."""
    offset: int
    size: int
    access_type: str              # 'read' or 'write'
    function_address: str
    instruction_address: str      # hex — single instruction, not a range
```

**Test:** Run on cJSON. At least one StructCandidate with multiple FieldAccess entries at different offsets.

---

### Deliverable 4.2: Type Recovery Agent

**What:** Takes struct candidates, infers layouts via LLM.

**Files:**
- `reaper/agents/type_recovery.py`
- `reaper/agents/prompts/type_recovery.txt`

**Input:** One StructCandidate + HLIL context from `ContextAssembler.for_struct_candidate()`.

**Interface:**
```python
class TypeRecoveryAgent:
    def __init__(self, llm_client, context_asm, config):
        pass

    async def run(self, candidate: StructCandidate) -> StructDefinition | None:
        """Assembles context, calls LLM, returns parsed StructDefinition
        or None if the LLM determines no struct exists."""
```

**Output (structured):** `StructDefinition` from `submission.py` (3.3).

**Logic:**
1. For each StructCandidate from 4.1:
   - `context = await context_asm.for_struct_candidate(candidate)`
   - `response = await llm.send(session, context, thinking_level="minimal", structured_output=get_schema(StructDefinition))`
   - Parse into StructDefinition
   - Create a claim in the ledger for each struct (truth_level=NULL, to be evaluated later when critic is available; for first implementation, set to 'inferred' directly)

**Test:** Run on cJSON struct candidates. Output includes a struct with >=3 fields at correct offsets.

---

### Deliverable 4.3: Graph Rebuild on Type Recovery

**What:** Apply struct to Binja, rebuild affected graph regions, re-validate.

**Files:**
- `reaper/tools/graph_rebuild.py`

**Interface:**
```python
class GraphRebuilder:
    def __init__(self, extractor: HLILExtractor, bndb_writer: BNDBWriter,
                 neo4j_driver):
        pass

    async def apply_struct(self, struct_def: StructDefinition) -> list[str]:
        """
        1. Build C struct string from StructDefinition
        2. Apply to Binja via bndb_writer.set_type()
        3. Re-extract HLIL for affected functions (Binja now shows field names)
        4. For each affected function:
           - Delete old Variable nodes that were raw offset accesses
           - Create new Variable nodes for each struct field access
           - Create :FIELD_OF edges
           - Rebuild dataflow edges for affected instructions
        5. Re-run traversal order computation via validate_and_order(neo4j_driver)
           — new leaf nodes may change the topological ordering
        6. Return list of affected function addresses
        NOTE: Caller is responsible for re-registering functions in the ledger
        after all structs are applied (see pipeline runner — calls
        ledger.register_functions_from_graph() once after the type recovery loop).
        """
```

**Test:** Apply cJSON struct definition. HLIL shows `->next`, `->type` etc. New `:FIELD_OF` edges exist. Leaf validation passes. Traversal order is recomputed.

---

## Phase 5: Initial Sweep (Pass 1)

### Deliverable 5.1: Variable Rename Agent (Leaf Level)

**What:** Agent that renames one variable from local HLIL context. Minimal thinking.

**Files:**
- `reaper/agents/rename_variable.py`
- `reaper/agents/prompts/rename_variable.txt`

**Interface:**
```python
class RenameVariableAgent:
    def __init__(self, llm_client, context_asm, tracer, config):
        pass

    async def run(self, func_address: str, var_id: str,
                  include_callee_renames: bool = False) -> Submission:
        """Assemble context via context_asm.for_variable(), call LLM
        with structured_output=get_schema(Submission), return parsed
        Submission with one Rename entry.
        Uses thinking_level from config['thinking_levels']['pass1_rename']."""
```

**Input:** `func_address` + `var_id` (Neo4j node id, e.g. `"0x1400:var_18"` or `"0x1400:arg0"`)
**Output:** `Submission` with one `Rename` entry (via structured output).

**Test:** Variable assigned from `malloc()` → name contains "alloc"/"buffer"/"ptr" or similar.

---

### Deliverable 5.2: Bottom-Up Traversal Dispatcher (Pass 1)

**What:** Walks functions in traversal order. Within each function: variables → arguments → function summary.

**Files:**
- `reaper/harness/pass1_dispatcher.py`
- `reaper/agents/prompts/function_summary.txt` — system prompt for the function summary LLM call (step f below)

**Interface:**
```python
class Pass1Dispatcher:
    def __init__(self, llm_client, neo4j_driver, context_asm,
                 bndb_writer, ledger, tracer, config):
        pass

    async def run(self):
        """Execute full Pass 1 sweep."""
```

**Logic:**
1. Query: `MATCH (f:Function) RETURN f ORDER BY f.traversal_order ASC`
2. Group by `traversal_order` level
3. For each level (ascending):
   - For each function at this level, **in parallel** (asyncio.Semaphore, up to `max_concurrent_agents`):
     a. Query non-pinned `:Variable` nodes via `:CONTAINS`
     b. **SEQUENTIAL within each function:** For each variable: `await rename_agent(context_asm.for_variable(...))` → get Submission. Do NOT parallelize variable renames within the same function — each rename immediately updates Neo4j, and a concurrent rename in the same function would race
     c. Apply renames to Neo4j + BNDB immediately (**no critic in Pass 1** — speed)
     d. Query non-pinned `:Argument` nodes — rename with `context_asm.for_variable(addr, arg_id, include_callee_renames=True)` (callee context from lower levels)
     e. Apply argument renames
     f. Function-summary rename: same LLM call pattern as variable rename, but with `context_asm.for_function(addr, include_callees=True)` context and `structured_output=get_schema(FunctionSummary)`. Agent sees full HLIL with renamed vars/args + callee names → outputs `FunctionSummary` (llm_name, canon_name, summary)
     g. Apply: `bndb_writer.rename_function()`, `ledger.set_function_names()`, `ledger.set_function_summary()`
   - **Await all functions at this level before next level** — upper levels need lower-level renames as context
4. **SCC second pass:** After step 3 completes, query all functions with `scc_id IS NOT NULL`. Group by `scc_id`. For each SCC group: re-run steps (d)-(g) only (argument renames + function summary) — variables are leaves and already grounded, but arguments and function summaries can now incorporate context from SCC peers that were processed in step 3. This is one extra pass per SCC, not an unbounded fixpoint
5. `bndb_writer.save()`

**Note:** Pass 1 produces renames and summaries, NOT claims. `all_functions_have_claims()` will still return false after Pass 1. Claims come from Pass 2.

**Test:** Run on cJSON. `MATCH (n) WHERE n.pinned = false AND n.llm_name IS NULL RETURN count(n)` = 0. Every Function has a summary in the ledger. BNDB has canon_names.

---

## Phase 6: Deep Review (Pass 2) + Critic Loop

### Deliverable 6.1: Critic Agent

**What:** Evaluates claims against evidence. Build this FIRST — both Pass 2 and investigation depend on it.

**Files:**
- `reaper/agents/critic_agent.py`
- `reaper/agents/prompts/critic_agent.txt`

**Input:** Claim (already in ledger with truth_level=NULL) + `ContextAssembler.for_evidence(claim.evidence)` + function context.
**Output:** `CriticVerdict` (structured output — truth_level is Literal, not free string).

**Shared harness class — `CriticEvaluator` in `reaper/agents/critic_agent.py`:**

Both Pass2Dispatcher and InvestigationLoop instantiate a `CriticEvaluator`
to wrap the LLM-based critic with rejection tracking. One instance per
dispatcher, NOT shared across dispatchers.

Rejection count is tracked per (function_address, agent_role) in the evaluator's
in-memory dict `_rejection_counts: dict[tuple[str, str], int]` — NOT on the
claim row. This avoids the orphan problem: when an agent retries after rejection,
it may produce a completely different claim. The old claim (truth_level=NULL)
is deleted via `ledger.delete_claim()`, and the new claim gets a fresh row.
The evaluator's counter persists across retries.

```python
class CriticEvaluator:
    def __init__(self, llm_client, context_asm, ledger, tracer, config):
        self._rejection_counts: dict[tuple[str, str], int] = {}

    async def evaluate_claim(self, claim_id: int, claim: Claim,
                             func_addr: str, agent_role: str) -> CriticOutcome:
    verdict = await critic_session(claim, evidence_context)
    if verdict.accepted:
        await ledger.set_truth_level(claim_id, verdict.truth_level, critic_session_id)
        self._rejection_counts.pop((func_addr, agent_role), None)
    else:
        key = (func_addr, agent_role)
        self._rejection_counts[key] = self._rejection_counts.get(key, 0) + 1
        if self._rejection_counts[key] >= config['limits']['max_critic_rejections']:
            await ledger.set_truth_level(claim_id, 'speculation', critic_session_id)
            self._rejection_counts.pop(key, None)
        else:
            # Delete the rejected claim — agent will produce a replacement
            await ledger.delete_claim(claim_id)
            # Requeue: caller re-invokes the original agent with feedback
            # baked into the context. The calling dispatcher (Pass 2 or
            # investigation loop) handles this — it appends
            # f"\n\nPRIOR ATTEMPT REJECTED:\n{verdict.feedback}"
            # to the context string and re-calls the agent. The agent
            # produces a new Submission/InvestigationResult, which
            # creates a new claim row and re-enters this critic flow.
            return CriticOutcome(accepted=False, feedback=verdict.feedback)
```

**Test:** Well-evidenced claim → mid_confidence or higher. Vague claim → speculation or rejection with feedback.

---

### Deliverable 6.2: Review Agent

**What:** Evaluates Pass 1 labels, produces claims and TODO tasks.

**Files:**
- `reaper/agents/review_agent.py`
- `reaper/agents/prompts/review_agent.txt`

**Input:** `ContextAssembler.for_function(addr, include_callees=True, include_claims=True)`
**Output:** `ReviewOutput` (structured) — renames + claims + task specs (rich `TaskSpec` objects, not bare strings).

**Test:** Mislabel a variable. Verify review agent flags it with a corrective claim or TODO task with concrete goal + start_position.

---

### Deliverable 6.3: Pass 2 Dispatcher

**What:** Orchestrates review → critic → merge for all functions.

**Files:**
- `reaper/harness/pass2_dispatcher.py`

**Interface:**
```python
class Pass2Dispatcher:
    def __init__(self, llm_client, neo4j_driver, context_asm, shadow_mgr,
                 merge_agent, bndb_writer, ledger, todo, tracer, config):
        pass

    async def run(self):
        """Execute full Pass 2 review sweep."""
```

**Logic:**
1. Walk functions by `traversal_order` ascending
2. Group by level for parallelism
3. For each function, in parallel:
   a. Review agent reads master graph/ledger directly (read-only — no shadow checkout needed for review)
   b. Review agent → `ReviewOutput`
   c. For each claim: insert into ledger (truth_level=NULL) → critic evaluates → accept/reject
   d. For each TaskSpec in review output: critic reviews the task (is it atomic? goal concrete?) → create in TodoLedger
   e. Collect accepted renames only → `merge_agent.attempt_merge(accepted_submission)` (shadow checkout/diff/apply happens inside merge agent)
4. Wait for level before advancing

**Test:** Run on cJSON. Every function has >=1 claim with truth_level. TODO ledger has tasks.

---

## Phase 7: Investigation & Resynthesis

### Deliverable 7.1: Scheduler Agent

**What:** Reviews TODO ledger, merges overlapping tasks, decomposes large ones.

**Files:**
- `reaper/harness/scheduler.py`

**Interface:**
```python
class Scheduler:
    def __init__(self, llm_client, todo, context_asm, tracer, config):
        pass

    async def review_pending_tasks(self): ...
    async def run_once(self) -> list[dict]: ...
```

**Logic:**
1. `review_pending_tasks()`: Batch pending task descriptions into LLM call(s). If >20 pending tasks, chunk into groups of 20 to avoid context overflow. Ask for merge/decompose recommendations.
2. `run_once()`: `get_ready_tasks()` → assign each (sets status to `in_progress`) → return list of assigned task dicts. Does NOT run agents — caller (InvestigationLoop) handles execution.
3. Stale task check: tasks in_progress past `task_timeout_seconds` → reset to pending.

**Test:** Two overlapping tasks → merged. One vague task → decomposed.

---

### Deliverable 7.2: Investigation Agent

**What:** Executes a single TODO task.

**Files:**
- `reaper/agents/investigation_agent.py`
- `reaper/agents/prompts/investigation_agent.txt`

**Input:** Task from TodoLedger + `ContextAssembler.for_task(task)`
**Output:** `InvestigationResult` (structured) — answer + claims + subtasks (rich `TaskSpec`, not bare strings).

**Interface:**
```python
class InvestigationAgent:
    def __init__(self, llm_client, context_asm, ledger, todo, tracer, config):
        pass

    async def run(self, task: dict) -> InvestigationResult:
        """Assembles context via for_task(), calls LLM, returns result."""
```

**Completion flow (harness-side):**
1. Agent → `InvestigationResult`
2. Each claim → insert in ledger → critic flow
3. Each subtask → `todo.create_task()` from the `TaskSpec`, with dependency on current task
4. If subtasks created: current task back to `pending` (depends on new subtasks)
5. If no subtasks + claims accepted: `todo.complete_task()`

**Test:** Task "determine whether the second argument of cJSON_Parse can be NULL" → concrete answer + claim with evidence.

---

### Deliverable 7.3: Investigation Loop Runner

**What:** Runs scheduler + investigation until TODO drains or gets stuck.

**Files:**
- `reaper/harness/investigation_loop.py`

**Interface:**
```python
class InvestigationLoop:
    def __init__(self, scheduler, inv_agent, llm_client, context_asm,
                 todo, ledger, tracer, config):
        """Creates an internal CriticEvaluator(llm_client, context_asm,
        ledger, tracer, config) for _handle_result critic flow."""
        pass
```

**Logic:**
```python
async def run(self) -> bool:
    while True:
        await self.scheduler.review_pending_tasks()
        tasks = await self.scheduler.run_once()  # returns assigned task dicts
        if tasks:
            sem = asyncio.Semaphore(self.config['limits']['max_concurrent_agents'])
            async def execute(task):
                async with sem:
                    result = await self.inv_agent.run(task)
                    # Harness-side completion flow (see 7.2):
                    # claims → critic, subtasks → todo, complete/requeue
                    await self._handle_result(task, result)
            await asyncio.gather(*(execute(t) for t in tasks))
        if await self.todo.is_empty():
            return True   # all tasks completed
        if tasks or await self.todo.has_in_progress():
            continue      # progress being made
        # No dispatched AND nothing in progress → stuck
        await self.tracer.log("investigation_stuck", "inv_loop", {})
        return False      # partial completion
```

**Test:** 5 tasks (3 independent, 2 with deps). Loop runs, all complete. Loop terminates.

---

### Deliverable 7.4: Resynthesis Agent

**What:** Reviews evidence landscape for contradictions and patterns.

**Files:**
- `reaper/agents/resynthesis_agent.py`
- `reaper/agents/prompts/resynthesis_agent.txt`

**Input:** `ContextAssembler.for_subgraph(function_addresses)`
**Output:** `ResynthesisResult` (structured — Contradiction, MergedClaim, TaskSpec are all Pydantic models).

**Interface:**
```python
class ResynthesisAgent:
    def __init__(self, llm_client, context_asm, ledger, todo, tracer, config):
        pass

    async def run(self, context: str) -> ResynthesisResult:
        """Single LLM call with for_subgraph context → ResynthesisResult."""
```

**Scope groups (computed by `compute_resynthesis_groups()` in `reaper/tools/graph_analysis.py`):**

```python
async def compute_resynthesis_groups(neo4j_driver) -> list[list[str]]:
    """Returns groups of function addresses for resynthesis review.
    Each group is processed as one resynthesis unit."""
    groups = []

    # 1. Each SCC (functions sharing an scc_id)
    # MATCH (f:Function) WHERE f.scc_id IS NOT NULL
    # RETURN f.scc_id, collect(f.address)

    # 2. Each struct's consumers (functions using the same struct type)
    # MATCH (f:Function)-[:CONTAINS]->(:Variable)-[:FIELD_OF]->(s)
    # RETURN s.id, collect(DISTINCT f.address)

    # 3. Caller/callee clusters (direct call neighborhoods, depth 1)
    # MATCH (caller:Function)-[:CONTAINS]->(:Call)-[:CALL]->(callee:Function)
    # WHERE NOT callee.pinned
    # RETURN caller.address, collect(DISTINCT callee.address)
    # Merge caller into its callee list as one group.

    # Deduplicate: if a function appears in multiple groups,
    # merge those groups into one. Use union-find.
    return deduplicated_groups
```

**Test:** Two contradictory claims → flagged. Two duplicate claims → merged.

---

### Deliverable 7.5: Resynthesis Loop + Completion Check

**What:** Runs resynthesis → investigation cycles with a cap.

**Files:**
- `reaper/harness/completion.py`

**Interface:**
```python
class ResynthesisLoop:
    def __init__(self, resynth_agent, inv_loop, todo, ledger,
                 neo4j_driver, context_asm, tracer, config):
        pass
```

**Logic:**
```python
async def run(self) -> bool:
    for iteration in range(self.config['limits']['max_resynthesis_iterations']):
        groups = await compute_resynthesis_groups(self.neo4j_driver)
        new_task_count = 0
        # Groups are independent — process sequentially for v1 simplicity.
        # Future: parallelize with asyncio.gather + Semaphore like Pass 1/2.
        for group_addrs in groups:
            context = await self.context_asm.for_subgraph(group_addrs)
            result = await self.resynth_agent.run(context)
            for task_spec in result.new_tasks:
                await self.todo.create_task(**task_spec.model_dump())
                new_task_count += 1
            for mc in result.merged_claims:
                await self.ledger.update_claim_text(mc.keep_id, mc.merged_text)
                for rid in mc.remove_ids:
                    await self.ledger.delete_claim(rid)
        if new_task_count == 0:
            break  # stable
        await self.inv_loop.run()
    await self.ledger.register_functions_from_graph()
    return await is_re_complete(self.ledger, self.todo)

async def is_re_complete(ledger, todo) -> bool:
    return await ledger.all_functions_have_claims() and await todo.is_empty()
```

**Test:** Resynthesis finds contradiction → 1 task → investigation resolves → second pass clean → done. Verify terminates within max_resynthesis_iterations.

---

## Phase 8: Test Target + Integration

### Deliverable 8.1: Build Stripped cJSON Test Binary

**What:** Compile cJSON, strip it, extract ground truth via Binja headless.

**Files:**
- `reaper/eval/cjson/build.sh`
- `reaper/eval/cjson/extract_ground_truth.py`
- `reaper/eval/cjson/cjson_test` (stripped, gitignored)
- `reaper/eval/cjson/cjson_test_symbols` (unstripped, gitignored)
- `reaper/eval/cjson/ground_truth.json`

**build.sh:**
```bash
#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")"
# Pin to v1.7.18 for reproducibility
CJSON_TAG="v1.7.18"
curl -sL "https://raw.githubusercontent.com/DaveGamble/cJSON/${CJSON_TAG}/cJSON.c" -o cJSON.c
curl -sL "https://raw.githubusercontent.com/DaveGamble/cJSON/${CJSON_TAG}/cJSON.h" -o cJSON.h
cat > main.c << 'MAIN'
#include <stdio.h>
#include <stdlib.h>
#include "cJSON.h"
int main() {
    const char *json = "{\"key\": \"value\"}";
    cJSON *root = cJSON_Parse(json);
    if (root) {
        char *out = cJSON_Print(root);
        if (out) { printf("%s\n", out); free(out); }
        cJSON_Delete(root);
    }
    return 0;
}
MAIN
# Dynamic linking (NOT -static) so import symbols exist for Pass -1
gcc -O2 -g -o cjson_test_symbols cJSON.c main.c -lm
cp cjson_test_symbols cjson_test
strip cjson_test
echo "Binaries built. Run extract_ground_truth.py next (requires Binja headless)."
```

**extract_ground_truth.py:**
```python
"""Extract ground truth by comparing stripped vs. unstripped in Binja headless.
Match functions by address. Record function name, parameters, local variables.
"""
import binaryninja
import json

def extract():
    sym_bv = binaryninja.open_view("cjson_test_symbols")
    stripped_bv = binaryninja.open_view("cjson_test")

    ground_truth = {}
    for func in sym_bv.functions:
        # Skip runtime/unnamed functions
        if func.name.startswith("sub_") or func.name.startswith("_"):
            continue
        addr = hex(func.start)
        # Verify address exists in stripped binary
        stripped_func = stripped_bv.get_function_at(func.start)
        if not stripped_func:
            continue
        ground_truth[addr] = {
            "function_name": func.name,
            "parameters": [{"name": p.name, "type": str(p.type)}
                           for p in func.parameter_vars],
            "local_variables": [{"name": v.name, "type": str(v.type)}
                                for v in func.vars
                                if v not in func.parameter_vars],
        }

    with open("ground_truth.json", "w") as f:
        json.dump(ground_truth, f, indent=2)
    print(f"Extracted {len(ground_truth)} functions")

if __name__ == "__main__":
    extract()
```

**Test:** `bash build.sh` succeeds. `nm cjson_test` shows no text symbols. `ground_truth.json` has ~25-30 entries.

---

### Deliverable 8.2: End-to-End Pipeline Runner

**What:** Single entry point for the full RE pipeline.

**Files:**
- `reaper/run.py`

**Interface:**
```bash
python -m reaper.run --binary reaper/eval/cjson/cjson_test \
                     --config reaper/configs/default.toml \
                     --run-id cjson_001
```

**Logic:**
```python
import asyncio
import tomllib
import neo4j

from reaper.harness.llm_client import ReaperLLMClient
from reaper.harness.tracer import Tracer
from reaper.harness.ledger import Ledger
from reaper.harness.todo import TodoLedger
from reaper.harness.context import ContextAssembler
from reaper.harness.shadow import ShadowCopyManager
from reaper.harness.merge_agent import MergeAgent
from reaper.harness.pass1_dispatcher import Pass1Dispatcher
from reaper.harness.pass2_dispatcher import Pass2Dispatcher
from reaper.harness.scheduler import Scheduler
from reaper.harness.investigation_loop import InvestigationLoop
from reaper.harness.completion import ResynthesisLoop
from reaper.tools.hlil_extract import HLILExtractor
from reaper.tools.graph_nodes import build_nodes
from reaper.tools.graph_edges import build_edges
from reaper.tools.pin_symbols import pin_symbols
from reaper.tools.graph_analysis import validate_and_order
from reaper.tools.bndb_writer import BNDBWriter
from reaper.tools.struct_detector import StructAccessDetector
from reaper.tools.graph_rebuild import GraphRebuilder
from reaper.agents.type_recovery import TypeRecoveryAgent
from reaper.agents.investigation_agent import InvestigationAgent
from reaper.agents.resynthesis_agent import ResynthesisAgent
from reaper.infra.init_db import init_schema

def load_config(path: str) -> dict:
    with open(path, "rb") as f:
        return tomllib.load(f)

async def main(binary_path: str, config_path: str, run_id: str):
    config = load_config(config_path)
    tracer = Tracer(f"{config['paths']['traces_dir']}/{run_id}")

    # Connect Neo4j
    neo4j_driver = neo4j.AsyncGraphDatabase.driver(
        config['neo4j']['uri'],
        auth=(config['neo4j']['user'], config['neo4j']['password']))
    await init_schema(neo4j_driver)

    # Phase 2: Build graph
    extractor = HLILExtractor(binary_path, config['paths']['data_dir'])
    await build_nodes(extractor, neo4j_driver)
    await build_edges(extractor, neo4j_driver)
    await pin_symbols(extractor, neo4j_driver)
    await validate_and_order(neo4j_driver)

    # Phase 3: Init harness
    llm = ReaperLLMClient(config['vllm']['base_url'], config['vllm']['model'])
    ledger = Ledger(f"{config['paths']['data_dir']}/{run_id}_ledger.db", neo4j_driver)
    await ledger.init()
    await ledger.register_functions_from_graph()
    todo = TodoLedger(f"{config['paths']['data_dir']}/{run_id}_todo.db")
    await todo.init()
    context_asm = ContextAssembler(extractor, neo4j_driver, ledger, config)
    shadow_mgr = ShadowCopyManager(neo4j_driver, ledger, config)
    bndb_writer = BNDBWriter(extractor)
    merge = MergeAgent(llm, shadow_mgr, bndb_writer, ledger, todo, config)

    # Phase 4: Type recovery
    type_agent = TypeRecoveryAgent(llm, context_asm, config)
    candidates = StructAccessDetector(extractor).find_struct_accesses()
    rebuilder = GraphRebuilder(extractor, bndb_writer, neo4j_driver)
    for candidate in candidates:
        struct_def = await type_agent.run(candidate)
        if struct_def:
            await rebuilder.apply_struct(struct_def)
    # Re-register after graph changes from type recovery
    await ledger.register_functions_from_graph()

    # Phase 5: Pass 1
    await Pass1Dispatcher(llm, neo4j_driver, context_asm,
                          bndb_writer, ledger, tracer, config).run()

    # Phase 6: Pass 2
    await Pass2Dispatcher(llm, neo4j_driver, context_asm, shadow_mgr,
                          merge, bndb_writer, ledger, todo, tracer, config).run()

    # Phase 7: Investigation + Resynthesis
    scheduler = Scheduler(llm, todo, context_asm, tracer, config)
    inv_agent = InvestigationAgent(llm, context_asm, ledger, todo, tracer, config)
    inv_loop = InvestigationLoop(scheduler, inv_agent, llm, context_asm,
                                 todo, ledger, tracer, config)
    resynth_agent = ResynthesisAgent(llm, context_asm, ledger, todo, tracer, config)
    resynth_loop = ResynthesisLoop(
        resynth_agent, inv_loop, todo, ledger, neo4j_driver,
        context_asm, tracer, config)
    complete = await resynth_loop.run()

    # Final writeback
    bndb_writer.save()

    # Report
    await tracer.log("pipeline_complete", "runner",
                     {"complete": complete, "run_id": run_id})
    print(f"RE stage {'COMPLETE' if complete else 'INCOMPLETE (best-effort)'}")
    print(f"BNDB: {extractor.bndb_path}")
    print(f"Traces: {tracer.log_dir}")

    await llm.close()
    await neo4j_driver.close()

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--binary", required=True)
    parser.add_argument("--config", default="reaper/configs/default.toml")
    parser.add_argument("--run-id", required=True)
    args = parser.parse_args()
    asyncio.run(main(args.binary, args.config, args.run_id))
```

**Test:** Run on cJSON. Pipeline completes. BNDB exists with renamed functions. Ledger has claims. Trace log exists.

---

### Deliverable 8.3: Ground Truth Evaluator

**What:** Compare REAPER output against ground truth.

**Files:**
- `reaper/eval/evaluate.py`

**Metrics:**

1. **Function name exact match:** % where `canon_name` == ground truth name (case-insensitive)

2. **Function name semantic match (LLM judge):** For non-exact matches, ask the LLM at minimal thinking: "Ground truth: `cJSON_Parse`. REAPER: `parse_json_input`. Are these semantically equivalent for the same function? Answer only yes or no." Use structured output `{"equivalent": bool}`. Total = (exact + LLM-yes) / total_functions.

3. **Variable name accuracy:** Same two-tier (exact + LLM judge). Match by function address + relative position in var list.

4. **Claim coverage:** % of functions with >=1 claim at `mid_confidence` or above.

5. **Type recovery accuracy:** % of ground truth struct fields matched by offset + size (name match not required).

6. **False confidence rate:** For `high_confidence` claims, LLM judge evaluates correctness against ground truth. % wrong.

**Expected call count:** ~30 functions × 2 (name + var check) + ~10 high-confidence claims = ~70 LLM calls at minimal thinking. Fast.

**Output:** JSON report + human-readable summary to stdout.

**Test:** Perfect names → ~100%. Random names → near 0% semantic match.

---

## Implementation Order

```
Phase 1 (all parallel):    1.1 | 1.2 | 1.3 | 1.4 | 1.5 | 1.6
                                ↓
Phase 2 (sequential):      2.1 → 2.2 → 2.3 → 2.4 → 2.5
                                ↓
Phase 3:                   3.1 → then 3.2 | 3.3 | 3.4 | 3.5 in parallel
                                ↓
Phase 4 (sequential):      4.1 → 4.2 → 4.3
                                ↓
Phase 5:                   5.1 → 5.2
                                ↓
Phase 6:                   6.1 → 6.2 → 6.3
                                ↓
Phase 7:                   7.1 | 7.2 → 7.3 → 7.4 → 7.5
                                ↓
Phase 8:                   8.1 → 8.2 → 8.3
```

Total: **28 deliverables across 8 phases.**

## Summary of Constraints

- **Fully async** (`asyncio` + `httpx.AsyncClient` + `neo4j.AsyncDriver`)
- **One model:** DeepSeek-V4-Flash via vLLM on :8035 (shared). All agents same model, different prompts + thinking budgets
- **Structured output:** All agent I/O via OpenAI-standard `response_format` json_schema (Pydantic schemas). Verified live 2026-09-27: `extra_body.structured_outputs` / `guided_json` are silently ignored by this build
- **Thinking budget:** `max_completion_tokens` controls depth (minimal=1024 → max=40960, includes response headroom)
- **Concurrency:** `asyncio.Semaphore(max_concurrent_agents)` for parallel dispatch
- **Claims created before critic:** Insert with truth_level=NULL, critic sets it
- **Runaway prevention:** `max_critic_rejections` (force-accept at speculation), `max_resynthesis_iterations`, `task_timeout_seconds`, investigation loop stuck detection
- **No cross-store atomicity:** Neo4j first, then SQLite. Log inconsistencies, don't rollback
- **Idempotent graph ops:** All Neo4j writes use `MERGE` — safe to re-run
- **Binja 6.0 headless.** Not pip-installed — needs PYTHONPATH. Full API surface in `docs/binja-module.md`
- **Stable node IDs:** Neo4j node `id` based on original var names, never changes on rename
- **SQLite via aiosqlite** with WAL mode + asyncio.Lock on all writes. No concurrent writer crashes
- **Context size limit:** `max_context_tokens` (default 32768). Truncation strategy for oversized resynthesis contexts
- **BNDB rename tracking:** BNDBWriter maintains `_rename_map` so second/subsequent renames of the same variable work
- **SCC second pass:** After main Pass 1 traversal, one extra pass over SCC functions re-evaluates arguments and summaries with peer context
- **Harness owns all writes.** Agents propose, harness applies
