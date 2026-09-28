"""JudgeEngine — N independent LLM-judge scoring runs with resumable cache.

THE independence contract (user requirement, 2026-09-27): a "run" is ONE
fresh request to the vLLM endpoint with NO context and NO history. Each
(score item, run) pair gets a brand-new session created right before the call
and destroyed immediately after, and the message sent is the rubric-built
single-turn question only. Nothing leaks between runs or between items.

Two hard properties the engine guarantees and tests lock down:

  1. EVERY (item, run) is a fresh session, no matter what. The LLM client's
     per-session history reset is irrelevant — we never reuse a session.
  2. Strictly ONE request at a time. The engine does not fan out; the shared
     :8035 endpoint is serialized (the LLM client ALSO gates at 1 globally,
     but the engine never depends on that — it issues sequentially).

Results are cached in a JSONL file keyed by (rubric_id, item_key, run) so an
interrupted scoring session resumes exactly where it stopped. Cached rows are
never re-scored.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, Field

from reaper.harness.llm_client import ReaperLLMClient
from reaper.harness.submission import get_schema, parse_response
from reaper.eval.scoring.rubrics import Rubric, get_rubric

log = logging.getLogger(__name__)


class JudgeScore(BaseModel):
    score: int = Field(ge=0, le=10, description="rubric score 0-10")
    justification: str = Field(..., description="one short sentence")


def _pack(cache_line: dict) -> JudgeScore:
    return JudgeScore(**cache_line["result"])


class JudgeEngine:
    """Scores items against a rubric in N independent zero-context runs."""

    def __init__(self, llm: ReaperLLMClient, n_runs: int = 5,
                 cache_path: str | None = None):
        self.llm = llm
        self.n_runs = max(1, int(n_runs))
        self.cache_path = cache_path
        self._cache: dict[str, JudgeScore] = {}
        if cache_path and os.path.exists(cache_path):
            self._load_cache(cache_path)

    # -- cache ----------------------------------------------------------------

    def _load_cache(self, path: str) -> None:
        try:
            with open(path, "r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                        # Legacy pre-scoring refactor cache format:
                        #   {"address": .., "run": .., "result": {..}}
                        #   (rubric implied = "function-name").
                        rubric_id = rec.get("rubric", "function-name")
                        item_key = rec.get("item", rec.get("address"))
                        if item_key is None:
                            continue
                        self._cache[self._key(rubric_id, item_key, rec["run"])] = (
                            _pack(rec)
                        )
                    except Exception:
                        continue
        except OSError:
            pass

    @staticmethod
    def _key(rubric_id: str, item_key: str, run: int) -> str:
        return f"{rubric_id}::{item_key}::r{run}"

    def _append_cache(self, rubric_id: str, item_key: str, run: int,
                      score: JudgeScore) -> None:
        if not self.cache_path:
            return
        try:
            with open(self.cache_path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps({
                    "rubric": rubric_id,
                    "item": item_key,
                    "run": run,
                    "persisted_at": __import__("datetime").datetime.now(
                        __import__("datetime").timezone.utc).isoformat(),
                    "result": score.model_dump(),
                }, default=str) + "\n")
        except OSError:
            log.debug("judge cache write failed for %s", item_key)

    # -- scoring --------------------------------------------------------------

    async def rate(self, rubric: Rubric, item: dict, run: int) -> JudgeScore:
        """Score ONE (item, run) as a single fresh zero-context query.

        Session discipline: create_session -> ONE send (system prompt + the
        rubric-built question; NO history) -> destroy_session. This is the
        documented "independent" contract — see module docstring.
        """
        key = self._key(rubric.id, item["key"], run)
        cached = self._cache.get(key)
        if cached is not None:
            return cached

        question = rubric.format_question(item)
        session_id = f"judge::{rubric.id}::{item['key']}::r{run}"
        # Fresh session per (item, run) — zero context, by contract.
        await self.llm.create_session(
            session_id, rubric.prompt or "You are a strict, fair grading judge."
        )
        try:
            raw = await self.llm.send(
                session_id, question, thinking_level="minimal",
                structured_output=get_schema(JudgeScore),
            )
            score = parse_response(JudgeScore, raw)
        finally:
            self.llm.destroy_session(session_id)

        self._cache[key] = score
        self._append_cache(rubric.id, item["key"], run, score)
        return score

    async def score_item(self, rubric: Rubric, item: dict) -> dict:
        """Run the N independent runs for ONE item, aggregate them."""
        scores = []
        justifications = []
        for run in range(self.n_runs):
            r = await self.rate(rubric, item, run)
            scores.append(r.score)
            justifications.append(r.justification)
        mean = sum(scores) / len(scores)
        return {
            "key": item["key"],
            "scores": scores,
            "mean": round(mean, 2),
            "min": min(scores),
            "max": max(scores),
            "std": round(
                (sum((s - mean) ** 2 for s in scores) / len(scores)) ** 0.5, 2
            ),
            "justifications": justifications,
        }

    async def score_items(self, rubric: Rubric, items: list[dict]) -> list[dict]:
        """Score every item under one rubric (N runs each, sequentially)."""
        rows = []
        for item in items:
            rows.append(await self.score_item(rubric, item))
        return rows


async def score_items(llm: ReaperLLMClient, rubric_id: str, items: list[dict],
                      n_runs: int = 5, cache_path: str | None = None) -> list[dict]:
    """Convenience wrapper: look up the rubric and score items with a fresh
    engine. Returns per-item rows (NOT aggregated)."""
    rubric = get_rubric(rubric_id)
    engine = JudgeEngine(llm, n_runs=n_runs, cache_path=cache_path)
    return await engine.score_items(rubric, items)


__all__ = ["JudgeEngine", "JudgeScore", "score_items"]
