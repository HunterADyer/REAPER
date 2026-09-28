"""reaper.eval.heavy — per-mechanism HEAVY dynamic tests.

What "heavy" means here: each case builds the REAL production harness (real
SQLite ledger/todo via aiosqlite, a MemoryNeo4jDriver graph, a
ScriptedExtractor conforming to the live 2.1 text contract, RecordingTracer),
seeds a KNOWN scenario into it, invokes the ACTUAL mechanism class under test
(CriticEvaluator, RenameVariableAgent, Scheduler, InvestigationLoop,
ResynthesisSocket…) and then — in live mode — scores the mechanism's output
with the modular LLM-judge rubric engine in N independent zero-context runs.

Two modes, one code path:

  offline : LLM is a StubLLM with scripted responses → fully deterministic,
            no network, no Binja. Used as the CI regression gate: assertions
            must ALL pass and scoring is skipped.
  live    : LLM is ReaperLLMClient against the shared :8035 vLLM. The
            mechanism runs for real AND the output is scored (N runs). This
            is the "let the inherent LLM variance aggregate the score" control.

A HeavyCase must implement:

    id              : stable slug used by the runner registry
    description     : one-line human description
    mechanisms      : tuple of mechanism ids this case exercises (e.g.
                      ("critic_loop", "claim_labelling"))
    seed(fx)        : populate ledger/todo/graph/extractor with a scenario
                      whose expected outcome is recorded on ``fx.expected``
    run_case(fx)    : drive the real mechanism; MUTATE the same fixtures.
    assert_expected : list of (passed: bool, message: str) MUST-hold checks
                      (these run in BOTH modes and always gate the verdict)
    score_items     : optional, map module/mechanism output → scoring items
                      by dimension for the LLM-judge (live mode only)

Fixtures (``fx``) are built by :func:`build_fixtures` and provide the exact
production classes with real DB files under a per-case tmp dir:
ledger, todo, tracer, neo4j (MemoryNeo4jDriver), context_asm, bndb_writer,
and config (loaded from configs/default.toml, overridable).
"""

from __future__ import annotations

import asyncio
import json
import logging
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from tests.fake_harness import RecordingTracer, ScriptedExtractor, StubLLM
from tests.fake_neo4j import MemoryNeo4jDriver
from reaper.harness.context import ContextAssembler
from reaper.harness.ledger import Ledger
from reaper.harness.todo import TodoLedger
from reaper.harness.tracer import Tracer
from reaper.tools.bndb_writer import BNDBWriter

log = logging.getLogger(__name__)

#: Config is loaded once from the repo config; cases may supply overrides.
_CFG_CACHE: dict | None = None
CONFIG_PATH = Path(__file__).resolve().parents[2] / "configs" / "default.toml"


def load_config() -> dict:
    """Load + cache the default config (tomllib). Callers may shallow-copy."""
    global _CFG_CACHE
    if _CFG_CACHE is None:
        import tomllib

        with open(CONFIG_PATH, "rb") as fh:
            _CFG_CACHE = tomllib.load(fh)
    return json.loads(json.dumps(_CFG_CACHE))  # deep copy


@dataclass
class Fixtures:
    """All production classes wired to a per-case tmpdir — REAL state."""

    workdir: Path
    config: dict
    ledger: Ledger
    todo: TodoLedger
    tracer: Callable
    neo4j: MemoryNeo4jDriver
    extractor: ScriptedExtractor
    context_asm: ContextAssembler
    bndb_writer: BNDBWriter
    trace_log: Tracer | None = None
    # Mechanisms may record the expected outcome of their seed here; the
    # runner aggregates this in the report for auditability.
    expected: dict = field(default_factory=dict)
    extra: dict = field(default_factory=dict)

    async def close(self) -> None:
        try:
            await self.ledger.close()
        except Exception:
            pass
        try:
            await self.todo.close()
        except Exception:
            pass


async def build_fixtures(config_override: dict | None = None) -> Fixtures:
    """Build a full harness backed by real SQLite files in a temp dir.

    The extractor/neo4j are fakes (they mirror the production contracts), but
    ledger/todo/context/bndb-writer are the REAL classes operating on REAL
    files — that is what makes these "heavy".
    """
    workdir = Path(tempfile.mkdtemp(prefix="reaper_heavy_"))
    cfg = load_config()
    if config_override:
        cfg = _deep_merge(cfg, config_override)

    data_dir = workdir / "data"
    data_dir.mkdir(parents=True, exist_ok=True)

    neo4j = MemoryNeo4jDriver()
    tracer = RecordingTracer()
    ledger = Ledger(str(data_dir / "ledger.db"), neo4j)
    await ledger.init()
    todo = TodoLedger(str(data_dir / "todo.db"))
    await todo.init()
    extractor = ScriptedExtractor(bv=__import__("tests.fake_binja",
                                                fromlist=["FakeBinaryView"]).FakeBinaryView())
    context_asm = ContextAssembler(extractor, neo4j, ledger, cfg)
    bndb_writer = BNDBWriter(extractor)

    # A real Tracer is optional; RecordingTracer is cheaper and API-compatible
    # (agents call tracer.log(...)). Use the recording one.
    return Fixtures(
        workdir=workdir,
        config=cfg,
        ledger=ledger,
        todo=todo,
        tracer=tracer,
        neo4j=neo4j,
        extractor=extractor,
        context_asm=context_asm,
        bndb_writer=bndb_writer,
        trace_log=None,
    )


def _deep_merge(base: dict, over: dict) -> dict:
    out = json.loads(json.dumps(base))
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


@dataclass
class Check:
    """One assertion result. ALL checks must pass for a case to be green."""

    passed: bool
    message: str


@dataclass
class CaseResult:
    """Aggregate report for one heavy case run."""

    case_id: str
    mode: str            # "offline" | "live"
    checks: list[Check] = field(default_factory=list)
    score_report: dict | None = None
    error: str | None = None
    expected: dict = field(default_factory=dict)

    @property
    def passed(self) -> bool:
        return all(c.passed for c in self.checks)


class HeavyCase:
    """Base class for one per-mechanism heavy test.

    Subclasses override:
      id / description / mechanisms   — identity + which mechanisms exercised
      seed(fx)                       — populate fixtures with known scenario;
                                       also record fx.expected for the report
      run_case(fx, llm)              — drive the REAL mechanism under test
      scripted_responses(fx)         — ordered raw JSON strings for the OFFLINE
                                       StubLLM (must match each mech's schema)
      assert_expected(fx)            — MUST-hold checks, run in BOTH modes and
                                       always gate the verdict
      score_items(fx)                — optional {dimension: [items]} for the
                                       LLM-judge (live mode only)
    """

    id: str = ""
    description: str = ""
    mechanisms: tuple[str, ...] = ()

    # -- contract ------------------------------------------------------------

    async def seed(self, fx: Fixtures) -> None:  # noqa: B027 - optional
        pass

    async def run_case(self, fx: Fixtures, llm) -> None:
        raise NotImplementedError

    def scripted_responses(self, fx: Fixtures) -> list[str]:
        return []

    async def assert_expected(self, fx: Fixtures) -> list[Check]:
        return []

    def score_items(self, fx: Fixtures) -> dict[str, list[dict]]:
        return {}

    # -- helpers -------------------------------------------------------------

    @staticmethod
    def ok(message: str) -> Check:
        return Check(True, message)

    @staticmethod
    def fail(message: str) -> Check:
        return Check(False, message)


__all__ = [
    "Fixtures", "build_fixtures", "load_config", "Check", "CaseResult",
    "HeavyCase",
]
