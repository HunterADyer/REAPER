"""End-to-end REAPER pipeline runner — Deliverable 8.2.

Single entry point for the full RE stage. Mirrors design-re-stage.md § 8.2
exactly (all constructor calls + import graph). Requires Binary Ninja headless
for the Phase-2 graph build; when it is absent a clear message is printed
instead of a confusing traceback (see reaper/tools/_compat.py).

Usage:
    python -m reaper.run --binary eval/cjson/cjson_test \\
                         --config configs/default.toml \\
                         --run-id cjson_001
"""

import argparse
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
from reaper.tools import _compat


def load_config(path: str) -> dict:
    with open(path, "rb") as f:
        return tomllib.load(f)


async def build_graph(extractor, neo4j_driver) -> None:
    """Phase 2: Node/edge construction + symbol preservation + ordering."""
    await build_nodes(extractor, neo4j_driver)
    await build_edges(extractor, neo4j_driver)
    await pin_symbols(extractor, neo4j_driver)
    await validate_and_order(neo4j_driver)


async def main(binary_path: str, config_path: str, run_id: str = "default"):
    config = load_config(config_path)
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
    await init_schema(neo4j_driver)

    # Phase 2: Build graph
    extractor = HLILExtractor(binary_path, config['paths']['data_dir'])
    await build_graph(extractor, neo4j_driver)

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


def main() -> None:  # noqa: D103 — console-script entry point (async wrapper)
    parser = argparse.ArgumentParser()
    parser.add_argument("--binary", required=True)
    parser.add_argument("--config", default="configs/default.toml")
    parser.add_argument("--run-id", required=True)
    args = parser.parse_args()
    asyncio.run(main(args.binary, args.config, args.run_id))  # type: ignore[arg-type]


if __name__ == "__main__":
    main()

