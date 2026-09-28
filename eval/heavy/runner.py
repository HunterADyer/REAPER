"""Heavy-case runner — orchestrate one/all mechanisms, offline or live.

Offline mode: the mechanism is driven with a StubLLM (scripted responses, no
network) and only the MUST-hold checks gate the verdict. Live mode: the real
:8035 vLLM drives the mechanism and the output is ALSO fed back through the
modular LLM-judge scorer (N independent zero-context runs) so the inherent
variance of the model is aggregated into the score.

The runner never modifies repo state: per-case fixtures live in a tmpdir.
"""
# NOTE (ID): cases/ is NOT a pkg yet in pyproject; HeavyCase is imported from
# reaper.eval.heavy.base so we can create the registry in __init__.py instead.

from __future__ import annotations

import asyncio
import json
import logging
import sys
from pathlib import Path

from tests.fake_harness import StubLLM
from reaper.eval.heavy.base import Fixtures, build_fixtures, load_config

log = logging.getLogger(__name__)

#: Registry filled by eval/heavy/cases/*.py -> register().
_REGISTRY: dict[str, type] = {}


def register(case_cls: type) -> type:
    """Register a HeavyCase subclass by its ``id``."""
    case_id = getattr(case_cls, "id", "")
    if not case_id:
        raise ValueError(f"case {case_cls.__name__} has empty id")
    _REGISTRY[case_id] = case_cls
    return case_cls


def list_cases() -> list[tuple[str, str, tuple[str, ...]]]:
    return sorted(
        (cid, cls.description, cls.mechanisms) for cid, cls in _REGISTRY.items()
    )


def get_case(case_id: str):
    if case_id not in _REGISTRY:
        raise KeyError(
            f"unknown heavy case {case_id!r}; registered: {sorted(_REGISTRY)}"
        )
    return _REGISTRY[case_id]


async def run_case(case_id: str, mode: str = "offline", n_runs: int = 5,
                   base_url: str | None = None, model: str | None = None,
                   config_override: dict | None = None) -> dict:
    """Run one heavy case. Returns a JSON-serializable report dict.

    Args:
        case_id : registered case slug
        mode    : "offline" (StubLLM, deterministic, no network) | "live"
        n_runs  : independent LLM-judge scoring runs (live mode)
        base_url/model : vLLM endpoint (live only)
        config_override : deep-merged over configs/default.toml
    """
    case_cls = get_case(case_id)
    case = case_cls()
    fx: Fixtures | None = None
    try:
        fx = await build_fixtures(config_override)
        fx.mode = mode
        await case.seed(fx)

        if mode == "live":
            from reaper.harness.llm_client import ReaperLLMClient

            llm = ReaperLLMClient(
                base_url or "http://localhost:8035/v1",
                model or "deepseek",
                run_id=f"heavy:{case_id}",
            )
        else:
            llm = StubLLM(responses=list(case.scripted_responses(fx)))
        try:
            await case.run_case(fx, llm)

            checks = [
                {"passed": c.passed, "message": c.message}
                for c in await case.assert_expected(fx)
            ]
            report: dict = {
                "case_id": case_id,
                "description": case.description,
                "mechanisms": list(case.mechanisms),
                "mode": mode,
                "checks": checks,
                "passed": bool(checks) and all(c["passed"] for c in checks),
                "expected": fx.expected,
                "trace": getattr(fx.tracer, "events", []),
            }
            # Live mode: score the mechanism output with the rubric engine
            # BEFORE closing the client.
            items = case.score_items(fx)
            if mode == "live" and items:
                from reaper.eval.scoring.scheduler import ScoreScheduler

                scheduler = ScoreScheduler(llm, n_runs=n_runs)
                report["score"] = await scheduler.score(
                    {k: v for k, v in items.items() if v}
                )
        finally:
            try:
                if hasattr(llm, "close"):
                    if asyncio.iscoroutinefunction(llm.close):
                        await llm.close()
                    else:
                        llm.close()
            except Exception:  # noqa: BLE001
                pass
        return report
    except Exception as exc:  # noqa: BLE001
        log.exception("heavy case %s failed", case_id)
        checks = [{"passed": False, "message": f"{type(exc).__name__}: {exc}"}]
        return {
            "case_id": case_id, "mode": mode, "checks": checks,
            "passed": False, "expected": getattr(fx, "expected", {})
            if fx else {},
        }
    finally:
        if fx is not None:
            await fx.close()


async def run_all(mode: str = "offline", n_runs: int = 5,
                  base_url: str | None = None, model: str | None = None,
                  only: list[str] | None = None) -> list[dict]:
    """Run all registered (or a filtered subset of) heavy cases sequentially."""
    cases = [cid for cid, _, _ in list_cases()]
    if only:
        cases = [c for c in cases if c in set(only)]
    reports = []
    for case_id in cases:
        reports.append(await run_case(
            case_id, mode=mode, n_runs=n_runs, base_url=base_url, model=model))
    return reports


def _fmt(report: dict) -> str:
    lines = [f"== {report['case_id']} ({report['mode']}) =="]
    for c in report.get("checks", []):
        mark = "PASS" if c["passed"] else "FAIL"
        lines.append(f"  [{mark}] {c['message']}")
    score = report.get("score")
    if score:
        for dim, sec in (score.get("dimensions") or {}).items():
            lines.append(f"  score[{dim}]: n={sec.get('count')} "
                         f"mean={sec.get('overall_mean')}")
    lines.append(f"  VERDICT: {'PASS' if report.get('passed') else 'FAIL'}")
    return "\n".join(lines)


async def main() -> int:
    import argparse

    p = argparse.ArgumentParser(description="REAPER heavy per-mechanism tests")
    p.add_argument("--case", default=None,
                   help="case id to run (default: all)")
    p.add_argument("--mode", default="offline", choices=["offline", "live"])
    p.add_argument("--n-runs", type=int, default=5)
    p.add_argument("--base-url", default=None)
    p.add_argument("--model", default=None)
    p.add_argument("--out", default="data/heavy_report.json")
    p.add_argument("--list", action="store_true", help="list registered cases")
    args = p.parse_args()

    if args.list:
        for cid, desc, mechs in list_cases():
            print(f"{cid:<24} [{','.join(mechs or ())}]  {desc}")
        return 0

    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    if args.case:
        reports = [await run_case(
            args.case, mode=args.mode, n_runs=args.n_runs,
            base_url=args.base_url, model=args.model)]
    else:
        reports = await run_all(
            mode=args.mode, n_runs=args.n_runs,
            base_url=args.base_url, model=args.model)
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(reports, fh, indent=2)
    for r in reports:
        print(_fmt(r))
    print(f"\nreport written to {args.out}")
    ok = all(r.get("passed", False) for r in reports)
    print("\nALL PASS" if ok and reports else "\nSOME FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
