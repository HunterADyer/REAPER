"""Tests for Deliverable 7.2 — InvestigationAgent.

Verifies the InvestigationResult structured output is requested and parsed,
including the task's own context being sent to the LLM.
"""

from __future__ import annotations

import json

import pytest

from reaper.agents.investigation_agent import InvestigationAgent

from fake_harness import RecordingTracer, StubLLM


class FakeContext:
    def __init__(self):
        self.requested = []

    async def for_task(self, task):
        self.requested.append(task)
        return f"INVESTIGATION CONTEXT for task {task.get('id')}"


_RESULT_JSON = json.dumps({
    "answer": "yes, the second argument may be NULL",
    "claims": [{
        "function_address": "0x2000",
        "claim_text": "arg2 is checked for NULL before use",
        "evidence": [{
            "address_start": "0x2020", "address_end": "0x2040",
            "description": "NULL guard before dereference",
        }],
    }],
    "subtasks": [{
        "description": "determine what the callee does with arg2",
        "start_position": "0x2000", "goal": "characterize callee behavior",
        "graph_refs": ["0x2000"],
        "context_spec": {"functions": ["0x2000"], "include_claims": True},
    }],
})


@pytest.mark.asyncio
async def test_investigation_agent_returns_structured_result():
    ctx = FakeContext()
    tracer = RecordingTracer()
    llm = StubLLM(responses=[_RESULT_JSON],
                  structured_model_name="InvestigationResult")
    agent = InvestigationAgent(llm, ctx, None, None, tracer,
                               {"thinking_levels": {"investigation": "high"}})

    result = await agent.run({"id": 42, "description": "is arg2 nullable",
                              "start_position": "0x2000", "goal": "answer"})

    assert result.answer.startswith("yes")
    assert len(result.claims) == 1
    assert result.claims[0].evidence[0].address_start == "0x2020"
    assert len(result.subtasks) == 1
    assert result.subtasks[0].start_position == "0x2000"
    assert ctx.requested[0]["id"] == 42
    assert tracer.events[0]["event_type"] == "investigation_agent"


@pytest.mark.asyncio
async def test_investigation_agent_allows_empty():
    llm = StubLLM(responses=[json.dumps({"answer": "no subtasks", "claims": [],
                                         "subtasks": []})])
    agent = InvestigationAgent(llm, FakeContext(), None, None, RecordingTracer(),
                               {"thinking_levels": {}})
    result = await agent.run({"id": 1})
    assert result.answer == "no subtasks"
    assert result.claims == [] and result.subtasks == []
