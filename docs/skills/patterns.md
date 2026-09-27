# REAPER — Code Patterns

Reusable templates for implementing deliverables. Each has a template and a
`<!-- FILL -->` placeholder — paste a verified working example there after
you implement the relevant deliverable, so later deliverables can reference it.

---

## Pattern 1: LLM Client Call (all agents)

**Used by:** 5.1, 4.2, 6.1, 6.2, 7.1, 7.2, 7.4, 3.5

```python
from reaper.harness.submission import get_schema, parse_response

session_id = f"{agent_role}_{func_addr}"
await self.llm.create_session(session_id, SYSTEM_PROMPT_TEXT)
response = await self.llm.send(
    session_id,
    context_text,
    thinking_level=self.config['thinking_levels']['<config_key>'],
    structured_output=get_schema(OutputModel)
)
result = parse_response(OutputModel, response)
self.llm.destroy_session(session_id)
return result
```

<!-- VERIFIED (5.1 agents/rename_variable.py) — the exact call that every
agent here mimics (review/critic/resynthesis use the identical shape):

    async def run(self, func_address, var_id, include_callee_renames=False):
        context = await self.context_asm.for_variable(
            func_address, var_id, include_callee_renames=include_callee_renames)
        session_id = f"rename_{func_address}_{var_id}"
        try:
            await self.llm.create_session(session_id, self.prompt)
            response = await self.llm.send(
                session_id, context,
                thinking_level=self.config["thinking_levels"]["pass1_rename"],
                structured_output=get_schema(Submission))
            result = parse_response(Submission, response)
        finally:
            self.llm.destroy_session(session_id)
        return result

NOTE: only the prompt, session prefix, schema, and thinking key differ
between agents. Never close the client here — run.py owns llm.close(). -->

---

## Pattern 2: Neo4j MERGE (graph builders)

**Used by:** 2.2, 2.3, 2.4, 2.5, 3.2, 4.3

```python
async with neo4j_driver.session() as session:
    await session.run(
        "MERGE (n:Label {id: $id}) "
        "SET n.prop1 = $val1, n.prop2 = $val2",
        {"id": node_id, "val1": v1, "val2": v2}
    )
```

Batch pattern (multiple nodes):
```python
async with neo4j_driver.session() as session:
    for item in items:
        await session.run(
            "MERGE (n:Label {id: $id}) SET n += $props",
            {"id": item.id, "props": item.dict()}
        )
```

<!-- VERIFIED (5.2 harness/pass1_dispatcher.py) — rename apply; id-based key for
variables, address-keyed for functions. NEVER str.format() a query containing
`{...}` map syntax — use concatenation for labels:

    async def _merge_rename(self, rn: Rename) -> None:
        async with self.neo4j_driver.session() as session:
            if ":" in rn.node_id:
                await session.run(
                    "MERGE (n {id: $id}) "
                    "SET n.llm_name = $llm, n.canon_name = $canon",
                    {"id": rn.node_id, "llm": rn.llm_name, "canon": rn.canon_name})
            else:
                await session.run(
                    "MERGE (n:Function {address: $id}) "
                    "SET n.llm_name = $llm, n.canon_name = $canon",
                    {"id": rn.node_id, "llm": rn.llm_name, "canon": rn.canon_name})

Identical discipline in graph rebuild (4.3): all writes MERGE, idempotent. -->

---

## Pattern 3: aiosqlite Write-Behind-Lock (Ledger, TodoLedger)

**Used by:** 1.5, 1.6

```python
import asyncio
import aiosqlite

class Store:
    def __init__(self, db_path: str):
        self.db_path = db_path
        # NO I/O here — caller must await init()

    async def init(self):
        self._db = await aiosqlite.connect(self.db_path)
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.execute("PRAGMA foreign_keys=ON")
        self._write_lock = asyncio.Lock()
        # CREATE TABLE IF NOT EXISTS ...

    async def read_op(self, ...) -> ...:
        # No lock needed for reads (WAL mode)
        cursor = await self._db.execute("SELECT ...", (...))
        return await cursor.fetchall()

    async def write_op(self, ...) -> ...:
        async with self._write_lock:
            await self._db.execute("INSERT ...", (...))
            await self._db.commit()
            return result
```

