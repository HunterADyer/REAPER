"""Shared test fixtures.

Provides two fake backends so harness deliverables (1.3-1.6, 3.3) can be
tested deterministically without real services:

- FakeNeo4jDriver  — minimal async driver that returns canned :Function
                     addresses (used by Ledger's Neo4j reads).
- FakeVLLMServer   — a real threaded HTTP server on an ephemeral port that
                     mimics the vLLM OpenAI-compatible endpoint, with
                     configurable failure behavior for retry tests.

Both are intentionally small and self-contained; they are NOT production code.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable


# ---------------------------------------------------------------------------
# Fake Neo4j driver
# ---------------------------------------------------------------------------

class _FakeAsyncResult:
    """Async-iterable of record dicts (mimics neo4j AsyncResult).

    The real Neo4j ``AsyncResult`` supports ``await result`` (returning
    itself) *and* ``async for`` iteration — the harness relies on this
    two-way contract (e.g. ``res = await session.run(...)`` then
    ``[rec async for rec in res]``). We mirror both here.
    """

    def __init__(self, records: list[dict]):
        self._records = list(records)

    def __await__(self):
        """`await result` returns the result itself (neo4j AsyncResult)."""
        if False:  # pragma: no cover - unreachable; makes this a generator
            yield
        return self

    def __aiter__(self):
        return self

    async def __anext__(self) -> dict:
        try:
            return self._records.pop(0)
        except IndexError:
            raise StopAsyncIteration


class _FakeAsyncSession:
    def __init__(self, address_map: dict[str, dict]):
        self._address_map = address_map

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def run(self, query: str, **params) -> _FakeAsyncResult:
        if "Function" in query:
            return _FakeAsyncResult(
                [{"address": addr} for addr in sorted(self._address_map)]
            )
        return _FakeAsyncResult([])


class FakeNeo4jDriver:
    """Canned async driver: ``session().run()`` returns function addresses."""

    def __init__(self, function_addresses: list[str]):
        self._address_map = {addr: {"address": addr} for addr in function_addresses}

    def session(self) -> _FakeAsyncSession:
        return _FakeAsyncSession(self._address_map)

    async def close(self) -> None:
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


# ---------------------------------------------------------------------------
# Fake vLLM server (real HTTP, deterministic)
# ---------------------------------------------------------------------------

class _FakeVLLMHandler(BaseHTTPRequestHandler):
    # Class-level config mutated before starting the server thread.
    responses_served = 0
    fail_first_n = 0          # how many requests return 503 before success
    fail_status: int = 503
    content_builder: Callable[[dict], str] = lambda body: "4"  # default answer
    payloads: list[dict] = []  # every request body captured for assertions

    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # silence request logging
        pass

    def _send(self, status: int, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):  # noqa: N802 (BaseHTTPRequestHandler API)
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError:
            body = {}
        self.__class__.payloads.append(body)

        self.__class__.responses_served += 1
        if self.__class__.responses_served <= self.__class__.fail_first_n:
            self._send(self.__class__.fail_status, b'{"error": "simulated failure"}')
            return

        content = self.__class__.content_builder(body)
        payload = json.dumps(
            {"choices": [{"message": {"content": content}}]},
            ensure_ascii=False,
        ).encode("utf-8")
        self._send(200, payload)


class FakeVLLMServer:
    """Run a fake vLLM endpoint in a background thread.

    Usage:
        server = FakeVLLMServer()
        server.fail_first_n = 2   # must be set BEFORE start()
        server.start()
        client = ReaperLLMClient(server.base_url, model="fake-model", max_retries=3)
        ...
        server.stop()
    """

    def __init__(self):
        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), _FakeVLLMHandler)
        self.port = self._httpd.server_address[1]
        self.base_url = f"http://127.0.0.1:{self.port}"
        self._thread = None



    # -- pass-through config for the handler --------------------------------

    @property
    def fail_first_n(self) -> int:
        return _FakeVLLMHandler.fail_first_n

    @fail_first_n.setter
    def fail_first_n(self, value: int) -> None:
        if self._thread is not None and self._thread.is_alive():
            raise RuntimeError("set fail_first_n before starting the server")
        _FakeVLLMHandler.fail_first_n = value

    @property
    def content_builder(self) -> Callable[[dict], str]:
        return _FakeVLLMHandler.content_builder

    @content_builder.setter
    def content_builder(self, fn: Callable[[dict], str]) -> None:
        _FakeVLLMHandler.content_builder = fn

    @property
    def payloads(self) -> list[dict]:
        return _FakeVLLMHandler.payloads

    @property
    def responses_served(self) -> int:
        return _FakeVLLMHandler.responses_served

    # -- lifecycle ------------------------------------------------------------

    def start(self) -> None:
        _FakeVLLMHandler.responses_served = 0
        _FakeVLLMHandler.payloads = []
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)


# ---------------------------------------------------------------------------
# Recording Neo4j driver (additive — used by the 2.2-2.5 graph tool tests)
# ---------------------------------------------------------------------------

class _RecordedResult:
    """Async-iterable of record dicts; also awaitable (mirrors AsyncResult)."""

    def __init__(self, records: list[dict]):
        self._records = list(records)

    def __await__(self):
        if False:  # pragma: no cover - makes this a generator
            yield
        return self

    def __aiter__(self):
        return self

    async def __anext__(self) -> dict:
        try:
            return self._records.pop(0)
        except IndexError:
            raise StopAsyncIteration


class _RecordedAsyncSession:
    def __init__(self, driver):
        self._driver = driver

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def run(self, query: str, parameters: dict | None = None, **kwparameters):
        """Mirror AsyncSession.run(): params may arrive as a positional dict
        (builders do this) or as kwargs (analysis _collect does this)."""
        params = dict(parameters or {})
        params.update(kwparameters)
        self._driver.queries.append((query, params))
        if self._driver.responder is not None:
            records = self._driver.responder(query, params) or []
            return _RecordedResult(records)
        return _RecordedResult([])


class RecordingNeo4jDriver:
    """Async driver that records every run() for graph-builder tests.

    ``responder(query, params) -> list[dict]`` is optional; when provided its
    records are returned for the matching query (used to fake read results in
    the graph_analysis tests). Every call, mutation or not, is recorded in
    ``queries`` so tests can assert on the emitted Cypher + parameters.
    """

    def __init__(self, responder=None):
        self.responder = responder
        self.queries: list[tuple[str, dict]] = []

    def session(self):
        return _RecordedAsyncSession(self)

    async def close(self) -> None:
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def queries_matching(self, fragment: str) -> list[tuple[str, dict]]:
        """Filter recorded queries to those containing ``fragment``."""
        return [q for q in self.queries if fragment in q[0]]
