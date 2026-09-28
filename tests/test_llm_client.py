"""Tests for Deliverable 1.3 — ReaperLLMClient.

Runs against a real threaded fake-vLLM HTTP server (see conftest) so the
retry/backoff logic is exercised end-to-end without a real vLLM instance.

Backoff sleeps are neutralized by monkeypatching the module-level
``_BACKOFF_SECONDS`` to [0.0, 0.0, 0.0].
"""

from __future__ import annotations

import pytest

import httpx
import reaper.harness.llm_client as llm_module
from reaper.harness.llm_client import LLMTransientError, ReaperLLMClient

from conftest import FakeVLLMServer


@pytest.fixture
def zero_backoff(monkeypatch):
    monkeypatch.setattr(llm_module, "_BACKOFF_SECONDS", [0.0, 0.0, 0.0])


@pytest.fixture
def fake_vllm():
    servers = []

    def _make(fail_first_n: int = 0, content_builder=None) -> FakeVLLMServer:
        server = FakeVLLMServer()
        server.fail_first_n = fail_first_n
        # reset the class-level handler config so it cannot leak between tests
        server.content_builder = content_builder if content_builder is not None else (
            lambda body: "4"
        )
        server.start()
        servers.append(server)
        return server

    yield _make
    for s in servers:
        s.stop()


@pytest.mark.asyncio
@pytest.mark.usefixtures("zero_backoff")
async def test_send_returns_response_and_records_history(fake_vllm):
    server = fake_vllm()
    client = ReaperLLMClient(server.base_url, model="fake-model")
    try:
        await client.create_session("sess_a", system_prompt="You are a helper.")
        text = await client.send("sess_a", "What is 2+2?", thinking_level="low")
        assert text == "4"
        history = client.get_history("sess_a")
        assert len(history) == 2
        assert history[0] == {"role": "user", "content": "What is 2+2?"}
        assert history[1] == {"role": "assistant", "content": "4"}
        # payload shape shipped to vLLM
        body = server.payloads[0]
        assert body["model"] == "fake-model"
        assert body["max_completion_tokens"] == 4096  # 'low' budget
        roles = [m["role"] for m in body["messages"]]
        assert roles == ["system", "user"]
        assert "extra_body" not in body
        assert "response_format" not in body  # plain call ships no schema
    finally:
        await client.close()


@pytest.mark.asyncio
@pytest.mark.usefixtures("zero_backoff")
async def test_retry_on_5xx_then_succeeds(fake_vllm):
    server = fake_vllm(fail_first_n=2)
    client = ReaperLLMClient(server.base_url, model="fake-model", max_retries=3)
    try:
        await client.create_session("s_retry", "sys")
        text = await client.send("s_retry", "hello")
        assert text == "4"
        assert server.responses_served == 3  # 2 failures + 1 success
    finally:
        await client.close()


@pytest.mark.asyncio
@pytest.mark.usefixtures("zero_backoff")
async def test_retry_exhausted_raises_transient(fake_vllm):
    server = fake_vllm(fail_first_n=10)
    client = ReaperLLMClient(server.base_url, model="fake-model", max_retries=3)
    try:
        await client.create_session("s_fail", "sys")
        with pytest.raises(LLMTransientError):
            await client.send("s_fail", "hello")
        assert server.responses_served == 4  # 1 initial + 3 retries
    finally:
        await client.close()


