"""Ground-truth evaluator — Deliverable 8.3.

Compares REAPER's output against ground truth extracted in 8.1 and reports the
design's six metrics:

  1. function name exact match (%)
  2. function name semantic match (exact + LLM judge)
  3. variable name accuracy (exact + optional LLM judge; matched by function
     address + relative position in the variable list)
  4. claim coverage (% of functions with >=1 claim at mid_confidence+)
  5. type recovery accuracy (% of ground-truth struct fields matched by offset
     + size; name match not required)
  6. false confidence rate (of high_confidence claims judged wrong)

Everything is pure except the optional semantic-judge steps. ``judge`` is any
object with ``async def yes_no(self, question) -> bool``; the built-in
``LLMJudge`` wraps the REAPER LLM client with a ``{"equivalent": bool}``
structured schema. When no judge is supplied, semantic metrics collapse to
exact only (never invented).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel

from reaper.harness.llm_client import ReaperLLMClient
from reaper.harness.submission import get_schema, parse_response

log = logging.getLogger(__name__)

_MID_OR_ABOVE = {"mid_confidence", "high_confidence"}


class _JudgeYesNo(BaseModel):
    equivalent: bool


class LLMJudge:
    """LLM-based semantic equivalence judge (minimal thinking)."""

    def __init__(self, llm_client: ReaperLLMClient):
        self.llm = llm_client
        self._n_calls = 0

    async def yes_no(self, question: str) -> bool:
        self._n_calls += 1
        session_id = f"judge_{self._n_calls}"
        await self.llm.create_session(session_id, "You answer yes/no honestly.")
        try:
            response = await self.llm.send(
                session_id, question, thinking_level="minimal",
                structured_output=get_schema(_JudgeYesNo),
            )
            return bool(parse_response(_JudgeYesNo, response).equivalent)
        finally:
            self.llm.destroy_session(session_id)

    async def close(self):
        try:
            await self.llm.close()
        except Exception:
            pass


@dataclass
class Metrics:
    total_functions: int = 0
    exact_names: int = 0
    semantic_names: int = 0
    exact_vars: int = 0
    total_vars: int = 0
    semantic_vars: int = 0
    covered_claim_functions: int = 0       # functions with >=1 mid+ claim
    total_struct_fields: int = 0
    matched_struct_fields: int = 0
    high_confidence_claims: int = 0
    wrong_high_confidence_claims: int = 0
    notes: list[str] = field(default_factory=list)


def _sem(value: str) -> str:
    """Case/space insensitive stem for exact-equality comparisons."""
    return (value or "").strip().lower()


def _function_exact(gt_name: str, reaper_canon: str) -> bool:
    return bool(gt_name) and bool(reaper_canon) and _sem(gt_name) == _sem(reaper_canon)


def _reaper_vars(reaper_fn: dict) -> list[dict]:
    """Ordered variable list (parameters first) from a REAPER function record."""
    out = []
    for key in ("parameters", "variables"):
        out.extend(reaper_fn.get(key) or [])
    return out


def _gt_vars(gt_fn: dict) -> list[dict]:
    """Ordered ground-truth variable list: parameters then local variables."""
    out = []
    for key in ("parameters", "local_variables"):
        out.extend(gt_fn.get(key) or [])
    return out


async def compute_metrics(ground_truth: dict, reaper_output: dict,
                          judge=None) -> Metrics:
    """Compute Design § 8.3 metrics from the two JSON-style structures.

    ``reaper_output`` is a dict of ``address -> {"canon_name", "variables",
    "claims", "fields"}``; ``ground_truth`` is the 8.1 output plus an OPTIONAL
    ``"structs"`` key for metric 5.
    """
    m = Metrics()
    for addr, gt_fn in ground_truth.items():
        if not isinstance(gt_fn, dict) or "function_name" not in gt_fn:
            continue
        gt_name = (gt_fn.get("function_name") or "").strip()
        rp = reaper_output.get(addr) or {}
        m.total_functions += 1

        # 1+2. function names
        rc = (rp.get("canon_name") or "").strip()
        if _function_exact(gt_name, rc):
            m.exact_names += 1
            m.semantic_names += 1
        elif rc and judge is not None and gt_name:
            if await judge.yes_no(
                f"Ground truth: `{gt_name}`. REAPER: `{rc}`. "
                "Are these semantically equivalent names for the same function? "
                "Answer only yes or no."
            ):
                m.semantic_names += 1

        # 3. variables by address + relative position
        gt_vars = _gt_vars(gt_fn)
        rp_vars = _reaper_vars(rp)
        for i, rv in enumerate(rp_vars):
            if i >= len(gt_vars):
                break
            rname = (rv.get("canon_name") or rv.get("llm_name") or "").strip()
            gname = (gt_vars[i].get("name") or "").strip()
            if not rname or not gname:
                continue
            m.total_vars += 1
            if _sem(rname) == _sem(gname):
                m.exact_vars += 1
                m.semantic_vars += 1
            elif judge is not None:
                if await judge.yes_no(
                    f"Ground truth variable: `{gname}`. REAPER: `{rname}`. "
                    "Are these semantically equivalent variable names? "
                    "Answer only yes or no."
                ):
                    m.semantic_vars += 1

    # 4. claim coverage
    for addr in ground_truth:
        gt_fn = ground_truth[addr]
        if not isinstance(gt_fn, dict) or "function_name" not in gt_fn:
            continue
        rp = reaper_output.get(addr) or {}
        claims = rp.get("claims") or []
        if any(c.get("truth_level") in _MID_OR_ABOVE for c in claims):
            m.covered_claim_functions += 1

    # 5. type recovery accuracy (offset + size only)
    gt_structs = ground_truth.get("structs") or {}
    rp_structs = reaper_output.get("structs") or {}
    for sname, gt_fields in gt_structs.items():
        rp_fields = rp_structs.get(sname) or []
        keyed = {(int(f.get("offset", -1)), int(f.get("size", -1)))
                 for f in rp_fields}
        for gf in gt_fields or []:
            m.total_struct_fields += 1
            if (int(gf.get("offset", -1)), int(gf.get("size", -1))) in keyed:
                m.matched_struct_fields += 1

    # 6. false confidence rate
    for addr in ground_truth:
        gt_fn = ground_truth[addr]
        if not isinstance(gt_fn, dict) or "function_name" not in gt_fn:
            continue
        gt_name = (gt_fn.get("function_name") or "").strip()
        rp = reaper_output.get(addr) or {}
        for c in rp.get("claims") or []:
            if c.get("truth_level") != "high_confidence":
                continue
            m.high_confidence_claims += 1
            if judge is not None:
                ok = await judge.yes_no(
                    f"Ground truth: this function is `{gt_name}`. "
                    f"REAPER high-confidence claim: `{c.get('claim_text')}`. "
                    "Is the claim correct for this function? Answer only yes or no."
                )
                if not ok:
                    m.wrong_high_confidence_claims += 1
    return m


def to_report(m: Metrics) -> dict:
    """Serialize metrics into a JSON-reportable dict."""
    def pct(a, b):
        return round(100.0 * a / b, 1) if b else 0.0

    return {
        "function_name_exact_match": pct(m.exact_names, m.total_functions),
        "function_name_semantic_match": pct(m.semantic_names, m.total_functions),
        "function_total": m.total_functions,
        "variable_name_exact_match": pct(m.exact_vars, m.total_vars),
        "variable_name_semantic_match": pct(m.semantic_vars, m.total_vars),
        "variable_total": m.total_vars,
        "claim_coverage": pct(m.covered_claim_functions, m.total_functions),
        "type_recovery_accuracy": pct(m.matched_struct_fields, m.total_struct_fields),
        "struct_fields_total": m.total_struct_fields,
        "high_confidence_claims": m.high_confidence_claims,
        "false_confidence_rate": pct(m.wrong_high_confidence_claims,
                                     m.high_confidence_claims),
        "notes": list(m.notes),
    }


def format_report(report: dict) -> str:
    lines = ["REAPER ground-truth evaluation", "=" * 40]
    lines.append(
        f"  function name exact match:     {report['function_name_exact_match']}%")
    lines.append(
        f"  function name semantic match:  {report['function_name_semantic_match']}%")
    lines.append(
        f"  variable name exact match:     {report['variable_name_exact_match']}%")
    lines.append(
        f"  variable name semantic match:  {report['variable_name_semantic_match']}%")
    lines.append(f"  claim coverage:                {report['claim_coverage']}%")
    lines.append(
        f"  type recovery accuracy:        {report['type_recovery_accuracy']}%")
    lines.append(
        f"  false confidence rate:         {report['false_confidence_rate']}%")
    if report.get("notes"):
        lines.append("notes:")
        lines.extend(f"  - {n}" for n in report["notes"])
    return "\n".join(lines)


async def _load(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    return data if isinstance(data, dict) else {}


async def main() -> int:
    parser = argparse.ArgumentParser(
        description="Evaluate REAPER output against ground truth (8.3).")
    parser.add_argument("--ground-truth", required=True)
    parser.add_argument("--reaper-output", required=True,
                        help="JSON: {address: {canon_name, variables, claims, "
                             "fields}} plus optional 'structs'")
    parser.add_argument("--llm", action="store_true",
                        help="use the LLM judge for semantic metrics")
    parser.add_argument("--base-url", default="http://localhost:8035/v1",
                        help="vLLM OpenAI-compatible base URL (default matches "
                             "the live shared vLLM endpoint)")
    parser.add_argument("--model", default="deepseek",
                        help="model name served by --base-url (default matches "
                             "the live shared vLLM deployment)")
    args = parser.parse_args()

    ground_truth = await _load(args.ground_truth)
    reaper_output = await _load(args.reaper_output)
    judge = None
    if args.llm:
        judge = LLMJudge(ReaperLLMClient(args.base_url, args.model))
    try:
        metrics = await compute_metrics(ground_truth, reaper_output, judge=judge)
    finally:
        if judge is not None:
            await judge.close()
    report = to_report(metrics)
    print(format_report(report))
    with open("evaluation_report.json", "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2)
    return 0


if __name__ == "__main__":
    asyncio.run(main())