<!-- VERIFIED (1.5 harness/ledger.py) — claim insert with evidence, behind lock:
Note add_claim() also dovetails with the graph: it INSERT OR IGNOREs the
function row (idempotent, safe even if register_functions_from_graph() has not
run), and claims are always born with truth_level = NULL for the critic.

    async def add_claim(self, function_address, claim_text, submitted_by, evidence):
        db = self._require_ready()
        now = _now()
        async with self._write_lock:
            await db.execute(
                "INSERT OR IGNORE INTO functions (address) VALUES (?)",
                (function_address,),
            )
            cursor = await db.execute(
                "INSERT INTO claims (function_address, claim_text, submitted_by, "
                "created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                (function_address, claim_text, submitted_by, now, now),
            )
            claim_id = int(cursor.lastrowid)
            for ev in evidence:
                await db.execute(
                    "INSERT INTO evidence_links (claim_id, address_start, "
                    "address_end, description) VALUES (?, ?, ?, ?)",
                    (claim_id, ev.get("address_start"), ev.get("address_end"),
                     ev.get("description")),
                )
            await db.commit()
            return claim_id
--> 

---

## Pattern 4: Agent Class (all agent deliverables)

**Used by:** 5.1, 4.2, 6.2, 7.2, 7.4

```python
class SomeAgent:
    def __init__(self, llm_client, context_asm, tracer, config):
        self.llm = llm_client
        self.ctx = context_asm
        self.tracer = tracer
        self.config = config

    async def run(self, input_param) -> OutputModel:
        # 1. Assemble context
        context = await self.ctx.for_<method>(input_param)

        # 2. LLM call (Pattern 1)
        session_id = f"<role>_{unique_id}"
        await self.llm.create_session(session_id, PROMPT)
        response = await self.llm.send(
            session_id, context,
            thinking_level=self.config['thinking_levels']['<key>'],
            structured_output=get_schema(OutputModel)
        )
        result = parse_response(OutputModel, response)

        # 3. Log + cleanup
        await self.tracer.log("<event>", session_id, {"input": ..., "output": ...})
        self.llm.destroy_session(session_id)
        return result
```

Prompt text lives in `reaper/agents/prompts/<agent_name>.txt`, loaded once at
`__init__` time (read the file, store as `self.prompt`).

<!-- VERIFIED (5.1 agents/rename_variable.py + 6.2 review_agent.py): the
standard agent skeleton is: load self.prompt from agents/prompts/<name>.txt
at __init__; every run() opens a session, calls llm.send(..., structured_output=
get_schema(OutputModel)), parses with parse_response, and destroy_session in a
finally. The LLM client is never closed by agents (run.py owns llm.close()).
See Pattern 1 for the exact call; each agent only varies prompt/schema/keys. -->

---

## Pattern 5: Parallel Dispatch with Semaphore (dispatchers, loops)

**Used by:** 5.2, 6.3, 7.3

```python
sem = asyncio.Semaphore(self.config['limits']['max_concurrent_agents'])

async def process(item):
    async with sem:
        result = await self.agent.run(item)
        await self._handle_result(item, result)

await asyncio.gather(*(process(item) for item in items))
```

IMPORTANT: Within a single function, variable renames are SEQUENTIAL (each
rename updates Neo4j immediately, next rename sees the updated state).
Parallelism is ACROSS functions at the same traversal level.

<!-- VERIFIED (5.2 harness/pass1_dispatcher.py) — the level loop. Sequential
WITHIN a function (renames hit Neo4j immediately, next rename must see them);
parallel ACROSS functions at the same level via asyncio.gather + a Semaphore;
levels are AWAITED before the next:

    async def run(self) -> None:
        functions = await self._functions_ordered()
        max_level = max(level for _, level in functions)
        for level in range(max_level + 1):
            addrs = [a for a, lvl in functions if lvl == level]
            if not addrs:
                continue
            await asyncio.gather(*(self._process_function(a) for a in addrs))
            await self._log("pass1_level_done", {"level": level, "functions": addrs})

    async def _process_function(self, func_addr):
        async with self._semaphore:
            for var_id in await self._query_nodes(func_addr, "Variable"):
                sub = await self.rename_agent.run(func_addr, var_id)
                await self._apply_submission(sub)   # SEQUENTIAL per function
            ...

async def _query_nodes:  # label via concatenation, not .format()
    query = ("MATCH (f:Function {address: $fa})-[:CONTAINS]->(n:" + label +
             ") WHERE NOT coalesce(n.pinned, false) RETURN n.id AS id") -->

---

## Pattern 6: Critic Evaluation Flow (Pass 2, Investigation)

**Used by:** 6.1, 6.3, 7.3

```python
from reaper.agents.critic_agent import CriticEvaluator

# In dispatcher __init__:
self.critic = CriticEvaluator(llm_client, context_asm, ledger, tracer, config)

# After agent produces a claim:
claim_id = await self.ledger.add_claim(
    func_addr, claim.claim_text, session_id,
    [e.model_dump() for e in claim.evidence]
)
outcome = await self.critic.evaluate_claim(claim_id, claim, func_addr, agent_role)

if not outcome.accepted:
    # Re-invoke agent with feedback
    context += f"\n\nPRIOR ATTEMPT REJECTED:\n{outcome.feedback}"
    # ... retry (agent produces new claim, old one was deleted by critic) ...
```

