#!/usr/bin/env python3
"""Serialized live smoke test against the shared vLLM endpoint (1.3 / finding #3).

Deliberately issues requests ONE AT A TIME: every request is awaited to
completion before the next is sent — no ``asyncio.gather``, no concurrent
sessions — so at most a single request is ever in flight against the shared
:8035 server. This mirrors the mandated "one llm thread active at a time"
operating rule for this environment.

Uses the REAL ``ReaperLLMClient`` and the REAL Pydantic structured-output
schemas from the pipeline (Submission / FunctionSummary / CriticVerdict),
with the endpoint/model read from configs/default.toml — the same values
``run_pipeline`` uses (run.py instantiates ``ReaperLLMClient`` with exactly
these strings).

Usage:
    python3 scripts/smoke_llm_endpoint.py [--config configs/default.toml] [--checks 5]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
import tomllib
from pathlib import Path

_THIS_DIR = Path(__file__).resolve().parent          # .../scripts
REPO_ROOT = _THIS_DIR.parent
sys.path.insert(0, str(REPO_ROOT))

from reaper.harness.llm_client import ReaperLLMClient  # noqa: E402
from reaper.harness.submission import (  # noqa: E402
    CriticVerdict,
    FunctionSummary,
    Submission,
    get_schema,
    parse_response,
)


class UsageCaptureClient(ReaperLLMClient):
    """Identical to ReaperLLMClient, except it also records the vLLM ``usage``
    block and any reasoning-channel text so the smoke test can report tokens
    and throughput instead of only boolean success."""

    def __init__(self, base_url: str, model: str, max_retries: int = 3):
        super().__init__(base_url, model, max_retries)
        self.last_usage: dict | None = None
        self.last_reasoning: str | None = None

    async def _post_chat(self, payload: dict) -> str:
        resp = await self._http.post("/v1/chat/completions", json=payload)
        resp.raise_for_status()
        data = resp.json()
        self.last_usage = data.get("usage") or {}
        msg = (data.get("choices", [{}])[0].get("message") or {})
        self.last_reasoning = msg.get("reasoning_content") or msg.get("reasoning") or None
        content = msg.get("content")
        if content is None:
            raise RuntimeError(f"unexpected vLLM response shape: {data}")
        return content


def _load_config(path: Path) -> dict:
    with path.open("rb") as fh:
        return tomllib.load(fh)


def _make_checks() -> list[dict]:
    """Small, realistic checks. Structured checks use the exact schemas the
    agents send. All are kept short to stay polite to the shared endpoint."""
    return [
        {
            "label": "connect-sanity", "level": "minimal", "plain": True,
            "system": "You are a terse assistant. Reply in as few tokens as possible.",
            "message": "Reply with exactly the single word: OK",
            "model": None,
        },
        {
            "label": "pass1-rename", "level": "minimal", "plain": False,
            "system": (
                "You are a binary-reverse-engineering rename agent. Given a short C "
                "function body you propose a pothole_case name. Return ONLY the "
                "structured output."
            ),
            "message": (
                'C: int f(char *a){ return (int)(unsigned char)a[0]; }\n'
                'Propose a pothole_case name for this function.'
            ),
            "model": Submission,
        },
        {
            "label": "pass1-summary", "level": "low", "plain": False,
            "system": (
                "You are a binary-reverse-engineering agent writing a one-paragraph "
                "function purpose summary. Return ONLY the structured output."
            ),
            "message": (
                'C: static int next(struct cJSON *it){ return it ? it->next : 0; }\n'
                'Summarize the purpose of this function in one short paragraph.'
            ),
            "model": FunctionSummary,
        },
        {
            "label": "critic-verdict", "level": "low", "plain": False,
            "system": (
                "You are a claim verifier for a reverse-engineering effort. Judge the "
                "claim's truth_level; return ONLY the structured output."
            ),
            "message": (
                "Claim: 'struct cJSON stores a linked list via the next pointer.' "
                "It is supported by the field declaration. Verdict:"
            ),
            "model": CriticVerdict,
        },
        {
            "label": "close-sanity", "level": "low", "plain": True,
            "system": "You are a terse assistant. Reply in as few tokens as possible.",
            "message": "Reply with exactly the single word: DONE",
            "model": None,
        },
    ]


async def _run_serial(client: ReaperLLMClient, checks: list[dict]) -> list[dict]:
    """Run every check strictly serially; track in-flight to prove serialism."""
    results = []
    max_inflight = 0
    inflight = 0
    for idx, check in enumerate(checks):
        inflight += 1
        max_inflight = max(max_inflight, inflight)
        t0 = time.perf_counter()
        try:
            session_id = f"smoke-{idx}"
            await client.create_session(session_id, check["system"])
            kwargs = {"thinking_level": check["level"]}
            if not check["plain"]:
                kwargs["structured_output"] = get_schema(check["model"])
            text = await client.send(session_id, check["message"], **kwargs)
            dt = time.perf_counter() - t0
            detail = text.strip()[:120]
            if not check["plain"]:
                try:
                    json.loads(text)
                    detail = "json=valid "
                except Exception as e:  # noqa: BLE001
                    detail = f"json=INVALID({type(e).__name__}) "
                try:
                    parse_response(check["model"], text)
                    detail += "schema=valid"
                except Exception as e:  # noqa: BLE001
                    detail += f"schema=INVALID({type(e).__name__})"
                detail += " :: " + text.strip()[:100]
            results.append({
                "label": check["label"], "level": check["level"],
                "plain": check["plain"], "ok": True, "status": "OK",
                "latency_s": round(dt, 3),
                "usage": client.last_usage or {},
                "reasoning": bool(client.last_reasoning),
                "detail": detail,
            })
        except Exception as e:  # noqa: BLE001
            dt = time.perf_counter() - t0
            results.append({
                "label": check["label"], "level": check["level"],
                "plain": check["plain"], "ok": False,
                "status": f"ERROR {type(e).__name__}",
                "latency_s": round(dt, 3), "usage": {}, "reasoning": False,
                "detail": str(e),
            })
        finally:
            inflight -= 1
    results.append({"max_inflight": max_inflight})
    return results


async def main(config_path: str) -> int:
    config = _load_config(Path(config_path).resolve())
    base_url = config["vllm"]["base_url"]
    model = config["vllm"]["model"]
    checks = _make_checks()

    print(f"[smoke] endpoint={base_url} model={model} checks={len(checks)} (STRICTLY SERIAL)")
    client = None
    try:
        client = UsageCaptureClient(base_url, model)
        results = await _run_serial(client, checks)
    finally:
        if client is not None:
            await client.close()

    max_inflight = results.pop()["max_inflight"]
    header = (
        f"{'#':>2}  {'check':<18} {'level':<9} {'struct':<8} {'status':<26} "
        f"{'lat(s)':>7} {'out_tok':>7} {'tok/s':>7}"
    )
    print(header)
    print("-" * len(header))
    n_ok = 0
    total_wall = 0.0
    for i, r in enumerate(results, 1):
        out_tok = r["usage"].get("completion_tokens", 0) or 0
        tok_s = round(out_tok / r["latency_s"], 1) if r["latency_s"] > 0 else 0.0
        total_wall += r["latency_s"]
        n_ok += int(r["ok"])
        struct = "-" if r["plain"] else "yes"
        print(
            f"{i:>2}  {r['label']:<18} {r['level']:<9} {struct:<8} "
            f"{r['status']:<26} {r['latency_s']:>7.2f} {out_tok:>7} {tok_s:>7.1f}"
        )
        print(f"      {r['detail'][:160]}")
    print("-" * len(header))
    print(
        f"result: {n_ok}/{len(results)} ok | wall total={total_wall:.2f}s | "
        f"mean={total_wall / len(results):.2f}s | max in-flight={max_inflight} "
        f"({'SERIAL-OK' if max_inflight <= 1 else 'NOT-SERIAL'})"
    )
    return 0 if n_ok == len(results) else 1


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Serialized vLLM endpoint smoke test")
    ap.add_argument("--config", default=str(REPO_ROOT / "configs" / "default.toml"))
    args = ap.parse_args()
    sys.exit(asyncio.run(main(args.config)))
