"""ScoreScheduler — take evidence, score it with the rubric, N independent runs.

This is the orchestrator that ties the scoring pipeline together. Following
the user's design (2026-09-27): the scheduler does NOT parse the .bndb or
call tools — it receives the *evidence* (ground-truth side + recovered side,
already reduced to flat structures) and just schedules every item through the
appropriate rubric for N INDEPENDENT zero-context scoring runs, then
aggregates.

Typical use (see eval/scoring/cli.py for the runnable wrapper):

    scheduler = ScoreScheduler(llm, n_runs=5, cache_path=...)
    items = all_dimensions(ground_truth, recovered_output)   # align.py
    report = await scheduler.score(items, dimensions=["function-name", ...])
"""

from __future__ import annotations

import logging
from typing import Any

from reaper.eval.scoring.judge import JudgeEngine
from reaper.eval.scoring.reporting import aggregate_report, aggregate_rows
from reaper.eval.scoring.rubrics import get_rubric

log = logging.getLogger(__name__)


class ScoreScheduler:
    """Scores evidence across one or more rubric dimensions, N runs each."""

    #: Order of dimensions for stable, human-readable report output.
    DEFAULT_DIMENSION_ORDER = ("function-name", "variable-name", "datatype")

    def __init__(self, llm, n_runs: int = 5, cache_path: str | None = None):
        self.llm = llm
        self.n_runs = max(1, int(n_runs))
        self.cache_path = cache_path
        self._engine = JudgeEngine(
            llm, n_runs=self.n_runs, cache_path=cache_path)

    async def score(self, items_by_dimension: dict[str, list[dict]],
                    dimensions: list[str] | None = None) -> dict:
        """Score supplied items. ``items_by_dimension`` maps dimension id ->
        list of items (usually from align.all_dimensions). Returns the
        aggregated report plus per-item rows under ``rows``."""
        if items_by_dimension is None:
            items_by_dimension = {}
        requested = dimensions or list(self.DEFAULT_DIMENSION_ORDER)
        sections: dict[str, dict] = {}
        rows_out: dict[str, list[dict]] = {}
        for dim in requested:
            rubric = get_rubric(dim)
            items = list(items_by_dimension.get(dim) or [])
            rows = await self._engine.score_items(rubric, items) if items else []
            rows_out[dim] = rows
            sections[dim] = aggregate_rows(rows, item_label=dim)
            log.info(
                "scored dimension %s: %d item(s), mean %.2f",
                dim, len(items), sections[dim].get("overall_mean", 0.0),
            )
        report = aggregate_report(sections)
        report["rows"] = rows_out
        return report

    async def close(self) -> None:
        try:
            await self.llm.close()
        except Exception:  # noqa: BLE001
            pass


__all__ = ["ScoreScheduler"]
