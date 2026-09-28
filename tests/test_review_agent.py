"""Tests for Deliverable 6.2 — ReviewAgent (reaper/agents/review_agent.py).

Verifies the single structured ReviewOutput call (renames + claims + tasks)
and that the function context passed to the LLM requests claims + callees.
"""

from __future__ import annotations

import json

import pytest

from reaper.agents.review_agent import ReviewAgent

from fake_harness import RecordingTracer, StubLLM


class FakeContext:
    def __init__(self):
        self.func_requests = []

    async def for_function(self, func_address, include_callees=True,
                           include_claims=True, **kwargs):
        self.func_requests.append(func_address)
        return f"REVIEW CONTEXT for {func_address} with claims + callees"


_REVIEW_JSON = json.dumps({
    "renames": [{
        "node_id": "0x1000:var_18", "llm_name": "cursor",
        "canon_name": "Cursor", "justification": "used to walk a list",
    }],
    "claims": [{
        "function_address": "0x1000",
        "claim_text": "var_18 walks a linked list",
        "evidence": [{
            "address_start": "0x1020", "address_end": "0x1028",
            "description": "loop iterates over ->next",
        }],
    }],
    "tasks": [{
        "description": "determine whether arg1 may be NULL",
        "start_position": "0x1000",
        "goal": "resolve the NULL-contract of arg1",
        "graph_refs": ["0x1000"],
        "context_spec": {"functions": ["0x1000"], "include_claims": True},
    }],
})


@pytest.mark.asyncio
async def test_review_agent_returns_structured_output():
    ctx = FakeContext()
    tracer = RecordingTracer()
    llm = StubLLM(responses=[_REVIEW_JSON], structured_model_name="ReviewOutput")
    agent = ReviewAgent(
        llm, ctx, tracer, {"thinking_levels": {"pass2_review": "max"}}
    )

    result = await agent.run("0x1000")

    assert len(result.renames) == 1
    assert result.renames[0].llm_name == "cursor"
    assert len(result.claims) == 1
    assert result.claims[0].evidence[0].address_start == "0x1020"
    assert len(result.tasks) == 1
    assert result.tasks[0].goal and result.tasks[0].start_position == "0x1000"
    assert ctx.func_requests == ["0x1000"]
    assert tracer.events[0]["event_type"] == "review_agent"


@pytest.mark.asyncio
async def test_review_agent_allows_empty_output():
    llm = StubLLM(responses=[json.dumps({"renames": [], "claims": [], "tasks": []})])
    agent = ReviewAgent(llm, FakeContext(), None, {"thinking_levels": {}})
    result = await agent.run("0x2000")
    assert result.renames == [] and result.claims == [] and result.tasks == []


@pytest.mark.asyncio
async def test_review_agent_normalizes_bare_rename_node_id():
    """Mirror of the rename-agent rule: review renames flow straight into the
    merge agent, which reads the STABLE node id — a bare suffix ('var_18')
    would break node resolution. Scope to the reviewed function."""
    bare = json.dumps({
        "renames": [{
            "node_id": "var_18", "llm_name": "cursor",
            "canon_name": "Cursor", "justification": "list cursor",
        }],
        "claims": [],
        "tasks": [],
    })
    llm = StubLLM(responses=[bare])
    agent = ReviewAgent(llm, FakeContext(), None, {"thinking_levels": {}})
    result = await agent.run("0x1000")
    assert len(result.renames) == 1
    assert result.renames[0].node_id == "0x1000:var_18"

