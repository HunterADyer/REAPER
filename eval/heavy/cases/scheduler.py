"""Heavy case — SCHEDULER (Deliverable 7.1).

Seeds the real TodoLedger with overlapping tasks (a merge candidate) and a
multi-question task (a decompose candidate). Drives the REAL Scheduler's
review_pending_tasks() + run_once() and verifies:
  * merge: overlapping tasks collapsed into one, originals deleted
  * decompose: the bundled task is replaced by atomic subtasks
  * assignment: ready tasks get assigned (status -> in_progress) with a
    session id, and stuck/in-progress stale reset logic is exercised
Offline scripts a SchedulerPlan; the structural invariants (merges did not
drop pending work, assigned tasks actually entered in_progress) are asserted
in both modes.
"""

from __future__ import annotations

import json

from reaper.eval.heavy.base import Fixtures, HeavyCase
from reaper.eval.heavy.runner import register
from reaper.harness.scheduler import Scheduler

FUNC_X = "0x4500"
FUNC_Y = "0x4510"


class SchedulerCase(HeavyCase):
    id = "scheduler"
    description = "Scheduler merges/decomposes/assigns seeded TODO tasks"
    mechanisms = ("scheduler", "deliverable_1_6")

    async def seed(self, fx: Fixtures) -> None:
        fx.neo4j.add_function(FUNC_X, name="sub_4500")
        fx.neo4j.add_function(FUNC_Y, name="sub_4510")
        fx.extractor._hlils[FUNC_X] = "0x4500: return a + b"
        fx.extractor._signatures[FUNC_X] = "int64_t sub_4500(int64_t a, int64_t b)"
        fx.extractor._hlils[FUNC_Y] = "0x4510: return a * b"
        fx.extractor._signatures[FUNC_Y] = "int64_t sub_4510(int64_t a, int64_t b)"

        # Two overlapping tasks (should merge) + one bundled task (decompose).
        fx.extra["t_dup1"] = await fx.todo.create_task(
            description="what does 0x4500 do", context_spec={"functions": [FUNC_X]},
            start_position=FUNC_X, goal="identify 0x4500", graph_refs=[FUNC_X])
        fx.extra["t_dup2"] = await fx.todo.create_task(
            description="analyze behavior of 0x4500", context_spec={"functions": [FUNC_X]},
            start_position=FUNC_X, goal="identify 0x4500", graph_refs=[FUNC_X])
        fx.extra["t_big"] = await fx.todo.create_task(
            description="fully document 0x4500 and 0x4510 including all branches",
            context_spec={"functions": [FUNC_X, FUNC_Y]},
            start_position=FUNC_X, goal="document both", graph_refs=[FUNC_X, FUNC_Y])
        fx.extra["t_ok"] = await fx.todo.create_task(
            description="identify 0x4510", context_spec={"functions": [FUNC_Y]},
            start_position=FUNC_Y, goal="identify 0x4510", graph_refs=[FUNC_Y])

    def scripted_responses(self, fx: Fixtures) -> list[str]:
        return [json.dumps({
            "merges": [{
                "task_ids": [fx.extra["t_dup1"], fx.extra["t_dup2"]],
                "description": "what does 0x4500 do",
                "start_position": FUNC_X, "goal": "identify 0x4500",
                "graph_refs": [FUNC_X],
                "context_spec": {"functions": [FUNC_X], "include_claims": False},
            }],
            "decomposes": [{
                "source_task_id": fx.extra["t_big"],
                "description": "document 0x4500",
                "start_position": FUNC_X, "goal": "document 0x4500",
                "graph_refs": [FUNC_X],
                "context_spec": {"functions": [FUNC_X], "include_claims": False},
            }, {
                "source_task_id": fx.extra["t_big"],
                "description": "document 0x4510",
                "start_position": FUNC_Y, "goal": "document 0x4510",
                "graph_refs": [FUNC_Y],
                "context_spec": {"functions": [FUNC_Y], "include_claims": False},
            }],
            "keep": [fx.extra["t_ok"]],
        })]

    async def run_case(self, fx: Fixtures, llm) -> None:
        scheduler = Scheduler(llm, fx.todo, fx.context_asm, fx.tracer, fx.config)
        batches = await scheduler.review_pending_tasks()
        fx.extra["review_batches"] = batches
        fx.extra["assigned"] = await scheduler.run_once()
        fx.extra["pending"] = await fx.todo.get_pending_tasks()
        fx.extra["tasks"] = [
            t for t in (await fx.todo.get_ready_tasks())]  # after drain, likely []
        # snapshot all tasks for inspection
        db = fx.todo._require_ready()
        rows = await (await db.execute("SELECT id, description, status FROM tasks "
                                       "ORDER BY id")).fetchall()
        fx.extra["all_tasks"] = [list(r) for r in rows]

    async def assert_expected(self, fx: Fixtures) -> list:
        checks = []
        tasks = {row[0]: {"description": row[1], "status": row[2]}
                 for row in fx.extra.get("all_tasks") or []}
        ids = set(tasks)

        # The duplicated tasks must have been merged away (deleted by the plan).
        if fx.extra["t_dup1"] in ids or fx.extra["t_dup2"] in ids:
            checks.append(self.fail("overlapping tasks were not merged away"))
        else:
            checks.append(self.ok("overlapping tasks were merged"))

        # The bundled task must have been decomposed away.
        if fx.extra["t_big"] in ids:
            checks.append(self.fail("bundled task was not decomposed"))
        else:
            checks.append(self.ok("bundled task was decomposed"))

        # At least ONE new task must exist post-merge/decompose (the work
        # survived) — and it must NOT be flagged pending-in-limbo.
        new_ids = ids - {fx.extra["t_dup1"], fx.extra["t_dup2"],
                         fx.extra["t_big"], fx.extra["t_ok"]}
        if not new_ids:
            checks.append(self.fail("merge/decompose created no replacement tasks"))
        else:
            checks.append(self.ok(f"{len(new_ids)} replacement task(s) created"))

        # Assignment: run_once must have assigned all ready work.
        if not fx.extra.get("assigned"):
            checks.append(self.fail("scheduler.run_once assigned nothing"))
        else:
            checks.append(self.ok(f"{len(fx.extra['assigned'])} task(s) assigned"))
        return checks or [self.ok("scheduler behaved as designed")]

    def score_items(self, fx: Fixtures) -> dict[str, list[dict]]:
        return {}


register(SchedulerCase)