<!-- VERIFIED (6.3 harness/pass2_dispatcher.py) — claims first, then critic;
claims are excluded from the merge Submission (already in the ledger — else
duplicate rows via shadow apply):

    claim_id = await self.ledger.add_claim(
        claim.function_address, claim.claim_text, f"pass2:{func_addr}",
        [e.model_dump() for e in claim.evidence])
    outcome = await self.critic.evaluate_claim(
        claim_id, claim, func_addr, "review_agent")
    if outcome.accepted:
        accepted_claims.append(claim)
    # ... after all claims + task critics:
    if review.renames and (not review.claims or accepted_claims):
        await self.merge_agent.attempt_merge(
            f"pass2_{func_addr}", Submission(renames=review.renames, claims=[]))

The critic itself (agents/critic_agent.py) owns rejection counting and deletes
the rejected claim; the CALLER only re-invokes the agent with feedback. -->

---

## Pattern 7: Binja HLIL Instruction Walker (graph builders, struct detector)

**Used by:** 2.1, 2.2, 2.3, 4.1

```python
from binaryninja import HighLevelILOperation

func = bv.get_function_at(int(address, 16))
for instr in func.hlil.instructions:
    # Top-level: match on operation
    match instr.operation:
        case HighLevelILOperation.HLIL_ASSIGN:
            dest = instr.dest    # target variable
            src = instr.src      # source expression
        case HighLevelILOperation.HLIL_CALL:
            target = instr.dest  # call target
            args = instr.params  # argument list
        case HighLevelILOperation.HLIL_RET:
            retval = instr.src   # return value
        case HighLevelILOperation.HLIL_VAR_INIT:
            var = instr.dest
            init_expr = instr.src
        # ... other cases per design doc 2.3 table

    # Recurse into sub-expressions
    def walk_expr(expr):
        if not isinstance(expr, HighLevelILInstruction):
            return
        # Process expr based on expr.operation
        for operand in expr.operands:
            walk_expr(operand)
```

<!-- VERIFIED — use reaper.tools._compat.walk_expr() / Op instead of raw Binja
enums (works without Binja, against fake HLIL too). For a context-aware walk
(access_type read/write) the struct detector (4.1) re-implements the same
dispatch with a `write` flag propagated ONLY through HLIL_ASSIGN.dest:

    def _walk(self, expr, func_addr, write):
        if expr is None or not hasattr(expr, "operation"):
            return
        op = expr.operation
        if op == Op.HLIL_DEREF:
            self._deref_access(expr, func_addr, write)
        elif op in (Op.HLIL_STRUCT_FIELD, Op.HLIL_DEREF_FIELD):
            self._field_access(expr, func_addr, write)
        if op in _BINARY_OPS:
            self._walk(expr.left, func_addr, False); self._walk(expr.right, ...)
        elif op == Op.HLIL_ASSIGN:
            self._walk(expr.dest, func_addr, True);  self._walk(expr.src, ...)
        # ... dispatch mirrors _compat.walk_expr exactly

Op/_BINARY_OPS/_UNARY_SRC_OPS/_ARRAY_OPS/_safe_list all come from
reaper.tools._compat. -->

---

## Pattern 8: Context Assembly (used by all context methods)

**Used by:** 3.1

```python
parts = []
# 1. Function signature
parts.append(f"Function: {self.extractor.get_function_signature(addr)}")
# 2. Full HLIL
parts.append(f"HLIL:\n{self.extractor.get_function_hlil(addr)}")
# 3. String references
refs = self.extractor.get_string_refs(addr)
if refs:
    parts.append("String refs:\n" + "\n".join(f'  {r["address"]}: "{r["value"]}"' for r in refs))
# 4. Optional sections based on flags
if include_claims:
    claims = await self.ledger.get_claims(addr)
    parts.append("Claims:\n" + "\n".join(
        f'  [claim_id={c["id"]}] {c["claim_text"]} ({c["truth_level"]})' for c in claims))
# 5. Truncation check
text = "\n\n".join(parts)
# ... truncation logic if over max_context_tokens ...
return text
```

<!-- VERIFIED (3.1 harness/context.py) — every method routes through
_finalize() which enforces config['limits']['max_context_tokens'] and logs a
warning on truncation. for_subgraph adds the 3-step ladder: (1) drop claims
below mid_confidence, (2) summarize HLIL to signature + first/last 10 lines,
(3) drop least-connected functions. All Neo4j/ledger reads degrade gracefully
(exception → section omitted, never crash). -->
