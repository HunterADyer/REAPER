"""Heavy per-mechanism cases — one module per mechanism under test.

Each case drives the REAL production class (CriticEvaluator, RenameVariableAgent,
ReviewAgent, TypeRecoveryAgent, ResynthesisAgent, Scheduler, InvestigationLoop)
against a seeded real-harness fixture (real SQLite ledger/todo, fake graph +
extractor). All cases work in BOTH offline (scripted StubLLM, deterministic)
and live (real :8035 vLLM) modes via the shared runner in `reaper.eval.heavy
.runner`.

Modules must call ``register(CaseClass)`` at import time; importing this
package triggers registration.
"""

from reaper.eval.heavy.cases import (  # noqa: F401
    claim_labelling,
    critic_loop,
    investigation,
    logic_conclusion,
    renaming,
    resynthesis,
    scheduler,
    type_recovery,
)
