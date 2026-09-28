"""Scoring aggregation math — shared by every consumer.

Given per-item rows from the JudgeEngine (each row = {key, scores[], mean,
min, max, std}), this module computes the thresholds-based aggregates used by
all reports: overall mean, strict-equivalent rate, related-or-better rate,
failed/no-rename rate, and a worst-first ordering. Pure functions only —
callers are responsible for I/O (caching, report writing).
"""

from __future__ import annotations

from statistics import mean as _mean

# Thresholds (rubric-band-agnostic by design; the 0-10 bands map onto these):
#   >= 8  : semantically equivalent
#   >= 5  : related / partially captures the role
#   <  0.5: failed / no rename (mean ~= 0)
EQUIV_GE = 8.0
RELATED_GE = 5.0
FAILED_LT = 0.5


def aggregate_rows(rows: list[dict], item_label: str = "items") -> dict:
    """Aggregate per-item judge rows into a report section.

    ``rows`` must be list of dicts from ``JudgeEngine.score_items`` (each with
    ``key`` and ``mean``). Returns a dict with overall + rates + worst list.
    """
    if not rows:
        return {"count": 0, "overall_mean": 0.0, "worst": []}
    overall = sum(r["mean"] for r in rows) / len(rows)
    strict = sum(1 for r in rows if r["mean"] >= EQUIV_GE)
    related = sum(1 for r in rows if r["mean"] >= RELATED_GE)
    failed = sum(1 for r in rows if r["mean"] < FAILED_LT)
    return {
        "count": len(rows),
        "overall_mean": round(overall, 2),
        "strict_equivalent_rate": round(100.0 * strict / len(rows), 1),
        "related_or_better_rate": round(100.0 * related / len(rows), 1),
        "failed_no_rename_rate": round(100.0 * failed / len(rows), 1),
        "worst": sorted(rows, key=lambda r: r["mean"])[:10],
    }


def aggregate_report(sections: dict[str, dict]) -> dict:
    """Assemble a full multi-dimension report. Each section must already be a
    value returned by :func:`aggregate_rows` (keyed by dimension id)."""
    return {
        "dimensions": sections,
        "totals": {
            dim: {
                "n": sec.get("count", 0),
                "mean": sec.get("overall_mean", 0.0),
            }
            for dim, sec in sections.items()
        },
    }


__all__ = ["aggregate_rows", "aggregate_report", "EQUIV_GE", "RELATED_GE", "FAILED_LT"]
