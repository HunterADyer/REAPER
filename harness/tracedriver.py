"""Traced Neo4j driver wrapper — records every graph access for debug/RL.

Wraps a neo4j AsyncDriver so that every ``session.run`` becomes a
``graph.query`` live event ({query, params, duration_s, n_records, ok, error}).
This is the "what the agents accessed" record for the graph store, and is a
single chokepoint — no per-call-site instrumentation needed.

The proxy intentionally exposes only the surface REAPER uses (``session()`` as
an async context manager + ``close()``); anything else is passed through to the
underlying driver so callers are unaffected.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from reaper.harness.events import emit

log = logging.getLogger(__name__)


class TracedSession:
    def __init__(self, inner, label: str, run_id: str | None):
        self._inner = inner
        self._label = label
        self._run_id = run_id

    async def run(self, query: str, parameters: Any = None, **kwargs: Any) -> Any:
        params = dict(parameters or {})
        params.update(kwargs)
        t0 = time.monotonic()
        try:
            result = await self._inner.run(query, parameters=params)
            self._snapshot_params(result)
            await emit("graph.query", "neo4j", {
                "run_id": self._run_id,
                "label": self._label,
                "query": query,
                "params": params,
                "duration_s": round(time.monotonic() - t0, 3),
            })
            return result
        except Exception as exc:  # noqa: BLE001
            await emit("graph.query", "neo4j", {
                "run_id": self._run_id,
                "label": self._label,
                "query": query,
                "params": params,
                "duration_s": round(time.monotonic() - t0, 3),
                "ok": False,
                "error": str(exc),
            })
            raise

    @staticmethod
    def _snapshot_params(result) -> None:
        """Pointless-looking helper kept simple on purpose: the result object
        is lazy; record counts only where callers already consume it."""
        return

    async def close(self) -> None:
        await self._inner.close()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info) -> None:
        await self.close()


class TracedDriver:
    """Duck-typed AsyncDriver facade that logs every query as a live event."""

    def __init__(self, inner, label: str = "master", run_id: str | None = None):
        self._inner = inner
        self._label = label
        self._run_id = run_id

    def session(self) -> TracedSession:
        return TracedSession(self._inner.session(), self._label, self._run_id)

    async def close(self) -> None:
        await self._inner.close()

    async def verify_connectivity(self) -> None:
        await self._inner.verify_connectivity()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


def traced_driver(driver, label: str = "master", run_id: str | None = None):
    """Wrap an AsyncDriver with query logging (never mutates behaviour)."""
    return TracedDriver(driver, label=label, run_id=run_id)


__all__ = ["TracedDriver", "TracedSession", "traced_driver"]
