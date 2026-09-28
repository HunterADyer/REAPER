"""Full debug event log — the durable "big logging" stream.

Subscribes to the process-local EventBus and appends EVERY event (dialogue,
thinking traces, tool/graph/SQL accesses, phases, claims, loops — whatever the
pipeline emits) as one JSON line per event to
``<data_dir>/<run_id>_events.jsonl``.

This is the exhaustive debugging record: strictly ordered, full payloads,
never truncated, safe to re-read after a crash. The :class:`~reaper.harness
.rltrace.RLTraceWriter` is a *cleaner* projection of just the LLM turns for
training data; this file is the firehose.
"""

from __future__ import annotations

import asyncio
import json
import logging

from reaper.harness.events import bus

log = logging.getLogger(__name__)

_SENTINEL = object()

# Event types deliberately excluded from the firehose (pure noise at the
# volume we care about; everything else is kept).
_SKIP = frozenset({
    "sql.access",      # extremely high volume; /api/events can still show them
})


class DebugLogWriter:
    """Appends every bus event to ``<data_dir>/<run_id>_events.jsonl``."""

    def __init__(self, data_dir: str, run_id: str):
        self.run_id = run_id or "default"
        self._path = f"{data_dir}/{self.run_id}_events.jsonl"
        self._queue = bus.subscribe()
        self._task: asyncio.Task | None = None

    @property
    def path(self) -> str:
        return self._path

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run(), name="debug-log")

    async def stop(self) -> None:
        if self._task is None:
            bus.unsubscribe(self._queue)
            return
        await self._queue.put(_SENTINEL)
        try:
            await asyncio.wait_for(self._task, timeout=5)
        except asyncio.TimeoutError:
            self._task.cancel()
            bus.unsubscribe(self._queue)

    async def _run(self) -> None:
        with open(self._path, "a", encoding="utf-8") as fh:
            while True:
                item = await self._queue.get()
                if item is _SENTINEL:
                    break
                if item.get("event") in _SKIP:
                    continue
                try:
                    fh.write(json.dumps(item, default=str) + "\n")
                    fh.flush()
                except Exception:  # noqa: BLE001
                    log.exception("debug-log write failed for %s", item.get("event"))


__all__ = ["DebugLogWriter"]
