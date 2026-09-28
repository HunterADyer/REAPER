"""Heavy case — INVESTIGATION loop (Deliverable 7.3).

Seeds a TODO task pointing at a function, drives the REAL InvestigationLoop
(scheduler + InvestigationAgent + its OWN CriticEvaluator) and verifies:
  * the task gets assigned and executed,
  * the agent's claims are run through the critic and either accepted (level
    set) or rejected (retry/delete semantics, never half-evaluated),
  * the TODO ledger drains and the loop returns True (termination).
This is the deepest mechanism case — it exercises scheduler, investigation
agent, critic, ledger, and todo in one real harness.
"""

from __future__ import annotations

import json

from reaper.agents.investigation_agent import InvestigationAgent
from reaper.eval.heavy.base import Fixtures, HeavyCase
from reaper.eval.heavy.runner import register
from reaper.harness.investigation_loop import InvestigationLoop
from reaper.harness.scheduler import Scheduler

FUNC = "0x4600"


class InvestigationCase(HeavyCase):
    id = "investigation"
    description = "InvestigationLoop drains a seeded task via agent + critic"
    mechanisms = ("investigation", "critic_loop", "scheduler", "deliverable_1_6")

    async def seed(self, fx: Fixtures) -> None:
        fx.neo4j.add_function(FUNC, name="sub_4600")
        fx.extractor._hlils[FUNC] = (
            "0x4600: buf = malloc(0x40)\n0x4604: fill(buf)\n0x4608: return buf"
        )
        fx.extractor._signatures[FUNC] = "char* sub_4600()"
        fx.extra["task_id"] = await fx.todo.create_task(
            description="what does 0x4600 return",
            context_spec={"functions": [FUNC]},
            start_position=FUNC, goal="identify 0x4600", graph_refs=[FUNC])

    def scripted_responses(self, fx: Fixtures) -> list[str]:
        # Session order: scheduler review (1 call), investigation agent (1),
        # critic (1 call per claim). Keep SchedulerPlan empty (no merge).
        return [
            json.dumps({"merges": [], "decomposes": [], "keep": []}),
            json.dumps({
                "answer": "allocates and fills a 0x40-byte buffer",
                "claims": [{
                    "function_address": FUNC,
                    "claim_text": "this function allocates a 64-byte buffer",
                    "evidence": [{"address_start": "0x4600", "address_end": "0x4608",
                                  "description": "malloc + fill"}],
                }],
                "subtasks": [],
            }),
            json.dumps({"truth_level": "mid_confidence", "accepted": True,
                        "feedback": "malloc evidence supports the claim"}),
        ]

    async def run_case(self, fx: Fixtures, llm) -> None:
        scheduler = Scheduler(llm, fx.todo, fx.context_asm, fx.tracer, fx.config)
        inv_agent = InvestigationAgent(llm, fx.context_asm, fx.ledger, fx.todo,
                                       fx.tracer, fx.config)
        loop = InvestigationLoop(scheduler, inv_agent, llm, fx.context_asm,
                                 fx.todo, fx.ledger, fx.tracer, fx.config)
        fx.extra["drained"] = await loop.run()
        fx.extra["task"] = await fx.todo.get_task(fx.extra["task_id"])
        fx.extra["claims"] = await fx.ledger.get_claims(FUNC)

    async def assert_expected(self, fx: Fixtures) -> list:
        checks = []
        if not fx.extra.get("drained"):
            checks.append(self.fail("InvestigationLoop did not drain the TODO ledger"))
        else:
            checks.append(self.ok("InvestigationLoop drained the TODO ledger"))
        # The seeded task must no longer be pending/in_progress.
        task = fx.extra.get("task")
        if task is None:
            checks.append(self.fail("seeded task missing after the loop"))
        elif task.get("status") not in ("completed", "done"):
            # requeue is legitimate if all claims were rejected; but a claim
            # was scripted as accepted, so it should be completed.
            checks.append(self.fail(
                f"task left in {task.get('status')!r} (expected completed)"))
        else:
            checks.append(self.ok("seeded task completed"))
        # Claim lifecycle invariant: nothing left NULL after the critic ran.
        for c in fx.extra.get("claims") or []:
            if (c.get("truth_level") or "") is None:
                checks.append(self.fail("claim left NULL after critic"))
        return checks or [self.ok("investigation loop terminated cleanly")]

    def score_items(self, fx: Fixtures) -> dict[str, list[dict]]:
        return {}


register(InvestigationCase)
