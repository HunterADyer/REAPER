"""Tests for Deliverable 6.3 — Pass2Dispatcher (reaper/harness/pass2_dispatcher.py).

Walks the review → critic → (task) → merge pipeline against doubles. Verifies
accepted-claim flow (renames merged, claim accepted, task created), the
all-rejected flow (renames dropped because the review failed), and the
no-claims flow (renames merged on their own). The merge agent itself is
doubled — its internals are covered by test_merge_agent.py.
"""

from __future__ import annotations

import json

import pytest

from reaper.harness.pass2_dispatcher import Pass2Dispatcher

from conftest import RecordingNeo4jDriver
from fake_harness import RecordingTracer, StubLLM


class FakeContext:
    async def for_function(self, func_address, include_callees=True,
                           include_claims=True, **kwargs):
        return f"REVIEW CONTEXT {func_address}"

    async def for_evidence(self, evidence_links):
        return "EVIDENCE: " + "; ".join(
            e.get("address_start", "?") for e in evidence_links
        )


class StubLedger:
    def __init__(self):
        self.claims = []
        self.levels = []
        self.deleted = []
        self._next = 1

    async def add_claim(self, function_address, claim_text, submitted_by, evidence):
        claim_id = self._next
        self._next += 1
        self.claims.append({"id": claim_id, "function_address": function_address,
                            "claim_text": claim_text})
        return claim_id

    async def set_truth_level(self, claim_id, truth_level, reviewed_by):
        self.levels.append((claim_id, truth_level))

    async def delete_claim(self, claim_id):
        self.deleted.append(claim_id)


class StubTodo:
    def __init__(self):
        self.tasks = []

    async def create_task(self, description, context_spec, start_position,
                          goal, graph_refs=None, depends_on=None):
        self.tasks.append({"description": description, "start_position": start_position,
                           "goal": goal, "context_spec": context_spec,
                           "graph_refs": graph_refs})
        return len(self.tasks)


class RecordingMerge:
    def __init__(self):
        self.calls = []

    async def attempt_merge(self, session_id, submission):
        self.calls.append((session_id, submission))
        return {"status": "applied"}


def _driver():
    def respond(query, params):
        if "f.traversal_order AS traversal_order" in query:
            return [{"address": "0x1000", "traversal_order": 0}]
        return []

    return RecordingNeo4jDriver(respond)


def _review_json(rename=True, claim=True, task=True):
    return json.dumps({
        "renames": [{
            "node_id": "0x1000:var_18", "llm_name": "cursor",
            "canon_name": "Cursor", "justification": "walks the list",
        }] if rename else [],
        "claims": [{
            "function_address": "0x1000",
            "claim_text": "var_18 walks a linked list",
            "evidence": [{
                "address_start": "0x1020", "address_end": "0x1028",
                "description": "loop iterates over ->next",
            }],
        }] if claim else [],
        "tasks": [{
            "description": "resolve NULL contract of arg1",
            "start_position": "0x1000",
            "goal": "determine whether arg1 may be NULL",
            "graph_refs": ["0x1000"],
            "context_spec": {"functions": ["0x1000"], "include_claims": True},
        }] if task else [],
    })


def _verdict(accepted):
    return json.dumps({"truth_level": "mid_confidence", "accepted": accepted,
                       "feedback": "ok"})


def _dispatcher(llm, limits=None):
    config = {"limits": {"max_critic_rejections": 3, "max_concurrent_agents": 4,
                         **({"max_review_retries": limits} if limits is not None else {})},
              "thinking_levels": {"pass2_review": "max", "critic": "high"}}
    ledger = StubLedger()
    todo = StubTodo()
    merge = RecordingMerge()
    d = Pass2Dispatcher(llm, _driver(), FakeContext(), None, merge, None,
                        ledger, todo, RecordingTracer(), config)
    return d, ledger, todo, merge


@pytest.mark.asyncio
async def test_accepted_claim_merges_renames_and_creates_task():
    llm = StubLLM(responses=[
        _review_json(rename=True, claim=True, task=True),
        _verdict(True),   # claim critic
        _verdict(True),   # task critic
    ])
    dispatcher, ledger, todo, merge = _dispatcher(llm)
    await dispatcher.run()

    # claim inserted + accepted (truth level set, not deleted)
    assert len(ledger.claims) == 1
    assert len(ledger.levels) == 1 and ledger.levels[0][1] != "speculation"
    assert not ledger.deleted

    # merge got ONLY the accepted renames (no claims — already in ledger)
    assert len(merge.calls) == 1
    session, submission = merge.calls[0]
    assert session == "pass2_0x1000"
    assert len(submission.renames) == 1
    assert submission.renames[0].node_id == "0x1000:var_18"
    assert submission.claims == []

    # task critically accepted then created in the todo ledger
    assert len(todo.tasks) == 1
    assert "arg1 may be NULL" in todo.tasks[0]["goal"]


@pytest.mark.asyncio
async def test_all_claims_rejected_still_merges_renames():
    # Rename merging is DECOUPLED from claim acceptance: a rejected review's
    # renames are independent work and are still merge-agent-verified. With no
    # retry budget the single review's renames are merged regardless.
    llm = StubLLM(responses=[
        _review_json(rename=True, claim=True, task=False),
        _verdict(False),  # claim rejected (and deleted by the critic)
    ])
    dispatcher, ledger, todo, merge = _dispatcher(llm, limits=0)
    await dispatcher.run()

    assert len(ledger.claims) == 1
    assert ledger.deleted == [1]  # rejected claim deleted by critic
    # renames merged even though every claim was rejected
    assert len(merge.calls) == 1
    session, submission = merge.calls[0]
    assert session == "pass2_0x1000"
    assert len(submission.renames) == 1
    assert submission.claims == []  # claims stay in the ledger, not the merge
    assert todo.tasks == []


@pytest.mark.asyncio
async def test_all_claims_rejected_retries_with_feedback_then_accepts():
    """Design § 6.1 retry-with-feedback: an all-rejected review re-invokes the
    review agent with the critic's feedback before the budget runs out."""
    llm = StubLLM(responses=[
        _review_json(rename=True, claim=True, task=False),
        _verdict(False),  # first claim rejected
        _review_json(rename=True, claim=True, task=False),
        _verdict(True),   # second attempt's claim accepted
    ])
    dispatcher, ledger, todo, merge = _dispatcher(llm, limits=2)
    await dispatcher.run()

    assert ledger.deleted == [1]          # first attempt's claim rejected+deleted
    assert len(ledger.levels) == 1        # only the second attempt was accepted
    assert ledger.levels[0][0] == 2       # claim id 2
    # exactly ONE merge (the final attempt's renames — no double-merge)
    assert len(merge.calls) == 1
    assert len(merge.calls[0][1].renames) == 1

    # The retry carried the critic's feedback into the review agent's context.
    sends = [c for c in llm.calls if c["op"] == "send"]
    assert len(sends) == 4  # review1, critic1, review2, critic2
    assert "PREVIOUS ATTEMPT WAS REJECTED" in sends[2]["message"]


@pytest.mark.asyncio
async def test_no_claims_renames_merged_directly():
    llm = StubLLM(responses=[
        _review_json(rename=True, claim=False, task=False),
    ])
    dispatcher, ledger, todo, merge = _dispatcher(llm)
    await dispatcher.run()

    assert ledger.claims == []
    assert len(merge.calls) == 1
    assert len(merge.calls[0][1].renames) == 1
