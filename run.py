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


async def run_pipeline(binary_path: str, config_path: str, run_id: str = "default",
                       phases=None):
    """Run the selected pipeline phases (default all of 2-7).

    Phases: 2 graph build · 3 harness init · 4 type recovery · 5 Pass 1 ·
            6 Pass 2 · 7 investigation + resynthesis (+ export).

    ``--phases`` enables resumability: Neo4j + SQLite state persist across
    invocations, so a crashed live run can be resumed by re-running only the
    phases that did not finish (or just ``3`` to re-export reports). Harness
    init (3) is implied by any later phase.
    """
    if phases is None:
        phases = frozenset({2, 3, 4, 5, 6, 7})
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
    llm = None
    ledger = None
    todo = None
    complete = False
    try:
        await init_schema(neo4j_driver)

        # Phase 2: Build graph
        extractor = HLILExtractor(binary_path, config['paths']['data_dir'])
        if 2 in phases:
            await build_graph(extractor, neo4j_driver)
        else:
            print(
                "[reaper.run] Phase 2 (graph build) skipped — "
                "reusing the existing Neo4j graph."
            )

        wants_harness = any(p in phases for p in (3, 4, 5, 6, 7))
        if wants_harness:
            # Phase 3: Init harness (required by every later phase).
            llm = ReaperLLMClient(config['vllm']['base_url'], config['vllm']['model'])
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
                type_agent = TypeRecoveryAgent(llm, context_asm, config)
                candidates = StructAccessDetector(extractor).find_struct_accesses()
                rebuilder = GraphRebuilder(extractor, bndb_writer, neo4j_driver)
                for candidate in candidates:
                    struct_def = await type_agent.run(candidate)
                    if struct_def:
                        await rebuilder.apply_struct(struct_def)
                        # Persist for the 8.3 metric-5 export (design § 8.4).
                        try:
                            await ledger.record_struct(struct_def)
                        except Exception:
                            log.exception("run: record_struct failed")
                # Re-register after graph changes from type recovery
                await ledger.register_functions_from_graph()

            # Phase 5: Pass 1
            if 5 in phases:
                await Pass1Dispatcher(llm, neo4j_driver, context_asm,
                                      bndb_writer, ledger, tracer, config).run()

            # Phase 6: Pass 2
            if 6 in phases:
                await Pass2Dispatcher(llm, neo4j_driver, context_asm, shadow_mgr,
                                      merge, bndb_writer, ledger, todo, tracer,
                                      config).run()

            # Phase 7: Investigation + Resynthesis
            if 7 in phases:
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
        else:
            print("RE stage graph-only ('--phases 2'): BNDB/graph built.")
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


def main() -> None:  # noqa: D103 — console-script entry point (async wrapper)
    parser = argparse.ArgumentParser()
    parser.add_argument("--binary", required=True)
    parser.add_argument("--config", default="configs/default.toml")
    parser.add_argument("--run-id", required=True)
    parser.add_argument(
        "--phases", default="2,3,4,5,6,7",
        help="comma-separated phases to run: 2 graph build, 3 harness init, "
             "4 type recovery, 5 Pass 1, 6 Pass 2, 7 investigation+resynthesis. "
             "Persisted state enables resumption (e.g. '--phases 7' after a "
             "crash, or '--phases 3' to re-export the evaluation report).",
    )
    args = parser.parse_args()
    phases = [p.strip() for p in args.phases.split(",") if p.strip()]
    asyncio.run(
        run_pipeline(args.binary, args.config, args.run_id, phases)  # type: ignore[arg-type]
    )


if __name__ == "__main__":
    main()

