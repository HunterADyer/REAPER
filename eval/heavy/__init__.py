"""reaper.eval.heavy — per-mechanism HEAVY dynamic testing pipeline.

Each case builds a real harness (SQLite ledger/todo, graph, extractor), seeds
a KNOWN scenario, drives the ACTUAL mechanism class, and (in live mode) scores
its output with the modular LLM-judge rubric engine over N independent
zero-context runs. Offline mode uses a scripted StubLLM for a deterministic CI
gate; live mode uses the shared :8035 vLLM.

Run: ``python -m reaper.eval.heavy.runner --list`` then
     ``python -m reaper.eval.heavy.runner --case critic_loop --mode live``
"""

from reaper.eval.heavy.base import (
    CaseResult,
    Check,
    Fixtures,
    HeavyCase,
    build_fixtures,
    load_config,
)
from reaper.eval.heavy import runner, cases  # noqa: F401  (runner + case registry)

__all__ = [
    "HeavyCase", "Fixtures", "CaseResult", "Check", "build_fixtures",
    "load_config",
]