@pytest.mark.asyncio
@pytest.mark.usefixtures("zero_backoff")
async def test_structured_output_uses_response_format_json_schema(fake_vllm):
    """Live-verified (2026-09-27): the :8035 vLLM build only honors the
    OpenAI-standard ``response_format`` json_schema — ``extra_body``
    structured_outputs and ``guided_json`` are silently ignored. Assert the
    client ships a valid json_schema and derives the schema name from title."""
    def builder(body):
        rf = body.get("response_format", {})
        return "found" if rf.get("type") == "json_schema" else "missing"

    server = fake_vllm(content_builder=builder)
    client = ReaperLLMClient(server.base_url, model="fake-model")
    try:
        await client.create_session("s_struct", "sys")
        schema = {
            "type": "object",
            "properties": {"x": {"type": "integer"}},
            "title": "Probe",
        }
        text = await client.send(
            "s_struct", "go", thinking_level="minimal", structured_output=schema
        )
        assert text == "found"
        rf = server.payloads[-1]["response_format"]
        assert rf["type"] == "json_schema"
        assert rf["json_schema"]["name"] == "Probe"  # derived from schema title
        assert rf["json_schema"]["schema"] == schema
        # legacy mechanisms must not be shipped (silently ignored by :8035)
        assert "extra_body" not in server.payloads[-1]
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_unknown_thinking_level_raises(fake_vllm, zero_backoff):
    server = fake_vllm()
    client = ReaperLLMClient(server.base_url, model="fake-model")
    try:
        await client.create_session("s_bad", "sys")
        with pytest.raises(ValueError):
            await client.send("s_bad", "hi", thinking_level="turbo")
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_reasoning_effort_shipped_only_for_deep_tiers(fake_vllm):
    """Empirically verified (2026-09-28, scripts/probe_effort.py): the :8035
    vLLM 0.24.0 DeepSeek-V4 tokenizer collapses minimal/low/medium/high to a
    cheap 'high' thinking mode and only honors the deep directive for
    max/xhigh. REAPER must therefore ship chat_template_kwargs.reasoning_effort
    ONLY for the deep tiers, and NEVER for the cheap ones."""
    server = fake_vllm()
    client = ReaperLLMClient(server.base_url, model="fake-model")
    try:
        n = 0
        for level, expected in [
            ("minimal", None),
            ("low", None),
            ("medium", None),
            ("high", None),
            ("max", "xhigh"),
            ("xhigh", "xhigh"),
        ]:
            sid = f"s_eff_{level}"
            await client.create_session(sid, "sys")
            await client.send(sid, "go", thinking_level=level)
            body = server.payloads[-1]
            kw = body.get("chat_template_kwargs")
            if expected is None:
                assert kw is None or "reasoning_effort" not in kw, (
                    f"{level} should not ship reasoning_effort, got {kw}")
            else:
                assert kw is not None, f"{level} must ship chat_template_kwargs"
                assert kw.get("reasoning_effort") == expected, (
                    f"{level} expected effort {expected!r}, got "
                    f"{kw.get('reasoning_effort')!r}")
            n += 1
        assert n == 6
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_missing_session_raises(fake_vllm, zero_backoff):
    server = fake_vllm()
    client = ReaperLLMClient(server.base_url, model="fake-model")
    try:
        with pytest.raises(KeyError):
            await client.send("nonexistent", "hi")
    finally:
        await client.close()


@pytest.mark.asyncio
@pytest.mark.usefixtures("zero_backoff")
async def test_sessions_are_isolated(fake_vllm):
    server = fake_vllm(content_builder=lambda body: "x")
    client = ReaperLLMClient(server.base_url, model="fake-model")
    try:
        await client.create_session("s1", "one")
        await client.create_session("s2", "two")
        await client.send("s1", "m1")
        await client.send("s2", "m2")
        assert len(client.get_history("s1")) == 2
        assert [m["role"] for m in client.get_history("s2")] == ["user", "assistant"]
        client.destroy_session("s1")
        with pytest.raises(KeyError):
            client.get_history("s1")
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_request_path_not_doubled_when_base_url_has_v1(fake_vllm, zero_backoff):
    """Finding #9 (found via live endpoint smoke): a base_url carrying the
    OpenAI-SDK '/v1' suffix (as in configs/default.toml) must be normalized to
    the server root so the request lands on exactly '/v1/chat/completions' —
    httpx appends the request path to the base path, so keeping '/v1' there
    produced a doubled '/v1/v1/chat/completions' that 404s on vLLM."""
    server = fake_vllm()
    client = ReaperLLMClient(server.base_url + "/v1", model="fake-model")
    try:
        assert client.base_url == server.base_url  # normalized to server root
        await client.create_session("s_path", "sys")
        await client.send("s_path", "hi", thinking_level="minimal")
        assert server.paths[-1] == "/v1/chat/completions", server.paths
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_base_url_normalization_with_and_without_v1_suffix():
    """Normalization must be idempotent across base_url shapes and never fall
    back to mis-resolving the OpenAI-compatible API path."""
    cases = [
        ("http://127.0.0.1:8035/v1", "http://127.0.0.1:8035"),
        ("http://127.0.0.1:8035/", "http://127.0.0.1:8035"),
        ("http://127.0.0.1:8035", "http://127.0.0.1:8035"),
    ]
    for base, expected in cases:
        client = ReaperLLMClient(base, model="m")
        try:
            assert client.base_url == expected, (base, client.base_url)
        finally:
            await client.close()


@pytest.mark.asyncio
@pytest.mark.usefixtures("zero_backoff")
async def test_async_context_manager(fake_vllm):
    server = fake_vllm()
    async with ReaperLLMClient(server.base_url, model="fake-model") as client:
        await client.create_session("s_ctx", "sys")
        text = await client.send("s_ctx", "ping", thinking_level="minimal")
        assert text == "4"
    # client closed by __aexit__ — a further send fails on the closed transport
    with pytest.raises(
        (httpx.ConnectError, httpx.RemoteProtocolError, RuntimeError, LLMTransientError)
    ):
        await client.send("s_ctx", "post-close")

