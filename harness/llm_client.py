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
import time
from typing import Any

import httpx

from reaper.harness.events import emit

log = logging.getLogger(__name__)

# thinking_level -> TOTAL max_completion_tokens budget (thinking + response).
_THINKING_BUDGETS = {
    "minimal": 1024,
    "low": 4096,
    "medium": 12288,
    "high": 20480,
    "max": 40960,
    # xhigh: extra-extra thinking tier. DeepSeek reasoning depth is
    # BUDGET-DRIVEN (reasoning tokens count inside max_completion_tokens), so
    # more budget => the model thinks harder/longer. 81920 is comfortably
    # within the :8035 model's 524288-token window even with a max context.
    "xhigh": 81920,
}

# Optional per-level request knobs sent to the live vLLM (safe: only added for
# levels listed; vLLM honors `chat_template_kwargs.reasoning_effort` for
# DeepSeek — verified live 2026-09-27 it is accepted with status 200). Levels
# NOT listed are unchanged (no behavior regression).
_REASONING_EFFORT_BY_LEVEL = {"xhigh": "high"}

# Per-request read timeout by thinking level. Reasoning models can legitimately
# think for many minutes at high/max budgets; a flat short timeout aborts LIVE
# runs (observed 2026-09-27: type-recovery at 'high' > 120s -> LLMTransientError
# despite a healthy endpoint). Scale the timeout with the thinking budget so a
# genuinely-stuck request is still never waited on past ~25 min.
_TIMEOUT_BY_LEVEL = {
    "minimal": 180.0,
    "low": 300.0,
    "medium": 480.0,
    "high": 720.0,
    "max": 900.0,
    "xhigh": 1500.0,
}
_DEFAULT_TIMEOUT = 600.0  # fallback for unknown/legacy levels

_BACKOFF_SECONDS = [5.0, 10.0, 20.0]


class LLMTransientError(Exception):
    """Raised after max retries on transient vLLM errors."""


class ReaperLLMClient:
    """Async client for the vLLM OpenAI-compatible endpoint.

    Session state (system prompt + conversation history) is kept CPU-side in
    memory. Each ``send()`` ships the full history plus the new message to
    ``/v1/chat/completions``. v1 usage is overwhelmingly single-turn.
    """

    def __init__(self, base_url: str, model: str, max_retries: int = 3,
                 run_id: str | None = None, max_concurrent: int = 1):
        # Serialize all requests through ONE global gate. Justified hard limit
        # (2026-09-27, user requirement): the :8035 vLLM is shared with other
        # jobs and a burst of concurrent requests risks dropping the GPU worker
        # "off the bus". Even though dispatchers fan out up to
        # max_concurrent_agents tasks, the actual HTTP requests are strictly
        # one-at-a-time here; CPU-side context assembly still overlaps freely.
        self._gate = asyncio.Semaphore(max(1, int(max_concurrent)))
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
        self.run_id = run_id
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
            effort = _REASONING_EFFORT_BY_LEVEL.get(thinking_level)
            if effort:
                # Ask the live vLLM for extra reasoning depth on this turn.
                # Only sent for levels that request it; ignored harmlessly by
                # builds that don't implement the knob.
                payload["chat_template_kwargs"] = {"reasoning_effort": effort}

            timeout_s = _TIMEOUT_BY_LEVEL.get(thinking_level, _DEFAULT_TIMEOUT)
            await emit("llm.request", session_id, {
                "run_id": self.run_id,
                "thinking_level": thinking_level,
                "max_completion_tokens": budget,
                "timeout_s": timeout_s,
                "message_chars": sum(len(m.get("content") or "") for m in messages),
                "n_messages": len(messages),
            })

            # Strictly one request at a time across ALL sessions (global gate):
            # a queued request's llm.request event was already emitted above, so
            # the GUI sees the true backlog (llm_pending includes queued turns).
            async with self._gate:
                try:
                    result = await self._send_with_retry(payload, timeout_s)
                except LLMTransientError as exc:
                    await emit("llm.error", session_id, {
                        "run_id": self.run_id,
                        "thinking_level": thinking_level,
                        "max_completion_tokens": budget,
                        "error": str(exc),
                    })
                    raise

                text = result["content"]
                # record conversation history (only on success)
                sess["history"].append({"role": "user", "content": message})
                sess["history"].append({"role": "assistant", "content": text})

                # Live turn telemetry + durable RL/tuning record (captures the
                # model's ``reasoning`` thinking trace and token usage).
                await emit("llm.response", session_id, {
                    "run_id": self.run_id,
                    "thinking_level": thinking_level,
                    "max_completion_tokens": budget,
                    "timeout_s": timeout_s,
                    "messages": messages,
                    "content": text,
                    "reasoning": result.get("reasoning"),
                    "usage": result.get("usage"),
                    "duration_s": result.get("duration_s"),
                    "n_retries": result.get("n_retries", 0),
                })
            return text


    # -- internals ------------------------------------------------------------

    async def _send_with_retry(self, payload: dict, timeout_s: float) -> dict:
        """POST with retry/backoff on transient 5xx/timeouts. Returns a dict
        with ``content``, ``reasoning`` (thinking trace, may be None), ``usage``
        (token breakdown when the server reports it), ``n_retries`` and
        ``duration_s``."""
        last_err: Exception | None = None
        n_retries = 0
        t0 = time.monotonic()
        for attempt in range(self.max_retries + 1):
            try:
                content, reasoning, usage = await self._post_chat(payload, timeout_s)
                return {
                    "content": content,
                    "reasoning": reasoning,
                    "usage": usage,
                    "n_retries": n_retries,
                    "duration_s": round(time.monotonic() - t0, 3),
                }
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
                n_retries += 1
                delay = _BACKOFF_SECONDS[min(attempt, len(_BACKOFF_SECONDS) - 1)]
                log.info(
                    "retrying vLLM call in %.1fs (attempt %d/%d)",
                    delay, attempt + 1, self.max_retries,
                )
                await asyncio.sleep(delay)

        raise LLMTransientError(
            f"vLLM request failed after {self.max_retries} retries: {last_err}"
        ) from last_err

    async def _post_chat(self, payload: dict, timeout_s: float) -> tuple:
        resp = await self._http.post(
            "/v1/chat/completions", json=payload, timeout=timeout_s)
        resp.raise_for_status()
        data = resp.json()
        try:
            choice = data["choices"][0]
            message = choice.get("message") or {}
            content = message.get("content") or ""
        except (KeyError, IndexError, TypeError) as e:
            raise RuntimeError(f"unexpected vLLM response shape: {data}") from e

        # Thinking trace: vLLM returns the model's reasoning on the message
        # (verified live 2026-09-27: ``message.reasoning``) — some builds use
        # ``reasoning_content`` or the OpenAI ``reasoning`` field. Capture
        # whatever variant is present (None when the model does not think).
        reasoning = (message.get("reasoning") or message.get("reasoning_content")
                     or choice.get("reasoning_content") or choice.get("reasoning")
                     or None)
        if isinstance(reasoning, (list, dict)):  # defensive: some SDK shapes
            reasoning = str(reasoning)
        usage = data.get("usage")
        return content, reasoning, usage

    async def close(self) -> None:
        """Close the httpx.AsyncClient."""
        await self._http.aclose()

    # -- async context manager ------------------------------------------------

    async def __aenter__(self) -> "ReaperLLMClient":
        return self

    async def __aexit__(self, *exc_info) -> None:
        await self.close()

