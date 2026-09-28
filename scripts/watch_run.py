#!/usr/bin/env python3
"""Passive deadlock/stall watch for the live REAPER run (Run Cockpit).

Poll the GUI API + event firehose. A run is NOT deadlocked as long as:
  - llm.response events keep landing, OR
  - the TODO ledger keeps draining (completed tasks growing / in_progress+pending
    shrinking), OR
  - the GPU is actively generating (nvidia-smi util > 30% — best signal that
    the single on-the-wire request is progressing).

It only prints when SOMETHING ABNORMAL is observed (silence beyond a threshold
= potential hang) or as a heartbeat every --heartbeat seconds when healthy.

Usage:
    python3 scripts/watch_run.py --threshold 900 --heartbeat 600 [--poll 30]
"""
from __future__ import annotations

import argparse
import json
import shlex
import sqlite3
import subprocess
import sys
import time
import urllib.request

BASE = "http://127.0.0.1:8000"
EVENTS = "data/cjson_001_events.jsonl"
TODO_DB = "data/cjson_001_todo.db"


def api(path: str) -> dict:
    try:
        with urllib.request.urlopen(f"{BASE}{path}", timeout=10) as r:
            return json.load(r)
    except Exception as exc:  # noqa: BLE001
        return {"_error": str(exc)}


def last_event_epoch() -> float:
    try:
        with open(EVENTS) as f:
            ts = 0.0
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    ts = float(json.loads(line).get("ts", ts))
                except Exception:  # noqa: BLE001
                    continue
            return ts
    except FileNotFoundError:
        return 0.0


def todo_counts() -> dict:
    try:
        con = sqlite3.connect(TODO_DB)
        try:
            rows = con.execute(
                "SELECT status, count(*) FROM tasks GROUP BY status"
            ).fetchall()
            return {s: int(n) for s, n in rows}
        finally:
            con.close()
    except Exception as exc:  # noqa: BLE001
        return {"_error": str(exc)}


def gpu_busy() -> bool:
    """True when any GPU shows util above 30% (actively generating)."""
    try:
        out = subprocess.run(
            shlex.split("nvidia-smi --query-gpu=utilization.gpu --format=csv,noheader,nounits"),
            capture_output=True, text=True, timeout=8,
        )
        vals = [int(v.strip()) for v in out.stdout.splitlines() if v.strip() != ""]
        return any(v > 30 for v in vals)
    except Exception:  # noqa: BLE001
        return False


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--threshold", type=float, default=900.0,
                   help="seconds of full silence before suspecting a hang")
    p.add_argument("--poll", type=float, default=30.0)
    p.add_argument("--heartbeat", type=float, default=600.0,
                   help="print a healthy heartbeat every N seconds")
    args = p.parse_args()

    last_healthy_print = 0.0
    print("[watch] starting: threshold=%.0fs poll=%.0fs heartbeat=%.0fs"
          % (args.threshold, args.poll, args.heartbeat), flush=True)
    while True:
        st = api("/api/state")
        loops = api("/api/loops")
        run = st.get("run") or {}
        counters = (loops.get("counters") or {})
        last = last_event_epoch()
        silent = time.time() - last
        todo = todo_counts()
        busy = gpu_busy()
        status = run.get("status", "unknown")
        phase = run.get("phases_status", {})
        err = run.get("error")

        if status != "running":
            print(f"[watch] RUN NOT RUNNING: status={status!r} error={err!r}", flush=True)
            if status in ("complete", "error"):
                print("[watch] exiting — run finished.", flush=True)
                return 0

        abnormal = silent > args.threshold and not busy
        if abnormal:
            print(
                f"[WARN] silence={silent:.0f}s (threshold {args.threshold:.0f}s) "
                f"GPU busy={busy} | phase={phase} | todo={todo} | "
                f"llm={counters.get('llm.calls')} errors={counters.get('llm.errors')} "
                f"accepted={counters.get('claim.accepted')} stuck={counters.get('investigation.stuck')}",
                flush=True,
            )
        elif time.time() - last_healthy_print >= args.heartbeat:
            print(
                f"[ok] silent={silent:.0f}s gpu_busy={busy} progress: "
                f"llm.calls={counters.get('llm.calls')} accepted={counters.get('claim.accepted')} "
                f"rejected={counters.get('claim.rejected')} iterations={counters.get('resynthesis.iterations')} "
                f"| todo={todo} | phase={phase}",
                flush=True,
            )
            last_healthy_print = time.time()

        time.sleep(args.poll)


if __name__ == "__main__":
    sys.exit(main())
