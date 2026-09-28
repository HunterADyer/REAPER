"""Heavy case — LOGIC/CONCLUSION engine (ReviewAgent, Deliverable 6.2).

The ReviewAgent is Pass 2's "logic/conclusion" step: given a function's full
context (HLIL + callees + claims) it concludes renames, evidenced claims, and
follow-up tasks in one structured pass. This heavy case seeds a function that
Pass 1 deliberately mislabelled, drives the REAL ReviewAgent, and verifies it
produces a coherent ReviewOutput (well-formed renames/claims/tasks). In live
mode the concluded renames/claims are sanity-scored with the rubric engine.
"""

from __future__ import annotations

import json

from reaper.agents.review_agent import ReviewAgent
from reaper.eval.heavy.base import Fixtures, HeavyCase
from reaper.eval.heavy.runner import register

FUNC = "0x4060"
VAR_A = f"{FUNC}:var_8"
VAR_B = f"{FUNC}:var_10"


class LogicConclusionCase(HeavyCase):
    id = "logic_conclusion"
    description = "ReviewAgent (Pass 2) concludes renames + evidenced claims + tasks"
    mechanisms = ("logic_conclusion", "review_agent", "renaming")

    async def seed(self, fx: Fixtures) -> None:
        fx.neo4j.add_function(FUNC, name="sub_4060", traversal_order=5)
        fx.neo4j.add_function("0x4070", name="sub_4070", traversal_order=3)
        fx.neo4j.add_variable(VAR_A, name="var_8")
        fx.neo4j.add_variable(VAR_B, name="var_10")
        fx.neo4j.add_edge(FUNC, "0x4070", "CALL")
        fx.extractor._hlils[FUNC] = (
            "0x4060: var_8 = call(0x4070)\n"
            "0x4064: var_10 = var_8 + 1\n"
            "0x4068: if (var_10 != 0) then 0x4060 else 0x406c"
        )
        fx.extractor._signatures[FUNC] = "int64_t sub_4060(int64_t seed)"
        fx.extractor._variables[FUNC] = [
            {"name": "var_8", "type": "int64_t", "source": "stack_variable",
             "identifier": VAR_A},
            {"name": "var_10", "type": "int64_t", "source": "stack_variable",
             "identifier": VAR_B},
        ]
        fx.extractor._params[FUNC] = [
            {"name": "seed", "type": "int64_t", "index": 0},
        ]
        fx.extractor._hlils["0x4070"] = "0x4070: return counter"
        fx.extractor._signatures["0x4070"] = "int64_t sub_4070()"
        fx.expected["fn"] = FUNC
        fx.expected["expected_rename"] = VAR_A
        fx.expected["expected_name"] = "seed_value"

    def scripted_responses(self, fx: Fixtures) -> list[str]:
        return [json.dumps({
            "renames": [{
                "node_id": VAR_A, "llm_name": "seed_value",
                "canon_name": "seed_value",
                "justification": "it holds the return of the chained call",
            }],
            "claims": [{
                "function_address": FUNC,
                "claim_text": "this function computes a rolling seed value",
                "evidence": [{"address_start": "0x4060", "address_end": "0x4068",
                              "description": "recursive accumulation"}],
            }],
            "tasks": [{
                "description": "check whether 0x4070 is a PRNG",
                "start_position": "0x4070",
                "goal": "identify the counter source",
                "graph_refs": [],
                "context_spec": {"functions": ["0x4070"], "include_claims": True},
            }],
        })]

    async def run_case(self, fx: Fixtures, llm) -> None:
        agent = ReviewAgent(llm, fx.context_asm, fx.tracer, fx.config)
        output = await agent.run(FUNC)
        fx.extra["review"] = output.model_dump()

    async def assert_expected(self, fx: Fixtures) -> list:
        checks = []
        review = fx.extra.get("review") or {}
        renames = review.get("renames") or []
        claims = review.get("claims") or []
        tasks = review.get("tasks") or []

        if not renames and not claims:
            checks.append(self.fail("review concluded neither renames nor claims"))
        else:
            checks.append(self.ok("review produced a non-empty conclusion"))

        # Every claim must carry evidence (structural honesty invariant).
        for c in claims:
            if not c.get("evidence"):
                checks.append(self.fail(
                    f"claim without evidence: {c.get('claim_text', '')[:50]}"))
        # Every task must carry a start position.
        for t in tasks:
            if not t.get("start_position"):
                checks.append(self.fail("task without start_position"))

        # Offline exact-name check on the seeded target variable; live mode is
        # gated by the rubric score instead.
        pin = next((r for r in renames if r.get("node_id") == VAR_A), None)
        if pin is not None:
            fx.extra["recovered_name"] = pin.get("canon_name") or pin.get("llm_name")
            if fx.mode == "offline" and fx.extra["recovered_name"] != fx.expected.get("expected_name"):
                checks.append(self.fail(
                    f"expected rename {fx.expected['expected_name']!r}, got "
                    f"{fx.extra['recovered_name']!r}"))
        return checks or [self.ok("review concluded a coherent structured output")]

    def score_items(self, fx: Fixtures) -> dict[str, list[dict]]:
        items = []
        for r in (fx.extra.get("review") or {}).get("renames") or []:
            if r.get("canon_name"):
                items.append({"key": r["node_id"], "true": fx.expected.get(
                    "expected_name", ""), "recovered": r["canon_name"]})
        return {"variable-name": items} if items else {}


register(LogicConclusionCase)
