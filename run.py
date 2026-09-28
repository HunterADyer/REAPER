"""End-to-end REAPER pipeline runner — Deliverable 8.2.

Single entry point for the full RE stage. Mirrors design-re-stage.md § 8.2
exactly (all constructor calls + import graph). Requires Binary Ninja headless
for the Phase-2 graph build; when it is absent a clear message is printed
instead of a confusing traceback (see reaper/tools/_compat.py).

Usage:
    python -m reaper.run --binary eval/cjson/cjson_test \\
                         --config configs/default.toml \\
                         --run-id cjson_001
    # resume a crashed run at a later phase (state persists in Neo4j + SQLite):
    python -m reaper.run --binary eval/cjson/cjson_test \\
                         --config configs/default.toml \\
                         --run-id cjson_001 --phases 7
    # re-export reports without re-running any LLM phase:
    python -m reaper.run --binary eval/cjson/cjson_test \\
                         --config configs/default.toml \\
                         --run-id cjson_001 --phases 3
"""

import argparse
import asyncio
import logging
import tomllib
from pathlib import Path

import neo4j

from reaper.harness.llm_client import ReaperLLMClient
from reaper.harness.tracer import Tracer
from reaper.harness.events import emit
from reaper.harness.rltrace import RLTraceWriter
from reaper.harness.debugtrace import DebugLogWriter
from reaper.harness.tracedriver import traced_driver
from reaper.harness.ledger import Ledger
from reaper.harness.todo import TodoLedger
from reaper.harness.context import ContextAssembler
from reaper.harness.shadow import ShadowCopyManager
from reaper.harness.merge_agent import MergeAgent
from reaper.harness.pass1_dispatcher import Pass1Dispatcher
from reaper.harness.ratify_dispatcher import RatifyDispatcher
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
from reaper.tools.export_report import extract_reaper_output, write_reaper_output
from reaper.agents.type_recovery import TypeRecoveryAgent
from reaper.agents.investigation_agent import InvestigationAgent
from reaper.agents.resynthesis_agent import ResynthesisAgent
from reaper.infra.init_db import init_schema
from reaper.tools import _compat

log = logging.getLogger(__name__)


def load_config(path: str) -> dict:
    with open(path, "rb") as f:
        return tomllib.load(f)


async def build_graph(extractor, neo4j_driver) -> None:
    """Phase 2: Node/edge construction + symbol preservation + ordering."""
    await build_nodes(extractor, neo4j_driver)
    await build_edges(extractor, neo4j_driver)
    await pin_symbols(extractor, neo4j_driver)
    await validate_and_order(neo4j_driver)


async def _phase_event(run_id: str, phase: int, status: str, **extra) -> None:
    """Emit a live run.phase event (never raises)."""
    await emit("run.phase", "runner", {
        "run_id": run_id, "phase": phase, "status": status, **extra})


