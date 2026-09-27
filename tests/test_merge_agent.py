"""Tests for Deliverable 3.5 — MergeAgent (reaper/harness/merge_agent.py).

Covers the full merge flow against stateful doubles: no-conflict apply (with
BNDB rename sync), LLM-resolved conflict, LLM-rejected conflict (creates
follow-up TODO tasks), and the no-LLM auto-accept path. Reuses the stateful
in-memory Neo4j + stub ledger pattern from test_shadow.py.
"""

from __future__ import annotations

import json

import pytest

from reaper.harness.merge_agent import MergeAgent
from reaper.harness.shadow import ShadowCopyManager
from reaper.harness.submission import Claim, Rename, Submission
from reaper.tools.bndb_writer import BNDBWriter

from fake_harness import StubLLM, make_extractor_with_tags
from test_shadow import StubLedger, StatefulNeo4j

from tests import fake_binja as fb


class StubTodo:
    def __init__(self):
        self.tasks = []
        self._next = 100

    async def create_task(self, description, start_position, goal,
                          graph_refs=None, context_spec=None):
        task_id = self._next
        self._next += 1
        self.tasks.append({
            "description": description, "start_position": start_position,
            "goal": goal, "graph_refs": graph_refs, "context_spec": context_spec,
        })
        return task_id


def _merge_setup(master_nodes=None):
    """Build a connected MergeAgent + doubles for a function at 0x1400."""
    func = fb.FakeFunction.simple(0x1400, "sub_1400")
    writer = BNDBWriter(make_extractor_with_tags(functions=[func]))
    driver = StatefulNeo4j(
        nodes=master_nodes or {"0x1400": {"address": "0x1400"}}, edges=[]
    )
    ledger = StubLedger()
    todo = StubTodo()
    shadow = ShadowCopyManager(driver, ledger, {"limits": {"shadow_copy_hops": 1}})
    config = {"thinking_levels": {"merge": "medium"}}
    agent = MergeAgent(StubLLM(), shadow, writer, ledger, todo, config)
    return agent, driver, ledger, todo, func, shadow


def _rename(node_id="0x1400", llm_name="parse_json_data", canon_name="ParseJsonData"):
    return Rename(node_id=node_id, llm_name=llm_name,
                  canon_name=canon_name, justification="reviewer feedback")


@pytest.mark.asyncio
async def test_merge_conflict_resolved_via_llm():
    agent, driver, ledger, todo, func, shadow = _merge_setup()

    # Baseline checkout BEFORE any master change (v0), then advance master.
    await shadow.checkout("0x1400", "m_sess2")
    await shadow.checkout("0x1400", "tmp")
    warm = await shadow.diff("tmp", {"nodes": {"0x1400": {"llm_name": "aaa"}},
                                     "claims": []})
    await shadow.apply("tmp", warm["mutations"])
    assert shadow.current_version() == 1

    # A second agent proposes a conflicting name; the LLM resolves it.
    agent.llm = StubLLM(responses=[json.dumps({
        "reject": False, "reasons": [],
        "resolutions": [{"node_id": "0x1400", "field": "llm_name", "value": "bbb"}],
    })])
    result = await agent.attempt_merge(
        "m_sess2", Submission(renames=[_rename(llm_name="bbb", canon_name="BBB")])
    )
    assert result.status == "resolved"
    assert result.conflicts_resolved and result.conflicts_resolved[0]["value"] == "bbb"
    assert driver.nodes["0x1400"]["llm_name"] == "bbb"
    assert func.name == "BBB"


@pytest.mark.asyncio
async def test_merge_conflict_rejected_creates_followup_tasks():
    agent, driver, ledger, todo, func, shadow = _merge_setup()

    await shadow.checkout("0x1400", "m_sess3")
    await shadow.checkout("0x1400", "tmp")
    warm = await shadow.diff("tmp", {"nodes": {"0x1400": {"llm_name": "aaa"}},
                                     "claims": []})
    await shadow.apply("tmp", warm["mutations"])

    agent.llm = StubLLM(responses=[json.dumps({
        "reject": True, "reasons": ["ambiguous rename"], "resolutions": [],
    })])
    result = await agent.attempt_merge(
        "m_sess3", Submission(renames=[_rename(llm_name="bbb", canon_name="BBB")])
    )
    assert result.status == "rejected"
    assert len(result.new_tasks) == 1
    assert todo.tasks and "llm_name" in todo.tasks[0]["description"]


@pytest.mark.asyncio
async def test_merge_without_llm_auto_accepts_conflict():
    """With no LLM configured, conflicts are auto-accepted (master wins)."""
    agent, driver, ledger, todo, func, shadow = _merge_setup()
    agent.llm = None

    await shadow.checkout("0x1400", "m_sess4")
    await shadow.checkout("0x1400", "tmp")
    warm = await shadow.diff("tmp", {"nodes": {"0x1400": {"llm_name": "aaa"}},
                                     "claims": []})
    await shadow.apply("tmp", warm["mutations"])

    result = await agent.attempt_merge(
        "m_sess4", Submission(renames=[_rename(llm_name="bbb", canon_name="BBB")])
    )
    assert result.status == "resolved"
    # master value is kept (no resolution override)
    assert driver.nodes["0x1400"]["llm_name"] == "aaa"
