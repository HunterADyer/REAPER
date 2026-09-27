"""Tests for Deliverable 7.4 — ResynthesisAgent.

Verifies the single ResynthesisResult structured call (contradictions +
merged_claims + new_tasks) and that the supplied subgraph context is forwarded.
"""

from __future__ import annotations

import json

import pytest

from reaper.agents.resynthesis_agent import ResynthesisAgent

from fake_harness import RecordingTracer, StubLLM


_RESULT_JSON = json.dumps({
    "contradictions": [{
        "claim_id_a": 1, "claim_id_b": 2,
        "explanation": "one says NULL allowed, the other forbids it",
    }],
    "merged_claims": [{
        "keep_id": 3, "remove_ids": [4, 5],
        "merged_text": "the buffer is validated before use",
    }],
    "new_tasks": [{
        "description": "resolve the NULL contract",
        "start_position": "0x1000", "goal": "determine whether arg1 may be NULL",
        "graph_refs": ["0x1000"],
        "context_spec": {"functions": ["0x1000"]},
    }],
})


@pytest.mark.asyncio
async def test_resynthesis_agent_returns_structured_result():
    llm = StubLLM(responses=[_RESULT_JSON],
                  structured_model_name="ResynthesisResult")
    tracer = RecordingTracer()
    agent = ResynthesisAgent(llm, None, None, None, tracer,
                             {"thinking_levels": {"resynthesis": "max"}})

    result = await agent.run("SUBGRAPH CONTEXT: 0x1000, 0x2000")

    assert len(result.contradictions) == 1
    assert result.contradictions[0].claim_id_a == 1
    assert len(result.merged_claims) == 1
    assert result.merged_claims[0].remove_ids == [4, 5]
    assert len(result.new_tasks) == 1
    # the context passed to the agent reaches the LLM call.
    send = [c for c in llm.calls if c["op"] == "send"][0]
    assert "SUBGRAPH CONTEXT" in send["message"]
    assert tracer.events[0]["event_type"] == "resynthesis_agent"


@pytest.mark.asyncio
async def test_resynthesis_agent_allows_empty():
    llm = StubLLM(responses=[json.dumps({"contradictions": [], "merged_claims": [],
                                         "new_tasks": []})])
    agent = ResynthesisAgent(llm, None, None, None, RecordingTracer(),
                             {"thinking_levels": {}})
    result = await agent.run("ctx")
    assert result.contradictions == []
    assert result.merged_claims == []
    assert result.new_tasks == []
