"""Modular LLM-judge scoring harness.

Subpackage split (each piece is independently reusable by eval/heavy and the
live scoring CLIs):

    rubrics.py   —— rubric registry: named, versioned rubrics per scoring
                    dimension (function-name, variable-name, struct-field,
                    claim-truth, conclusion). Each rubric fully defines its
                    prompt template + 0-10 bands + response schema hint.
    judge.py     —— JudgeEngine: score a set of items against a rubric by
                    making N INDEPENDENT scoring queries (each is a fresh,
                    zero-context single-turn call; no history leaks between
                    runs). Resumable JSONL cache keyed by (rubric, item, run).
    reporting.py —— aggregation math over N runs (mean/std/rates) shared by
                    every consumer so no caller ever re-implements it.
    align.py     —— entity alignment (functions by address, struct fields by
                    offset, claims by id) between ground truth and recovered
                    sides, with deterministic ordering + cache keys.
    bndbread.py  —— read back REAPER's recovered output from the SAVED .bndb
                    by calling the existing tools layer (never bespoke
                    parsing): function names (canon/llm tags), variable
                    renames, and user struct types.
    cli.py       —— ``python -m reaper.eval.scoring`` entry: name + datatype
                    dimensions over bndb-derived and/or reaper_output data.

Contract used by every consumer: whatever you are scoring, reduce it to a
list of *items* (dicts carrying ``key`` + at least the fields your rubric's
question template references) and hand them to ``JudgeEngine.score_items``.
The engine guarantees each run is an independent fresh query (per-item,
per-run session created and destroyed around one send call) — this is the
"no context or history between the 5 scoring runs" control.
"""

from reaper.eval.scoring.rubrics import Rubric, RubricLevel, RUBRICS, get_rubric
from reaper.eval.scoring.judge import JudgeEngine, JudgeScore, score_items
from reaper.eval.scoring.reporting import aggregate_rows, aggregate_report

__all__ = [
    "Rubric",
    "RubricLevel",
    "RUBRICS",
    "get_rubric",
    "JudgeEngine",
    "JudgeScore",
    "score_items",
    "aggregate_rows",
    "aggregate_report",
]
