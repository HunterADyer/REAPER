#!/usr/bin/env python3
"""Probe the live :8035 DeepSeek-V4 endpoint's reasoning_effort behavior.

Empirical check (the deployed vLLM template claims all of minimal/low/medium/
high collapse to "high", only none and max/xhigh differ). We send the SAME
small reasoning task at every effort level, one request at a time, and record:

  - reasoning_tokens (from usage.completion_tokens_details)
  - total completion_tokens
  - duration_s
  - whether the response parses as valid JSON

Note: REAPER also sends max_completion_tokens as the TOTAL budget. To isolate
the EFFORT knob we do NOT send a budget here (server default), so any
difference is purely from the effort directive. Strictly serialized.
"""
from __future__ import annotations

import json
import sys
import time

import httpx

BASE = "http://localhost:8035/v1"
MODEL = "deepseek"
TASK = (
    "Reverse-engineer ONE function from this stripped binary context and return "
    "a JSON object with keys {\"name\": str, \"purpose\": str, "
    "\"confidence\": number}. Function: sub_1400 calls strlen on arg1 and "
    "returns the result; arg2 is null. JSON only."
)

LEVELS = ["low", "medium", "high", "max", "xhigh"]
# The none level explicitly disables thinking if honored; keep it in the loop
# but it is expected to be cheap/fail-thinking.

async def main() -> int:
    async with httpx.AsyncClient(base_url=BASE, timeout=180) as client:
        for level in LEVELS:
            payload = {
                "model": MODEL,
                "messages": [
                    {"role": "system", "content": "You answer with JSON only."},
                    {"role": "user", "content": TASK},
                ],
                # REAPER-style: effort goes in chat_template_kwargs, NOT as a
                # top-level OpenAI field (that is how harness/llm_client sends it).
                "chat_template_kwargs": {"reasoning_effort": level},
            }
            t0 = time.monotonic()
            try:
                r = await client.post("/chat/completions", json=payload)
                r.raise_for_status()
                data = r.json()
            except Exception as exc:  # noqa: BLE001
                print(f"{level:<8} ERROR {type(exc).__name__}: {exc}")
                continue
            dt = time.monotonic() - t0
            usage = data.get("usage") or {}
            ctd = usage.get("completion_tokens_details") or {}
            choice = (data.get("choices") or [{}])[0]
            msg = choice.get("message") or {}
            content = (msg.get("reasoning") or msg.get("reasoning_content") or "")
            fin = choice.get("finish_reason")
            parsed = False
            try:
                json.loads(content or msg.get("content") or "")
                parsed = True
            except Exception:
                pass
            print(
                f"effort={level:<8} reason_tokens={ctd.get('reasoning_tokens', 0):>6} "
                f"completion_tokens={usage.get('completion_tokens', 0):>6} "
                f"dur={dt:6.1f}s finish={fin} json_ok={parsed} "
                f"thinking_snippet={(content or '')[:40]!r}"
            )
    return 0


if __name__ == "__main__":
    import asyncio

    sys.exit(asyncio.run(main()))
