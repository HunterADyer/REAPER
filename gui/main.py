"""reaper.gui.main — FastAPI server, WebSocket + REST monitoring, run launcher.

Usage (foreground):
    python -m reaper.gui.main --port 8000

Usage (detached daemon — recommended so you can monitor a long run):
    python -m reaper.gui.main --detach              # starts bg server, prints URL
    curl http://127.0.0.1:8000/api/state            # monitor from CLI/scripts
    python -m reaper.gui.main --status               # is the server alive?
    python -m reaper.gui.main --stop                 # stop the daemon

The GUI HOSTS the pipeline run in-process (this is what makes the live event
bus work — it is process-local). Start a run from the web UI (Run controls)
or POST to /api/run/start.

Detached note: the server keeps running after you close the browser. Reading
is strictly pull-based (REST) plus the optional WebSocket stream; nothing the
GUI does can mutate or corrupt pipeline state.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import signal
import subprocess
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import aiosqlite
import neo4j
import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from reaper.harness.events import bus
from reaper.harness.llm_client import _THINKING_BUDGETS  # noqa: F401 - exposed

# Repo root == package root (pyproject maps reaper -> "."). Resolve config,
# static files, and runtime artifacts against it, never against the CWD.
REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = str(REPO_ROOT / "configs" / "default.toml")
STATIC_DIR = Path(__file__).resolve().parent / "static"
GUI_META = REPO_ROOT / "data" / "gui.json"
GUI_PID = REPO_ROOT / "data" / "gui.pid"
GUI_LOG = REPO_ROOT / "data" / "gui.log"

DEFAULT_BINARY = str(REPO_ROOT / "eval" / "cjson" / "cjson_test")
DEFAULT_RUN_ID = "cjson_001"
DEFAULT_PHASES = "2,3,4,5,6,7"


def load_config(path: str | None = None) -> dict:
    cfg_path = Path(path or DEFAULT_CONFIG)
    with open(cfg_path, "rb") as fh:
        import tomllib
        return tomllib.load(fh)


class _RunState:
    """Server-side view of the current/previous run (mirrors bus events)."""

    def __init__(self) -> None:
        self.run_id: str | None = None
        self.binary: str | None = None
        self.phases: list[int] = []
        self.status: str = "idle"          # idle|running|complete|error
        self.started_at: float | None = None
        self.finished_at: float | None = None
        self.error: str | None = None
        self.complete: bool | None = None
        self.phases_status: dict[int, str] = {}
        self.coverage: dict = {}
        self.task: asyncio.Task | None = None

    def snapshot(self) -> dict:
        return {
            "run_id": self.run_id,
            "binary": self.binary,
            "phases": self.phases,
            "status": self.status,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "error": self.error,
            "complete": self.complete,
            "phases_status": self.phases_status,
            "coverage": self.coverage,
        }


def _json_config(cfg: dict) -> dict:
    return {
        "neo4j": {"uri": cfg["neo4j"]["uri"]},
        "vllm": {"base_url": cfg["vllm"]["base_url"], "model": cfg["vllm"]["model"]},
        "paths": dict(cfg["paths"]),
        "limits": dict(cfg.get("limits") or {}),
        "thinking_levels": dict(cfg.get("thinking_levels") or {}),
    }


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.cfg = load_config(app.state.config_path)
    _mkdirs(app.state.cfg)
    # GUI's own driver: read-only-ish access for /api/functions + connectivity.
    app.state.neo4j = neo4j.AsyncGraphDatabase.driver(
        app.state.cfg["neo4j"]["uri"],
        auth=(app.state.cfg["neo4j"]["user"], app.state.cfg["neo4j"]["password"]))
    # Reflect live events into server state so /api/state is accurate even
    # with zero WebSocket clients attached (detached monitoring).
    app.state.run = _RunState()
    app.state.boot = time.time()
    app.state.event_sink_task = asyncio.create_task(_event_sink(app))
    _write_meta(app)
    yield
    app.state.event_sink_task.cancel()
    try:
        await app.state.event_sink_task
    except (asyncio.CancelledError, Exception):
        pass
    if app.state.run.task is not None and not app.state.run.task.done():
        app.state.run.task.cancel()
    try:
        await app.state.neo4j.close()
    except Exception:
        pass


def _write_meta(app) -> None:
    try:
        GUI_META.parent.mkdir(parents=True, exist_ok=True)
        GUI_META.write_text(json.dumps({
            "pid": os.getpid(),
            "url": f"http://{app.state.host}:{app.state.port}",
            "started_at": time.time(),
            "config": app.state.config_path,
        }, indent=2))
        GUI_PID.write_text(str(os.getpid()))
    except Exception:  # noqa: BLE001
        pass


def _mkdirs(cfg: dict) -> None:
    for key in ("data_dir",):
        p = Path(cfg["paths"][key])
        if not p.is_absolute():
            p = REPO_ROOT / p
        p.mkdir(parents=True, exist_ok=True)


_HOST = "127.0.0.1"
_PORT = 8000


async def _event_sink(app) -> None:
    """Consume the shared bus and mirror lifecycle events into server state."""
    q = bus.subscribe()
    try:
        while True:
            ev = await q.get()
            etype = ev.get("event")
            data = ev.get("data") or {}
            rs = app.state.run
            if etype == "run.started":
                rs.run_id = data.get("run_id") or rs.run_id
                rs.binary = data.get("binary") or rs.binary
                rs.phases = data.get("phases") or rs.phases
                rs.status = "running"
                rs.started_at = time.time()
                rs.error = None
            elif etype == "run.phase":
                phase = data.get("phase")
                if phase is not None:
                    rs.phases_status[int(phase)] = data.get("status", "start")
            elif etype in ("completion_coverage",):
                rs.coverage = data or {}
            elif etype == "run.complete":
                rs.complete = data.get("complete")
                rs.coverage = data.get("coverage") or rs.coverage
                rs.status = "complete"
                rs.finished_at = time.time()
            elif etype == "run.error":
                rs.error = data.get("error")
                rs.status = "error"
                rs.finished_at = time.time()
    except asyncio.CancelledError:
        pass
    finally:
        bus.unsubscribe(q)


def _run_files(cfg: dict, run_id: str) -> dict:
    data_dir = Path(cfg["paths"]["data_dir"])
    if not data_dir.is_absolute():
        data_dir = REPO_ROOT / data_dir
    return {
        "debug_events": str(data_dir / f"{run_id}_events.jsonl"),
        "rltrace": str(data_dir / f"{run_id}_rltrace.jsonl"),
        "ledger_db": str(data_dir / f"{run_id}_ledger.db"),
        "todo_db": str(data_dir / f"{run_id}_todo.db"),
        "reaper_output": str(data_dir / f"{run_id}_reaper_output.json"),
        "bndb": str(data_dir / "target.bndb"),
        "trace_dir": str(Path(cfg["paths"]["traces_dir"]) / run_id)
        if not Path(cfg["paths"]["traces_dir"]).is_absolute()
        else str(Path(cfg["paths"]["traces_dir"]) / run_id),
    }


def create_app(config_path: str | None = None, host: str = "127.0.0.1",
               port: int = 8000) -> FastAPI:
    @asynccontextmanager
    async def _inner_lifespan(app: FastAPI):
        async with lifespan(app):
            yield

    app = FastAPI(title="REAPER Run Cockpit", lifespan=_inner_lifespan)
    app.state.config_path = config_path or DEFAULT_CONFIG
    app.state.host = host
    app.state.port = port
    register_routes(app)
    return app


def register_routes(app: FastAPI) -> None:
    # ---- static frontend ---------------------------------------------------
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    @app.get("/", include_in_schema=False)
    async def index():
        return FileResponse(str(STATIC_DIR / "index.html"))

    # ---- REST pull-monitoring APIs (work detached, no browser needed) ------

    @app.get("/api/state")
    async def api_state():
        cfg = app.state.cfg
        rs = app.state.run
        neo4j_ok = False
        n_funcs: int | None = None
        try:
            async with app.state.neo4j.session() as s:
                await (await s.run("RETURN 1 AS ok")).consume()
                neo4j_ok = True
                res = await s.run("MATCH (f:Function) RETURN count(f) AS n")
                rec = await res.single()
                n_funcs = rec["n"] if rec else 0
        except Exception:
            neo4j_ok = False
        return {
            "server": {"url": f"http://{app.state.host}:{app.state.port}",
                       "uptime_s": round(time.time() - app.state.boot, 1)},
            "run": rs.snapshot(),
            "config": _json_config(cfg),
            "files": _run_files(cfg, rs.run_id or "default"),
            "neo4j": {"connected": neo4j_ok, "n_functions": n_funcs},
        }

    @app.get("/api/events")
    async def api_events(after: int = 0, limit: int = 1000,
                         types: str = ""):
        events = await bus.history_after(after, limit)
        if types:
            keep = {t.strip() for t in types.split(",") if t.strip()}
            events = [e for e in events if e.get("event") in keep]
        return {"seq_now": next((e["seq"] for e in events[::-1]), after),
                "events": events}

    @app.get("/api/functions")
    async def api_functions():
        try:
            async with app.state.neo4j.session() as s:
                res = await s.run(
                    "MATCH (f:Function) RETURN f.address AS address, "
                    "f.name AS name, f.llm_name AS llm_name, "
                    "f.canon_name AS canon_name, "
                    "coalesce(f.pinned, false) AS pinned, "
                    "coalesce(f.ambiguous, false) AS ambiguous, "
                    "f.traversal_order AS traversal_order, "
                    "f.scc_id AS scc_id, coalesce(f.isolated, false) AS isolated")
                rows = [r async for r in res]
            return {"functions": [
                {k: (r[k] if k in r else None) for k in
                 ("address", "name", "llm_name", "canon_name", "pinned",
                  "ambiguous", "traversal_order", "scc_id", "isolated")}
                for r in rows]}
        except Exception as exc:  # noqa: BLE001
            return {"functions": [], "error": str(exc)}

    @app.get("/api/edges")
    async def api_edges(limit: int = 5000):
        """Graph edges for the call-graph view (relations between graph nodes)."""
        try:
            async with app.state.neo4j.session() as s:
                res = await s.run(
                    "MATCH (a)-[r]->(b) "
                    "RETURN a.id AS src, labels(a)[0] AS src_label, "
                    "       type(r) AS type, b.id AS dst, labels(b)[0] AS dst_label "
                    "LIMIT $limit", limit=limit)
                return {"edges": [{
                    "src": r["src"], "src_label": r["src_label"],
                    "type": r["type"], "dst": r["dst"],
                    "dst_label": r["dst_label"]} for r in [rec async for rec in res]]}
        except Exception as exc:  # noqa: BLE001
            return {"edges": [], "error": str(exc)}

    async def _dbfile(name: str) -> str:
        cfg = app.state.cfg
        rs = app.state.run
        p = Path(cfg["paths"]["data_dir"]) / f"{rs.run_id or 'default'}_{name}"
        if not p.is_absolute():
            p = REPO_ROOT / p
        return str(p)

    @app.get("/api/claims")
    async def api_claims(function: str = "", limit: int = 500):
        try:
            db = await aiosqlite.connect(await _dbfile("ledger.db"))
            try:
                sql = ("SELECT id, function_address, claim_text, truth_level, "
                       "submitted_by, reviewed_by FROM claims")
                params: tuple = ()
                if function:
                    sql += " WHERE function_address = ?"
                    params = (function,)
                sql += " ORDER BY id DESC LIMIT ?"
                rows = await (await db.execute(sql, params + (limit,))).fetchall()
                return {"claims": [{
                    "id": r[0], "function": r[1], "claim": r[2],
                    "truth_level": r[3], "submitted_by": r[4],
                    "reviewed_by": r[5]} for r in rows]}
            finally:
                await db.close()
        except Exception as exc:  # noqa: BLE001
            return {"claims": [], "error": str(exc)}

    @app.get("/api/tasks")
    async def api_tasks(status: str = "", limit: int = 500):
        try:
            db = await aiosqlite.connect(await _dbfile("todo.db"))
            try:
                sql = ("SELECT id, description, status, start_position, "
                       "rejection_count, assigned_to, critic_feedback "
                       "FROM tasks")
                params: tuple = ()
                if status:
                    sql += " WHERE status = ?"
                    params = (status,)
                sql += " ORDER BY id DESC LIMIT ?"
                rows = await (await db.execute(sql, params + (limit,))).fetchall()
                return {"tasks": [{
                    "id": r[0], "description": r[1], "status": r[2],
                    "start_position": r[3], "rejection_count": r[4],
                    "assigned_to": r[5], "critic_feedback": r[6]} for r in rows]}
            finally:
                await db.close()
        except Exception as exc:  # noqa: BLE001
            return {"tasks": [], "error": str(exc)}

    @app.get("/api/loops")
    async def api_loops():
        """Loops & live counters vs configured caps (non-termination watch)."""
        events = await bus.history_after(0, 100000)
        counts: dict[str, int] = {}
        for e in events:
            counts[e.get("event", "")] = counts.get(e.get("event", ""), 0) + 1
        llm_pending = counts.get("llm.request", 0) - \
            counts.get("llm.response", 0) - counts.get("llm.error", 0)
        return {
            "limits": app.state.cfg.get("limits") or {},
            "thinking_levels": app.state.cfg.get("thinking_levels") or {},
            "counters": {
                "llm.calls": counts.get("llm.response", 0),
                "llm.errors": counts.get("llm.error", 0),
                "llm.pending_in_flight": max(0, llm_pending),
                "claim.accepted": counts.get("claim_accepted", 0),
                "claim.rejected": counts.get("claim_rejected", 0),
                "claim.force_accepted": counts.get("claim_force_accepted", 0),
                "pass2.review_retries": counts.get("pass2_review_retry", 0),
                "resynthesis.iterations": counts.get("resynthesis_iteration", 0),
                "investigation.stuck": counts.get("investigation_stuck", 0),
                "scheduler.reviewed": counts.get("scheduler_reviewed", 0),
            },
        }

    # ---- run lifecycle ------------------------------------------------------

    @app.post("/api/run/start")
    async def api_run_start(payload: dict):
        rs = app.state.run
        if rs.task is not None and not rs.task.done():
            return JSONResponse({"ok": False, "error": "run already active"},
                                status_code=409)
        run_id = str(payload.get("run_id") or DEFAULT_RUN_ID)
        binary = str(payload.get("binary") or DEFAULT_BINARY)
        phases = [int(p.strip()) for p in str(payload.get("phases") or
                                              DEFAULT_PHASES).split(",")
                  if p.strip()]
        rs.run_id, rs.binary, rs.phases = run_id, binary, phases
        rs.status, rs.error = "starting", None
        rs.phases_status = {}
        rs.coverage = {}
        rs.task = asyncio.create_task(
            _run_wrapper(app, run_id, binary, phases), name="pipeline-run")
        return {"ok": True, "run": rs.snapshot()}

    @app.post("/api/run/stop")
    async def api_run_stop():
        rs = app.state.run
        if rs.task is not None and not rs.task.done():
            rs.task.cancel()
            return {"ok": True, "run": rs.snapshot(), "cancel_requested": True}
        return {"ok": True, "run": rs.snapshot(), "cancel_requested": False}

    @app.get("/api/health")
    async def api_health():
        return {"ok": True}

    # ---- WebSocket live feed ------------------------------------------------

    @app.websocket("/ws")
    async def ws(websocket: WebSocket):
        await websocket.accept()
        q = bus.subscribe()
        await websocket.send_json({"type": "hello", "seq_now": 0})
        try:
            while True:
                try:
                    item = await asyncio.wait_for(q.get(), timeout=10)
                except asyncio.TimeoutError:
                    try:
                        await websocket.send_json({"type": "ping"})
                    except Exception:  # noqa: BLE001
                        break
                    continue
                try:
                    await websocket.send_json(item)
                except Exception:  # noqa: BLE001
                    break
        except WebSocketDisconnect:
            pass
        finally:
            bus.unsubscribe(q)


async def _run_wrapper(app, run_id: str, binary: str, phases: list[int]) -> None:
    from reaper.run import run_pipeline
    rs = app.state.run
    rs.status = "running"
    rs.started_at = time.time()
    try:
        await run_pipeline(binary, app.state.config_path, run_id, phases)
    except asyncio.CancelledError:
        rs.status = "error"
        rs.error = "run cancelled"
        rs.finished_at = time.time()
    except Exception as exc:  # noqa: BLE001
        rs.status = "error"
        rs.error = f"{type(exc).__name__}: {exc}"
        rs.finished_at = time.time()
    else:
        rs.status = "complete"
        rs.finished_at = time.time()


# ---------------------------------------------------------------------------
# CLI: foreground / detached daemon
# ---------------------------------------------------------------------------

def _daemonize(argv: list[str]) -> None:
    """Spawn a detached background server writing to data/gui.log/.pid."""
    GUI_META.parent.mkdir(parents=True, exist_ok=True)
    with open(GUI_LOG, "ab") as fh:
        proc = subprocess.Popen(
            [sys.executable, "-m", "reaper.gui.main", "--serve", *argv],
            start_new_session=True,
            stdout=fh, stderr=fh,
            cwd=str(REPO_ROOT),
        )
    print(f"REAPER GUI started (pid {proc.pid}) → http://{_HOST}:{_PORT}")
    print(f"  log:   {GUI_LOG}")
    print(f"  stop:  python -m reaper.gui.main --stop")


def _stop() -> None:
    if not GUI_PID.exists():
        print("not running (no pidfile)"); return
    pid = int(GUI_PID.read_text().strip())
    try:
        os.kill(pid, signal.SIGTERM)
        print(f"stopped pid {pid}")
    except ProcessLookupError:
        print(f"pid {pid} already gone")


def _status() -> None:
    if not GUI_PID.exists():
        print("not running (no pidfile)"); return
    pid = int(GUI_PID.read_text().strip())
    alive = True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        alive = False
    print(f"pid {pid}: {'alive' if alive else 'dead'}")
    if alive and GUI_META.exists():
        try:
            print(json.dumps(json.loads(GUI_META.read_text()), indent=2))
        except Exception:  # noqa: BLE001
            pass


def main(argv: list[str] | None = None) -> int:
    global _HOST, _PORT
    parser = argparse.ArgumentParser(prog="reaper.gui")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--config", default=None)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--serve", action="store_true",
                      help="run the server in the foreground")
    mode.add_argument("--detach", action="store_true",
                      help="start a detached background server (daemon)")
    mode.add_argument("--stop", action="store_true", help="stop the daemon")
    mode.add_argument("--status", action="store_true",
                      help="report daemon status")
    args = parser.parse_args(argv)
    _HOST, _PORT = args.host, args.port

    if args.stop:
        _stop()
        return 0
    if args.status:
        _status()
        return 0
    serve_argv: list[str] = ["--host", args.host, "--port", str(args.port)]
    if args.config:
        serve_argv += ["--config", args.config]
    if args.detach:
        _daemonize(serve_argv)
        return 0

    app = create_app(config_path=args.config, host=args.host, port=args.port)
    print(f"REAPER Run Cockpit → http://{args.host}:{args.port}")
    print("  (start a run in the UI, or POST /api/run/start)")
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

