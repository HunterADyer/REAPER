"""Harness core: LLM client, tracer, ledger, todo, context, agents wiring.

Submodules and their owning deliverables:

- ``llm_client``  ReaperLLMClient + LLMTransientError          (1.3)
- ``tracer``      asyncio Tracer (JSONL event log)             (1.4)
- ``ledger``      Ledger — per-function SQLite claim store     (1.5)
- ``todo``        TodoLedger — SQLite task (TODO) queue        (1.6)
- ``submission``  submission protocol Pydantic models          (3.3)

All modules use async I/O only; SQLite access is via aiosqlite behind
per-instance asyncio.Lock. Import the modules directly (``from
harness.submission import Rename``) rather than going through this package
namespace — there are no re-exports by design.
"""

