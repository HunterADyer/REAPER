"""Process-local live event bus + emit helpers — instrumentation foundation.

An async pub/sub hub used to stream live run telemetry to two consumers:
the monitoring GUI (``reaper.gui``, WebSocket broadcast) and the durable
RL/tuning trace writer (``reaper.harness.rltrace``). Producers publish structured
events; consumers subscribe asynchronously. The bus is intentionally the ONLY
coupling between the pipeline and monitoring/tuning — producers never import
gui or rltrace code, so instrumentation stays modular (RL/tuning later can
subscribe to the same stream without touching the pipeline).

Publishing is fire-and-forget by design: a slow subscriber is dropped (oldest
event evicted from its queue) rather than blocking the pipeline. History is
bounded and only used to serve pull-based monitoring (``/api/events?after=``).

Note: the bus is process-local, which is why the GUI *hosts* the run
in-process instead of attaching cross-process.
"""

from __future__ import annotations

import asyncio
import itertools
import logging
import time
from collections import deque
from typing import Any

log = logging.getLogger(__name__)

_SEQ = itertools.count(1)


def _now() -> float:
    return time.time()


class EventBus:
    """Async broadcast hub. Each subscriber gets its own bounded queue."""

    def __init__(self, max_history: int = 20000, max_queue: int = 5000):
        self._subscribers: list[asyncio.Queue] = []
        self._history: deque[dict[str, Any]] = deque(maxlen=max_history)
        self._max_queue = max_queue
        self._lock = asyncio.Lock()

    @property
    def has_subscribers(self) -> bool:
        return bool(self._subscribers)

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=self._max_queue)
        self._subscribers.append(q)
        return q

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        try:
            self._subscribers.remove(queue)
        except ValueError:
            pass

    async def publish(self, event_type: str, session_id: str,
                      data: dict | None = None) -> None:
        """Broadcast one event to all subscribers (never raises, never blocks).

        ``data`` must be JSON-serializable (dict of str/int/float/bool/list/dict).
        """
        if data is not None and not isinstance(data, dict):
            raise TypeError(f"event data must be a dict, got {type(data)}")
        record: dict[str, Any] = {
            "seq": next(_SEQ),
            "ts": _now(),
            "event": event_type,
            "session": session_id,
        }
        if data is not None:
            record["data"] = data
        async with self._lock:
            self._history.append(record)
            subscribers = list(self._subscribers)
        for q in subscribers:
            try:
                q.put_nowait(record)
            except asyncio.QueueFull:
                # Never block the pipeline: drop the subscriber's oldest event
                # to keep the live feed current.
                try:
                    q.get_nowait()
                    q.put_nowait(record)
                except (asyncio.QueueEmpty, asyncio.QueueFull):
                    pass

    async def history_after(self, seq: int = 0, limit: int = 500) -> list[dict]:
        """Pull-based monitor view: events with ``seq > seq``, newest last."""
        async with self._lock:
            events = [e for e in self._history if e["seq"] > seq]
        return events[-limit:]


bus = EventBus()


async def emit(event_type: str, session_id: str = "system",
               data: dict | None = None) -> None:
    """Fire-and-forget publish that can never take down the pipeline."""
    try:
        await bus.publish(event_type, session_id, data)
    except Exception:  # noqa: BLE001 - telemetry must never break the run
        log.debug("event emit failed for %s", event_type, exc_info=True)


def emit_sync(event_type: str, session_id: str = "system",
              data: dict | None = None) -> None:
    """Synchronous fire-and-forget publish for non-async call sites.

    Used by SQLite trace callbacks and hot extractor readers that cannot
    ``await``. Schedules the publish on the running loop if one exists (the
    pipeline / GUI always run under an event loop); otherwise the event is
    dropped silently. Never raises.
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    if loop.is_running():
        try:
            loop.create_task(bus.publish(event_type, session_id, data))
        except Exception:  # noqa: BLE001
            log.debug("sync event emit failed for %s", event_type, exc_info=True)


__all__ = ["EventBus", "bus", "emit", "emit_sync"]
