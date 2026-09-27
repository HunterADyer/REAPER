"""vLLM client wrapper — Deliverable 1.3.

Async async client that talks to the local vLLM server
(DeepSeek-V4-Flash on :8035). Manages multiple concurrent sessions, each
with its own in-memory conversation history, per-session asyncio.Lock, and
retry-with-backoff on transient errors.

Structured output is requested via the OpenAI-standard
``response_format={"type": "json_schema", "json_schema": {name, schema}}``.
Verified live (2026-09-27) on the shared :8035 vLLM build: the formerly
documented ``extra_body={"structured_outputs": ...}`` and ``guided_json`` are
SILENTLY IGNORED by this server and must not be used.
``max_completion_tokens`` is the TOTAL budget (thinking + response) driven by
``thinking_level``.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

log = logging.getLogger(__name__)

# thinking_level -> TOTAL max_completion_tokens budget (thinking + response).
_THINKING_BUDGETS = {
    "minimal": 1024,
    "low": 4096,
    "medium": 12288,
    "high": 20480,
    "max": 40960,
}

_DEFAULT_TIMEOUT = 120.0  # seconds per request
_BACKOFF_SECONDS = [1.0, 2.0, 4.0]


class LLMTransientError(Exception):
    """Raised after max retries on transient vLLM errors."""


class ReaperLLMClient:
    """Async client for the vLLM OpenAI-compatible endpoint.

    Session state (system prompt + conversation history) is kept CPU-side in
    memory. Each ``send()`` ships the full history plus the new message to
    ``/v1/chat/completions``. v1 usage is overwhelmingly single-turn.
    """

    def __init__(self, base_url: str, model: str, max_retries: int = 3):
        # Normalize to the SERVER ROOT. httpx APPENDS the request path
        # ("/v1/chat/completions") onto the configured base path, so a base_url
        # that already carries the OpenAI-SDK "/v1" suffix (as in
        # configs/default.toml) would resolve to a doubled, 404-prone
        # ".../v1/v1/chat/completions". Strip that suffix once here so the
        # explicit "/v1/..." path sent in `_post_chat` is always the single
        # OpenAI-compatible API path.
        self.base_url = base_url.rstrip("/")
        if self.base_url.endswith("/v1"):
            self.base_url = self.base_url[:-3]
        self.model = model
        self.max_retries = max_retries
        self._http = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=_DEFAULT_TIMEOUT,
        )
        # session_id -> dict(system_prompt=..., history=[...], lock=Lock)
        self._sessions: dict[str, dict[str, Any]] = {}

    # -- session management -------------------------------------------------

    async def create_session(self, session_id: str, system_prompt: str) -> str:
        """Create a new agent session with a system prompt. Returns session_id."""
        self._sessions[session_id] = {
            "system_prompt": system_prompt,
            "history": [],
            "lock": asyncio.Lock(),
        }
        return session_id

    def get_history(self, session_id: str) -> list[dict]:
        """Return full conversation history for a session (sync, CPU-only)."""
        sess = self._sessions[session_id]
        return [{"role": r["role"], "content": r["content"]} for r in sess["history"]]

    def destroy_session(self, session_id: str) -> None:
        """Clean up a session's state (sync, CPU-only)."""
        self._sessions.pop(session_id, None)

    # -- core LLM call ------------------------------------------------------

    async def send(
        self,
        session_id: str,
        message: str,
        thinking_level: str = "low",
        structured_output: dict | None = None,
    ) -> str:
        """Send a message in a session, return the response text.

        Raises:
            LLMTransientError: after ``max_retries`` transient failures.
        """
        sess = self._sessions.get(session_id)
        if sess is None:
            raise KeyError(f"no session '{session_id}' — call create_session() first")

        budget = _THINKING_BUDGETS.get(thinking_level)
        if budget is None:
            raise ValueError(
                f"unknown thinking_level {thinking_level!r}; "
                f"expected one of {sorted(_THINKING_BUDGETS)}"
            )

        async with sess["lock"]:
            messages = [
                {"role": "system", "content": sess["system_prompt"]},
                *sess["history"],
                {"role": "user", "content": message},
            ]

            payload = {
                "model": self.model,
                "messages": messages,
                "max_completion_tokens": budget,
            }
            if structured_output is not None:
                # Live :8035 vLLM only enforces the OpenAI-standard
                # response_format json_schema (verified 2026-09-27 by live
                # probe — see scripts/smoke_llm_endpoint.py); the formerly
                # documented extra_body={"structured_outputs": ...} and
                # guided_json are silently ignored by this build.
                title = str(structured_output.get("title", "response"))
                name = "".join(
                    ch if ch.isalnum() or ch in "_-" else "_" for ch in title
                ) or "response"
                payload["response_format"] = {
                    "type": "json_schema",
                    "json_schema": {"name": name, "schema": structured_output},
                }

            text = await self._send_with_retry(payload)

            # record conversation history (only on success)
            sess["history"].append({"role": "user", "content": message})
            sess["history"].append({"role": "assistant", "content": text})
            return text


    # -- internals ------------------------------------------------------------

    async def _send_with_retry(self, payload: dict) -> str:
        last_err: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                return await self._post_chat(payload)
            except (httpx.TimeoutException, httpx.ConnectError, httpx.ConnectTimeout) as e:
                last_err = e
                log.warning("transient vLLM connect/timeout error: %s", e)
            except httpx.HTTPStatusError as e:
                if 500 <= e.response.status_code < 600:
                    last_err = e
                    log.warning(
                        "vLLM server error %s: %s",
                        e.response.status_code,
                        e.response.text[:200],
                    )
                else:
                    # 4xx are fatal — not transient
                    raise
                # fall through to backoff

            if attempt < self.max_retries:
                delay = _BACKOFF_SECONDS[min(attempt, len(_BACKOFF_SECONDS) - 1)]
                log.info(
                    "retrying vLLM call in %.1fs (attempt %d/%d)",
                    delay, attempt + 1, self.max_retries,
                )
                await asyncio.sleep(delay)

        raise LLMTransientError(
            f"vLLM request failed after {self.max_retries} retries: {last_err}"
        ) from last_err

    async def _post_chat(self, payload: dict) -> str:
        resp = await self._http.post("/v1/chat/completions", json=payload)
        resp.raise_for_status()
        data = resp.json()
        try:
            return data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as e:
            raise RuntimeError(f"unexpected vLLM response shape: {data}") from e

    async def close(self) -> None:
        """Close the httpx.AsyncClient."""
        await self._http.aclose()

    # -- async context manager ------------------------------------------------

    async def __aenter__(self) -> "ReaperLLMClient":
        return self

    async def __aexit__(self, *exc_info) -> None:
        await self.close()

