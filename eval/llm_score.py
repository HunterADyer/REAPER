"""LLM-judge scoring of recovered names against ground truth — rubric-based.

COMPATIBILITY SHIM — this module now delegates to the modular scoring engine
(``reaper.eval.scoring``). It is kept ONLY to preserve the public API that
tests and the original CLI depended on (:class:`Scorer`, :func:`_pairs`,
``RUBRIC``).

New code should use:
    reaper.eval.scoring.scheduler.ScoreScheduler   (multi-dimension, N runs)
    reaper.eval.scoring.rubrics.RUBRICS            (the rubric registry)
    reaper.eval.scoring.judge.JudgeEngine          (N independent runs)

The scoring model is unchanged (0-10 rubric, N independent zero-context runs,
resumable JSONL cache); it is just no longer implemented in this file.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

from reaper.harness.llm_client import ReaperLLMClient
from reaper.eval.scoring.judge import JudgeEngine, JudgeScore
from reaper.eval.scoring.rubrics import get_rubric
from reaper.eval.scoring.reporting import aggregate_rows

DEFAULT_BASE_URL = "http://localhost:8035/v1"
DEFAULT_MODEL = "deepseek"
N_RUNS_DEFAULT = 5

#: Backward-compat alias: the legacy inline rubric prose string.
RUBRIC = (
    "  10  exact match (textually identical or effectively identical)\n"
    "  8-9 semantically equivalent: same intent/role, different wording or style\n"
    "  5-7 related but incomplete: generic, partial, or only vaguely captures the role\n"
    "  2-4 wrong or misleading, but plausible (wrong domain / wrong function)\n"
    "  0   no rename, blank, or completely unrelated to the true role"
)


def _load(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    return data if isinstance(data, dict) else {}


def _pairs(ground_truth: dict, reaper_output: dict) -> list[dict]:
    """[(address, true_name, recovered_name)] aligned by function address.

    Legacy helper — delegates to the align module's function-items builder.
    """
    from reaper.eval.scoring.align import function_items

    return [
        {"address": it["key"], "true": it["true"], "recovered": it["recovered"]}
        for it in function_items(ground_truth, reaper_output)
    ]


class Scorer:
    """Legacy wrapper around JudgeEngine for the function-name dimension.

    Preserves the original interface: ``rate(pair, run)`` returns a
    ``JudgeScore``; ``score_all`` returns aggregated rows; ``_aggregate`` is
    a static tolerance for legacy callers. All real work is delegated.
    """

    def __init__(self, llm: ReaperLLMClient, cache_path: str,
                 n_runs: int = N_RUNS_DEFAULT):
        self.llm = llm
        self.n_runs = n_runs
        self.cache_path = cache_path
        self._engine = JudgeEngine(llm, n_runs=n_runs, cache_path=cache_path)
        self._rubric = get_rubric("function-name")

    async def rate(self, pair: dict, run: int) -> JudgeScore:
        item = {"key": pair["address"], "true": pair["true"],
                "recovered": pair["recovered"]}
        return await self._engine.rate(self._rubric, item, run)

    async def score_all(self, pairs: list[dict]) -> dict:
        rows = []
        for pair in pairs:
            item = {"key": pair["address"], "true": pair["true"],
                    "recovered": pair["recovered"]}
            row = await self._engine.score_item(self._rubric, item)
            row["true_name"] = pair["true"]
            row["recovered_name"] = pair["recovered"]
            rows.append(row)
            print(f"  {pair['address']}  mean={row['mean']:4.2f}  "
                  f"{pair['true'][:34]:<34} -> {pair['recovered'][:34]:<34}")
        return self._aggregate(rows)

    @staticmethod
    def _aggregate(rows: list[dict]) -> dict:
        """Legacy aggregation mapper -> the new reporting shape."""
        agg = aggregate_rows(rows)
        return {
            "functions": len(rows),
            "overall_mean": agg.get("overall_mean", 0.0),
            "strict_equivalent_rate": agg.get("strict_equivalent_rate", 0.0),
            "related_or_better_rate": agg.get("related_or_better_rate", 0.0),
            "failed_no_rename_rate": agg.get("failed_no_rename_rate", 0.0),
            "per_function": sorted(rows, key=lambda r: r["mean"]),
            "worst": agg.get("worst") or [],
        }


def _report(name_scores: dict, pairs_n: int) -> dict:
    return {
        "judge": {"type": "llm-rubric", "n_runs": name_scores.get("functions", 0),
                  "pairs_scored": pairs_n, "thinking_level": "minimal"},
        "function_name_scores": name_scores,
        "thresholds": {"equivalent_ge": 8.0, "related_ge": 5.0},
    }


async def main() -> int:
    parser = argparse.ArgumentParser(
        description="LLM-judge, rubric-based scoring of recovered names "
                    "(original vs recovered), averaged over N independent runs.")
    parser.add_argument("--ground-truth", required=True)
    parser.add_argument("--reaper-output", required=True)
    parser.add_argument("--n-runs", type=int, default=N_RUNS_DEFAULT,
                        help="independent scoring runs to average (default 5)")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--max-functions", type=int, default=0,
                        help="limit scoring to the first N functions (debug)")
    parser.add_argument("--cache", default=None,
                        help="resume-cache path (default <reaper-output dir>/score_cache.jsonl)")
    parser.add_argument("--out", default="score_report.json")
    args = parser.parse_args()

    if not os.path.exists(args.reaper_output):
        print(f"no pipeline output yet: {args.reaper_output}")
        return 0

    gn = _load(args.ground_truth)
    out = _load(args.reaper_output)
    pairs = _pairs(gn, out)
    if args.max_functions > 0:
        pairs = pairs[:args.max_functions]
    print(f"scoring {len(pairs)} function-name pairs × {args.n_runs} runs "
          f"(one request at a time, minimal thinking)")
    cache = args.cache or str(Path(args.reaper_output).with_name("score_cache.jsonl"))
    llm = ReaperLLMClient(args.base_url, args.model, run_id="scorer")
    try:
        name_scores = await Scorer(llm, cache, args.n_runs).score_all(pairs)
    finally:
        await llm.close()

    report = _report(name_scores, len(pairs))
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2)
    print("\n=== LLM-JUDGE NAME SCORE (rubric, avg of "
          f"{args.n_runs}) ===")
    print(f"functions scored : {name_scores['functions']}")
    print(f"overall mean     : {name_scores['overall_mean']}")
    print(f"strict equivalent (>=8): {name_scores['strict_equivalent_rate']}%")
    print(f"related or better (>=5): {name_scores['related_or_better_rate']}%")
    print(f"failed/no rename (<0.5): {name_scores['failed_no_rename_rate']}%")
    print("worst 10:")
    for r in name_scores["worst"][:10]:
        print(f"  {r['key']}  {r['mean']:4.2f}  "
              f"{r['true_name'][:36]:<36} -> {r['recovered_name'][:36]:<36}")
    print(f"\nreport written to {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
