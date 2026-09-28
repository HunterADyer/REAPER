"""Structured JSON trace logging — Deliverable 1.4.

One JSONL file per run at ``<log_dir>/trace.jsonl``. Every ``log()`` call
acquires an asyncio.Lock around the file handle write+flush so concurrent
agents never interleave or corrupt lines.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from reaper.harness.events import emit

log = logging.getLogger(__name__)

# Complete documented trace vocabulary. Logging an event outside this set no
# longer raises — it is warned about and dropped (see `Tracer.log`), so a
# mis-registered or future event can never crash a live pipeline run. The
# entries below are the union of every event type currently emitted across
# run.py, the harness dispatchers/loops, and the agents, plus the originally
# reserved vocabulary.
_ALLOWED_EVENT_TYPES = {
    # run orchestrator
    "pipeline_complete", "pipeline_phases_done",
    # Pass 1 / Pass 2 dispatchers
    "pass1_no_functions", "pass1_scc_done", "pass1_level_done",
    "pass2_no_functions", "pass2_level_done", "pass2_review_retry",
    "pass2_function_done",
    # scheduler / resynthesis / investigation loops
    "scheduler_reviewed", "scheduler_stale_reset", "scheduler_assigned",
    "resynthesis_iteration", "resynthesis_agent", "completion_coverage",
    "investigation_complete", "investigation_stuck",
    "investigation_iteration_cap", "investigation_agent",
    # Pass 1 rename / Pass 2 review / critic agents
    "review_agent", "rename_agent",
    "claim_accepted", "claim_force_accepted", "claim_rejected",
    # reserved vocabulary (not currently emitted; documented for future wiring)
    "llm_request", "llm_response", "claim_submit", "critic_feedback",
    "merge_attempt", "merge_result", "rename",
    "task_create", "task_complete", "graph_mutation",
}


class Tracer:
    """Appends JSON lines to a per-run trace file, serialized by a lock."""

    def __init__(self, log_dir: str):
        self.log_dir = str(log_dir)
        Path(log_dir).mkdir(parents=True, exist_ok=True)
        self._path = Path(log_dir) / "trace.jsonl"
        self._file = self._path.open("a", encoding="utf-8")
        self._lock = asyncio.Lock()

    async def log(self, event_type: str, session_id: str, data: dict) -> None:
        """Append a JSON line to the trace log (locked, flushed immediately).

        Unknown ``event_type`` values are warned about and dropped rather than
        raised — tracing must never take down a live run. Keep any new event
        type in :data:`_ALLOWED_EVENT_TYPES` so the warning stays silent in
        normal operation.
        """
        if event_type not in _ALLOWED_EVENT_TYPES:
            log.warning(
                "dropping trace record for unknown event type %r — add it to "
                "Tracer._ALLOWED_EVENT_TYPES (currently %d documented types)",
                event_type, len(_ALLOWED_EVENT_TYPES),
            )
            return
        record = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "event": event_type,
            "session": session_id,
            "data": data if data is not None else {},
        }
        line = json.dumps(record, default=str)
        async with self._lock:
            try:
                self._file.write(line + "\n")
                self._file.flush()
            except Exception:
                log.exception("tracer write failed for %s", event_type)
        # Mirror into the live event bus (GUI / RL / debug log). Fire-and-forget:
        # telemetry must never take down the pipeline.
        await emit(event_type, session_id, data if data is not None else {})

    async def close(self) -> None:
        """Flush and close the trace file (async; call at end of run)."""
        try:
            self._file.flush()
            self._file.close()
        except Exception:
            pass

