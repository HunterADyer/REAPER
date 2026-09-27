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

<!-- FILL: After implementing 1.3 + 5.1, paste verified RenameVariableAgent.run() here -->

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

<!-- FILL: After implementing 2.2, paste verified build_nodes() MERGE loop here -->

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

<!-- FILL: After implementing 5.1, paste verified RenameVariableAgent class here -->

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

<!-- FILL: After implementing 5.2, paste verified Pass1Dispatcher.run() level loop here -->

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

<!-- FILL: After implementing 6.1 + 6.3, paste verified critic loop here -->

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

<!-- FILL: After implementing 2.3, paste verified instruction walker here.
     Pay special attention to the operand names (dest/src/params) — verify
     against binja-module.md § HLIL instruction types -->

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

<!-- FILL: After implementing 3.1, paste verified for_function() here -->
