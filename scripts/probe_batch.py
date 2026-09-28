#!/usr/bin/env python3
"""Batching/throughput probe against the live DeepSeek-V4 endpoint.

Controlled concurrency sweep: issue N identical realistic REAPER-style
requests (system prompt + small context + structured-output JSON) at
concurrency 1/2/4/8, strictly measuring end-to-end latency + server-declared
token throughput, WITHOUT using the client's own gate. This is the data needed
to set llm_max_concurrent + server --max-num-seqs/--max-num-batched-tokens.

Usage:
    python3 scripts/probe_batch.py --concurrency 1,2,4,8 --requests 4 \
        --base-url http://localhost:8035/v1

Output per sweep: total_dur_s, mean_latency_s, tokens_out, tok/s, plus the
vLLM-reported reasoning/completion token split. Requests are fire-and-forget
(httpx AsyncClient with a per-request semaphore).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time

import httpx

DEFAULT_BASE = "http://localhost:8035/v1"
DEFAULT_MODEL = "deepseek"

_TASK = (
    "Reverse-engineer this stripped function. Return JSON "
    '{"name": str, "purpose": str, "confidence": number} describing '
    "sub_1400: it calls strlen(arg1), returns the length; arg2 is unused."
)


async def one(client: httpx.AsyncClient, model: str,
              effort: str, budget: int, start: float) -> dict:
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": "You answer with JSON only."},
            {"role": "user", "content": _TASK},
        ],
        "max_completion_tokens": budget,
        "chat_template_kwargs": {"reasoning_effort": effort},
    }
    try:
        r = await client.post("/chat/completions", json=payload, timeout=600)
        r.raise_for_status()
        data = r.json()
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}",
                "latency": time.time() - start}
    usage = data.get("usage") or {}
    ctd = usage.get("completion_tokens_details") or {}
    return {
        "ok": True,
        "latency": time.time() - start,
        "completion_tokens": usage.get("completion_tokens", 0),
        "reasoning_tokens": ctd.get("reasoning_tokens", 0),
    }


async def sweep(base: str, model: str, concurrency: int, n_reqs: int,
                effort: str, budget: int) -> dict:
    async with httpx.AsyncClient(base_url=base, timeout=600,
                                 limits=httpx.Limits(max_connections=64)) as c:
        sem = asyncio.Semaphore(concurrency)

        async def run(i: int) -> dict:
            async with sem:
                return await one(c, model, effort, budget, time.time())

        t0 = time.time()
        results = await asyncio.gather(*(run(i) for i in range(n_reqs)))
        total = time.time() - t0
    ok = [r for r in results if r.get("ok")]
    out_tokens = sum(r.get("completion_tokens", 0) for r in ok)
    lat = [r["latency"] for r in results if r.get("ok")]
    return {
        "concurrency": concurrency,
        "n_reqs": n_reqs,
        "ok": len(ok),
        "total_s": round(total, 2),
        "mean_latency_s": round(sum(lat) / len(lat), 2) if lat else None,
        "completion_tokens": out_tokens,
        "tok_per_s": round(out_tokens / total, 2) if total else 0.0,
        "per_req": [round(x, 1) for x in lat][:8],
    }


async def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--base-url", default=DEFAULT_BASE)
    p.add_argument("--model", default=DEFAULT_MODEL)
    p.add_argument("--concurrency", default="1,2,4,8")
    p.add_argument("--requests", type=int, default=4)
    p.add_argument("--effort", default="xhigh")
    p.add_argument("--budget", type=int, default=20480)
    args = p.parse_args()

    for concurrency in [int(x) for x in args.concurrency.split(",")]:
        res = await sweep(args.base_url, args.model, concurrency,
                          args.requests, args.effort, args.budget)
        print(f"c={res['concurrency']:>2} n={res['n_reqs']} ok={res['ok']} "
              f"total={res['total_s']:>7.2f}s mean_lat={res['mean_latency_s']}s "
              f"tok={res['completion_tokens']} {res['tok_per_s']:>7.1f} tok/s")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
