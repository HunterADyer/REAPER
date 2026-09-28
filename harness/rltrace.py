"""RL / tuning trace writer — durable, modular capture of every LLM turn.

Subscribes to the process-local :class:`~reaper.harness.events.EventBus` and
appends one JSON line per completed LLM call to
``<data_dir>/<run_id>_rltrace.jsonl``. Each record is self-contained (the exact
prompt as sent, the assistant content, the model's *thinking* trace when
present, token usage, timing, retries) so the file is directly usable as an
SFT / preference-tuning / RL-corpus dataset — no joining with other state.

Decoupled on purpose: the LLM client only publishes ``llm.response`` /
``llm.error`` events; this writer (and any future RL consumer — reward-model
training, rollout capture, etc.) subscribes independently. The writer never
raises into the pipeline: any failure appends nothing and is logged.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from reaper.harness.events import bus

log = logging.getLogger(__name__)

_SENTINEL = object()


def _iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class RLTraceWriter:
    """One durable JSONL file per run containing every LLM turn's full record."""

    def __init__(self, data_dir: str, run_id: str):
        self.run_id = run_id or "default"
        path = Path(data_dir) / f"{self.run_id}_rltrace.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        self._path = str(path)
        self._queue = bus.subscribe()
        self._task: asyncio.Task | None = None

    @property
    def path(self) -> str:
        return self._path

    def start(self) -> None:
        """Begin consuming events in a background task (idempotent)."""
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run(), name="rltrace-writer")

    async def stop(self) -> None:
        """Stop the consumer, flush pending lines, close the subscription."""
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
                try:
                    bucket = item.get("event")
                    if bucket not in ("llm.response", "llm.error"):
                        continue
                    rec = self._build(item)
                    if rec is not None:
                        fh.write(json.dumps(rec, default=str) + "\n")
                        fh.flush()
                except Exception:  # noqa: BLE001
                    log.exception("rltrace write failed for %s", item.get("event"))

    @staticmethod
    def _build(event: dict) -> dict | None:
        data = event.get("data") or {}
        if not data.get("messages"):
            return None
        out = {
            "ts": _iso(),
            "seq": event.get("seq"),
            "run_id": data.get("run_id"),
            "session_id": event.get("session"),
            "thinking_level": data.get("thinking_level"),
            "max_completion_tokens": data.get("max_completion_tokens"),
            "timeout_s": data.get("timeout_s"),
            "n_retries": data.get("n_retries", 0),
            "duration_s": data.get("duration_s"),
            "status": event.get("event") == "llm.response" and "ok" or "error",
            "messages": data.get("messages"),
            "response": data.get("content"),
            "reasoning": data.get("reasoning"),
            "usage": data.get("usage"),
        }
        if event.get("event") == "llm.error":
            out["error"] = data.get("error")
        return out


__all__ = ["RLTraceWriter"]