async def run_pipeline(binary_path: str, config_path: str, run_id: str = "default",
                       phases=None):
    """Run the selected pipeline phases (default deterministic 2-pass RE).

    Phases: 2 graph build · 3 harness init · 4 type recovery · 5 Pass 0
    (lowest-effort provisional-name sweep) · 6 Pass 1 = deterministic RATIFY
    (xhigh, evidence-grounded approve/rename decisions published to the ledger,
    rename exactly once — no critic) · 7 OPTIONAL deep investigation +
    resynthesis (only when explicitly requested).

    The DEFAULT is the deterministic 2-pass: phases 2,3,4,5,6. Phase 7 is
    opt-in via ``--phases 2,3,4,5,6,7`` for the old critic-driven deep dive;
    it is not needed for a completed RE stage.

    ``--phases`` enables resumability: Neo4j + SQLite state persist across
    invocations, so a crashed live run can be resumed by re-running only the
    phases that did not finish (or just ``3`` to re-export reports). Harness
    init (3) is implied by any later phase.
    """
    if phases is None:
        phases = frozenset({2, 3, 4, 5, 6})
    else:
        phases = frozenset(int(p) for p in (phases or []))
    config = load_config(config_path)
    # Resolve data/traces paths against the repo root — NOT the CWD — and make
    # sure the directories exist up front. This prevents wrong literal subdirs
    # (e.g. `reaper/data`) when running from another directory, and pre-creates
    # the dirs the ledger / output export expect to exist on first run.
    config_root = Path(config_path).resolve().parent.parent
    for _key in ("data_dir", "traces_dir"):
        _raw = config["paths"][_key]
        if not Path(_raw).is_absolute():
            config["paths"][_key] = str((config_root / _raw).resolve())
    Path(config["paths"]["data_dir"]).mkdir(parents=True, exist_ok=True)
    Path(config["paths"]["traces_dir"]).mkdir(parents=True, exist_ok=True)
    tracer = Tracer(f"{config['paths']['traces_dir']}/{run_id}")

    # Live telemetry: RL/tuning trace (clean per-LLM-turn records) + full
    # debug firehose (every event). Both are modular bus subscribers; the
    # monitoring GUI consumes the same stream over WebSocket/REST.
    rl_writer = RLTraceWriter(config["paths"]["data_dir"], run_id)
    debug_writer = DebugLogWriter(config["paths"]["data_dir"], run_id)
    rl_writer.start()
    debug_writer.start()
    await emit("run.started", "runner", {
        "run_id": run_id, "binary": binary_path, "config": config_path,
        "phases": sorted(phases),
    })

    if not _compat.BINJA_AVAILABLE:
        print(
            "[reaper.run] Binary Ninja headless not importable — the Phase 2 graph "
            "build / HLIL extraction requires it. Set "
            "PYTHONPATH=$HOME/binja-headless/python. Proceeding to attempt extraction "
            "(HLILExtractor will raise a clear error if a live BinaryView is needed)."
        )

    # Connect Neo4j
    neo4j_driver = neo4j.AsyncGraphDatabase.driver(
        config['neo4j']['uri'],
        auth=(config['neo4j']['user'], config['neo4j']['password']))
    # Wrap the driver so every graph access becomes a live 'graph.query'
    # event (debug/RL trace) — the "what the agents accessed" record.
    neo4j_driver = traced_driver(neo4j_driver, label="master", run_id=run_id)
    llm = None
    ledger = None
    todo = None
    complete = False
    try:
        await init_schema(neo4j_driver)

        # Phase 2: Build graph
        extractor = HLILExtractor(binary_path, config['paths']['data_dir'])
        if 2 in phases:
            await _phase_event(run_id, 2, "start")
            await build_graph(extractor, neo4j_driver)
            await _phase_event(run_id, 2, "done")
        else:
            print(
                "[reaper.run] Phase 2 (graph build) skipped — "
                "reusing the existing Neo4j graph."
            )

        wants_harness = any(p in phases for p in (3, 4, 5, 6, 7))
        if wants_harness:
            # Phase 3: Init harness (required by every later phase).
            llm = ReaperLLMClient(
                config['vllm']['base_url'], config['vllm']['model'], run_id=run_id,
                max_concurrent=int((config.get("limits") or {}).get(
                    "llm_max_concurrent", 1)))
            ledger = Ledger(f"{config['paths']['data_dir']}/{run_id}_ledger.db",
                            neo4j_driver)
            await ledger.init()
            await ledger.register_functions_from_graph()
            todo = TodoLedger(f"{config['paths']['data_dir']}/{run_id}_todo.db")
            await todo.init()
            context_asm = ContextAssembler(extractor, neo4j_driver, ledger, config)
            shadow_mgr = ShadowCopyManager(neo4j_driver, ledger, config)
            bndb_writer = BNDBWriter(extractor)
            merge = MergeAgent(llm, shadow_mgr, bndb_writer, ledger, todo, config)

            # Phase 4: Type recovery
            if 4 in phases:
                await _phase_event(run_id, 4, "start")
                type_agent = TypeRecoveryAgent(llm, context_asm, config)
                detector = StructAccessDetector(extractor)
                rebuilder = GraphRebuilder(extractor, bndb_writer, neo4j_driver)
                # Fixed-point loop: the initial pass-0 recovery runs at
                # pass0_type_recovery ("high"). Applying a struct can expose
                # NEW struct-access patterns (graph rebuild -> fresh HLIL), so
                # any FOLLOW-UP round runs at recovery_followup ("max" = xhigh)
                # for the deep refinement work. Each candidate is processed at
                # most once; max_type_recovery_rounds is a hard cap.
                max_rounds = int((config.get("limits") or {}).get(
                    "max_type_recovery_rounds", 5))
                processed = set()
                round_no = 0
                while round_no < max_rounds:
                    candidates = detector.find_struct_accesses()
                    pending = [c for c in candidates
                               if c.candidate_id not in processed]
                    if not pending:
                        break
                    structurally_changed = False
                    for candidate in pending:
                        processed.add(candidate.candidate_id)
                        try:
                            struct_def = await type_agent.run(
                                candidate, follow_up=(round_no > 0))
                        except Exception:
                            log.exception("run: type recovery failed for %s",
                                         candidate.candidate_id)
                            continue
                        if not struct_def:
                            continue
                        try:
                            await rebuilder.apply_struct(struct_def)
                            structurally_changed = True
                        except Exception:
                            log.exception("run: struct rebuild failed for %s",
                                          candidate.candidate_id)
                        # Persist for the 8.3 metric-5 export (design § 8.4).
                        try:
                            await ledger.record_struct(struct_def)
                        except Exception:
                            log.exception("run: record_struct failed")
                    # Only continue to a follow-up (max) round if applying
                    # structs changed the graph and may have exposed new
                    # candidates; otherwise the recovery is stable.
                    if not structurally_changed:
                        break
                    round_no += 1
                # Re-register after graph changes from type recovery
                await ledger.register_functions_from_graph()
                await _phase_event(run_id, 4, "done")

            # Phase 5: Pass 0 — LOWEST-EFFORT provisional-name sweep.
            # The old Pass-1 machinery (minimal thinking): variables → args →
            # summary per function, applied immediately, no critic. This seed
            # only needs to be plausible; the xhigh ratify pass fixes it.
            if 5 in phases:
                await _phase_event(run_id, 5, "start")
                await Pass1Dispatcher(llm, neo4j_driver, context_asm,
                                      bndb_writer, ledger, tracer, config).run()
                await _phase_event(run_id, 5, "done")

            # Phase 6: Pass 1 — DETERMINISTIC RATIFY (xhigh). One deep pass
            # where the ratifier explores function chains and approves or
            # renames every function/variable/argument name, grounding each
            # decision in cited graph/HLIL evidence; decisions are PUBLISHED to
            # the ledger (name_decisions table) and renames applied exactly
            # once. No critic loop — this is the final RE naming decision
            # (VR refines claims later with better tools).
            if 6 in phases:
                await _phase_event(run_id, 6, "start")
                await RatifyDispatcher(llm, neo4j_driver, context_asm,
                                       bndb_writer, ledger, tracer, config).run()
                await _phase_event(run_id, 6, "done")

            # Phase 7 (OPT-IN): the old critic-driven investigation +
            # resynthesis deep dive. Not required for a completed deterministic
            # RE; only runs when explicitly requested (--phases 2,3,4,5,6,7).
            if 7 in phases:
                await _phase_event(run_id, 7, "start")
                scheduler = Scheduler(llm, todo, context_asm, tracer, config)
                inv_agent = InvestigationAgent(llm, context_asm, ledger, todo,
                                               tracer, config)
                inv_loop = InvestigationLoop(scheduler, inv_agent, llm, context_asm,
                                             todo, ledger, tracer, config)
                resynth_agent = ResynthesisAgent(llm, context_asm, ledger, todo,
                                                 tracer, config)
                resynth_loop = ResynthesisLoop(
                    resynth_agent, inv_loop, todo, ledger, neo4j_driver,
                    context_asm, tracer, config)
                complete = await resynth_loop.run()

            # Completion for the deterministic 2-pass: the ratify pass IS the
            # completion signal (a decision published for every renamable
            # function). Pass-2/investigation no longer gate completion.
            if 6 in phases and 7 not in phases:
                if ledger is not None:
                    try:
                        cov = await ledger.decision_coverage_stats()
                        complete = (
                            cov.get("renamable_functions", 0) > 0
                            and cov.get("with_decisions", 0)
                            == cov.get("renamable_functions", 0)
                        )
                    except Exception:
                        log.exception("run: decision_coverage_stats failed")

            # Final writeback
            bndb_writer.save()
            await tracer.log("pipeline_phases_done", "runner", {
                "phases": sorted(phases), "run_id": run_id})
        else:
            await tracer.log("pipeline_phases_done", "runner", {
                "phases": sorted(phases), "run_id": run_id,
                "note": "graph build only"})

        # Final hygiene + report: sweep orphaned NULL claims, export the
        # evaluator input, and report partial-credit coverage.
        if ledger is not None:
            try:
                swept = await ledger.sweep_null_claims()
                if swept:
                    print(f"[reaper.run] swept {swept} unevaluated (NULL) claims")
            except Exception:
                log.exception("run: sweep_null_claims failed")
            try:
                coverage = await ledger.claim_coverage_stats()
            except Exception:
                coverage = {}
            output_path = f"{config['paths']['data_dir']}/{run_id}_reaper_output.json"
            try:
                data = await extract_reaper_output(neo4j_driver, ledger, extractor)
                write_reaper_output(data, output_path)
            except Exception:
                log.exception("run: reaper-output export failed")
            await tracer.log("pipeline_complete", "runner", {
                "complete": complete, "run_id": run_id,
                "coverage": coverage, "reaper_output": output_path})
            print(f"RE stage {'COMPLETE' if complete else 'INCOMPLETE (best-effort)'}")
            if coverage:
                print(
                    "Claim coverage (renamable functions): "
                    f"{coverage.get('with_claims', 0)}/"
                    f"{coverage.get('renamable_functions', 0)} with claims; "
                    f"{coverage.get('with_mid_confidence_or_above', 0)} "
                    "with mid-conf+ evidence; "
                    f"{coverage.get('speculation_only', 0)} speculation-only; "
                    f"{coverage.get('no_claims', 0)} no claims."
                )
            print(f"Reaper output (evaluation input): {output_path}")
            print(f"BNDB: {extractor.bndb_path}")
            print(f"Traces: {tracer.log_dir}")
            await emit("run.complete", "runner", {
                "run_id": run_id, "complete": complete,
                "coverage": coverage, "reaper_output": output_path,
            })
        else:
            print("RE stage graph-only ('--phases 2'): BNDB/graph built.")
            await emit("run.complete", "runner", {
                "run_id": run_id, "complete": None, "note": "graph build only",
            })
    except Exception as exc:  # noqa: BLE001 - record then re-raise for the CLI
        await emit("run.error", "runner", {
            "run_id": run_id, "error": f"{type(exc).__name__}: {exc}"})
        raise
    finally:
        if llm is not None:
            try:
                await llm.close()
            except Exception:
                log.debug("run: llm.close failed", exc_info=True)
        if todo is not None:
            try:
                await todo.close()
            except Exception:
                log.debug("run: todo.close failed", exc_info=True)
        if ledger is not None:
            try:
                await ledger.close()
            except Exception:
                log.debug("run: ledger.close failed", exc_info=True)
        if neo4j_driver is not None:
            try:
                await neo4j_driver.close()
            except Exception:
                log.debug("run: neo4j_driver.close failed", exc_info=True)
        # Flush RL/debug writers so no telemetry is lost on shutdown.
        for _w in (debug_writer, rl_writer):
            try:
                await _w.stop()
            except Exception:
                log.debug("run: telemetry writer stop failed", exc_info=True)


def main() -> None:  # noqa: D103 — console-script entry point (async wrapper)
    parser = argparse.ArgumentParser()
    parser.add_argument("--binary", required=True)
    parser.add_argument("--config", default="configs/default.toml")
    parser.add_argument("--run-id", required=True)
    parser.add_argument(
        "--phases", default="2,3,4,5,6",
        help="comma-separated phases to run: 2 graph build, 3 harness init, "
             "4 type recovery, 5 Pass 0 (low-effort provisional sweep), "
             "6 Pass 1 deterministic RATIFY (xhigh, evidence-grounded, no "
             "critic), 7 OPTIONAL deep investigation+resynthesis. Default is "
             "the deterministic 2-pass (2,3,4,5,6). Persisted state enables "
             "resumption (e.g. '--phases 7' after a crash, or '--phases 3' to "
             "re-export the evaluation report).",
    )
    args = parser.parse_args()
    phases = [p.strip() for p in args.phases.split(",") if p.strip()]
    asyncio.run(
        run_pipeline(args.binary, args.config, args.run_id, phases)  # type: ignore[arg-type]
    )


if __name__ == "__main__":
    main()

