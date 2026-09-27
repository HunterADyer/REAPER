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

log = logging.getLogger(__name__)

_ALLOWED_EVENT_TYPES = {
    "llm_request",
    "llm_response",
    "claim_submit",
    "critic_feedback",
    "merge_attempt",
    "merge_result",
    "rename",
    "task_create",
    "task_complete",
    "graph_mutation",
    "pipeline_complete",
    "investigation_stuck",
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

        Raises:
            ValueError: if ``event_type`` is not in :data:`_ALLOWED_EVENT_TYPES`.
        """
        if event_type not in _ALLOWED_EVENT_TYPES:
            raise ValueError(
                f"unknown trace event type {event_type!r}; "
                f"expected one of {sorted(_ALLOWED_EVENT_TYPES)}"
            )
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

    async def close(self) -> None:
        """Flush and close the trace file (async; call at end of run)."""
        try:
            self._file.flush()
            self._file.close()
        except Exception:
            pass

