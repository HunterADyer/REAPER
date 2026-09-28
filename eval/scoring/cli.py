"""Scoring pipeline CLI — ``python -m reaper.eval.scoring``.

Scores REAPER's recovered output against ground truth across every rubric
dimension (function names, variable names, datatypes) in N independent
zero-context LLM-judge runs, and writes a JSON report.

Usage:
    python -m reaper.eval.scoring --ground-truth eval/cjson/ground_truth.json \
        --reaper-output data/cjson_001_reaper_output.json \
        --n-runs 5 --out data/cjson_001_score_report.json

Flags:
    --dimensions function-name,variable-name,datatype  (default: all three)
    --max-items-per-dimension N  (debug: truncate each dimension)
    --cache PATH                 (resume cache; default <dir>/score_cache.jsonl)
    --base-url / --model         (defaults match the live shared :8035 endpoint)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from reaper.eval.scoring.align import all_dimensions
from reaper.eval.scoring.scheduler import ScoreScheduler
from reaper.harness.llm_client import ReaperLLMClient

DEFAULT_BASE_URL = "http://localhost:8035/v1"
DEFAULT_MODEL = "deepseek"


def _load(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    return data if isinstance(data, dict) else {}


def _format_report(report: dict) -> str:
    lines = ["REAPER LLM-JUDGE SCORE (N independent zero-context runs)", "=" * 48]
    totals = report.get("totals") or {}
    for dim, t in totals.items():
        lines.append(f"  {dim:<16} n={t.get('n', 0):>4}  mean={t.get('mean', 0.0):>6.2f}")
    lines.append("")
    for dim, sec in (report.get("dimensions") or {}).items():
        lines.append(f"-- {dim} (" +
                     f"{sec.get('count', 0)} item(s)) --")
        lines.append(f"   strict equivalent (>=8): {sec.get('strict_equivalent_rate', 0)}%")
        lines.append(f"   related or better (>=5): {sec.get('related_or_better_rate', 0)}%")
        lines.append(f"   failed/no rename (<0.5): {sec.get('failed_no_rename_rate', 0)}%")
        worst = sec.get("worst") or []
        if worst:
            lines.append("   worst 5:")
            for r in worst[:5]:
                lines.append(f"     {r['key']}  {r['mean']:4.2f}")
    return "\n".join(lines)


async def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--ground-truth", required=True)
    p.add_argument("--reaper-output", required=True)
    p.add_argument("--n-runs", type=int, default=5)
    p.add_argument("--dimensions", default=None,
                   help="comma-separated rubric ids (default: all)")
    p.add_argument("--max-items-per-dimension", type=int, default=0)
    p.add_argument("--base-url", default=DEFAULT_BASE_URL)
    p.add_argument("--model", default=DEFAULT_MODEL)
    p.add_argument("--cache", default=None)
    p.add_argument("--out", default="data/score_report.json")
    args = p.parse_args()

    validate = ("function-name", "variable-name", "datatype")
    dims = None
    if args.dimensions:
        dims = [d.strip() for d in args.dimensions.split(",") if d.strip()]
        bad = set(dims) - set(validate)
        if bad:
            p.error(f"unknown dimensions {sorted(bad)}; expected {validate}")

    ground_truth = _load(args.ground_truth)
    recovered = _load(args.reaper_output)
    items = all_dimensions(ground_truth, recovered)
    if args.max_items_per_dimension > 0:
        items = {k: v[:args.max_items_per_dimension] for k, v in items.items()}
    total = sum(len(v) for v in items.values())
    if total == 0:
        print("no scored pairs found — is ground_truth.json populated? "
              "(re-run extract_ground_truth.py once Binja is up)")
        return 0

    cache = args.cache or str(Path(args.out).with_name("score_cache.jsonl"))
    scheduler = ScoreScheduler(
        ReaperLLMClient(args.base_url, args.model, run_id="scorer"),
        n_runs=args.n_runs, cache_path=cache,
    )
    print(f"scoring {total} item(s) x {args.n_runs} independent runs "
          f"(dimensions: {dims or 'all'})")
    report = await scheduler.score(items, dimensions=dims)
    await scheduler.close()

    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2)
    print(_format_report(report))
    print(f"\nreport written to {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
