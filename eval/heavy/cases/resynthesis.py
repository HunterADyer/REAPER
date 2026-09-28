"""Heavy case — RESYNTHESIS loop (Deliverable 7.4/7.5).

Seeds a resynthesis group (two SCC-linked functions with a contradictory pair
of claims in the ledger), drives the REAL ResynthesisAgent over the seeded
subgraph context, and verifies it detects the contradiction / proposes a merge
and follow-up task. In live mode the concluded merged texts are not run through
a scoring rubric (they are text); the assertion is structural: the mechanism
must produce a well-formed ResynthesisResult.
"""

from __future__ import annotations

import json

from reaper.agents.resynthesis_agent import ResynthesisAgent
from reaper.eval.heavy.base import Fixtures, HeavyCase
from reaper.eval.heavy.runner import register

FUNC_A = "0x4400"
FUNC_B = "0x4410"


class ResynthesisCase(HeavyCase):
    id = "resynthesis"
    description = "ResynthesisAgent detects contradictions/duplicates over a seeded group"
    mechanisms = ("resynthesis",)

    async def seed(self, fx: Fixtures) -> None:
        fx.neo4j.add_function(FUNC_A, name="sub_4400", scc_id="scc_1")
        fx.neo4j.add_function(FUNC_B, name="sub_4410", scc_id="scc_1")
        fx.neo4j.add_edge(FUNC_A, FUNC_B, "CALL")
        fx.extractor._hlils[FUNC_A] = "0x4400: return state"
        fx.extractor._signatures[FUNC_A] = "int64_t sub_4400()"
        fx.extractor._hlils[FUNC_B] = "0x4410: state += 1"
        fx.extractor._signatures[FUNC_B] = "void sub_4410()"

        # Two CONTRADICTORY claims about the same behavior -> resynthesis target.
        fx.extra["claim_a"] = await fx.ledger.add_claim(
            FUNC_A, "this function initializes the global state to zero", "review_a",
            [{"address_start": "0x4400", "address_end": "0x4400",
              "description": "seed"}])
        fx.extra["claim_b"] = await fx.ledger.add_claim(
            FUNC_A, "this function increments a persistent counter each call",
            "review_b", [{"address_start": "0x4400", "address_end": "0x4400",
                          "description": "iteration"}])
        await fx.ledger.set_truth_level(
            fx.extra["claim_a"], "mid_confidence", "critic")
        await fx.ledger.set_truth_level(
            fx.extra["claim_b"], "mid_confidence", "critic")
        fx.expected["claim_a"] = fx.extra["claim_a"]
        fx.expected["claim_b"] = fx.extra["claim_b"]

    def scripted_responses(self, fx: Fixtures) -> list[str]:
        return [json.dumps({
            "contradictions": [{
                "claim_id_a": fx.expected["claim_a"],
                "claim_id_b": fx.expected["claim_b"],
                "explanation": "initializing to zero and incrementing disagree",
            }],
            "merged_claims": [{
                "keep_id": fx.expected["claim_b"],
                "remove_ids": [fx.expected["claim_a"]],
                "merged_text": "this function maintains a persistent counter",
            }],
            "new_tasks": [{
                "description": "confirm whether state starts at zero",
                "start_position": FUNC_A,
                "goal": "resolve the contradiction",
                "graph_refs": [FUNC_A, FUNC_B],
                "context_spec": {"functions": [FUNC_A], "include_claims": True},
            }],
        })]

    async def run_case(self, fx: Fixtures, llm) -> None:
        agent = ResynthesisAgent(llm, fx.context_asm, fx.ledger, fx.todo,
                                 fx.tracer, fx.config)
        context = await fx.context_asm.for_subgraph([FUNC_A, FUNC_B])
        result = await agent.run(context)
        fx.extra["resynthesis"] = result.model_dump()
        # Follow the SAME application path the loop uses for a merged claim.
        from reaper.harness.completion import ResynthesisLoop
        loop = ResynthesisLoop(agent, None, fx.todo, fx.ledger, fx.neo4j,
                               fx.context_asm, fx.tracer, fx.config)
        fx.extra["merged_ids"] = await loop._merge_claim(result.merged_claims[0]) \
            if result.merged_claims else []

    async def assert_expected(self, fx: Fixtures) -> list:
        checks = []
        result = fx.extra.get("resynthesis") or {}
        if not result.get("contradictions"):
            checks.append(self.fail("resynthesis did not flag the seeded contradiction"))
        else:
            checks.append(self.ok("resynthesis flagged the seeded contradiction"))
        if result.get("new_tasks"):
            checks.append(self.ok("resynthesis proposed a follow-up task"))
        else:
            checks.append(self.fail("resynthesis proposed no follow-up task"))
        # Merged claim must have been applied to the ledger.
        if fx.extra.get("merged_ids"):
            for cid in fx.extra["merged_ids"]:
                text = (await fx.ledger.get_claims(FUNC_A)) and [
                    c.get("claim_text") for c in (await fx.ledger.get_claims(FUNC_A))
                    if c["id"] == cid] or []
                if not text:
                    checks.append(self.fail(f"merged claim {cid} not found in ledger"))
        return checks or [self.ok("resynthesis loop behaved as designed")]

    def score_items(self, fx: Fixtures) -> dict[str, list[dict]]:
        return {}


register(ResynthesisCase)
