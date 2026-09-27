"""Tests for Deliverable 5.1 — RenameVariableAgent.

Verifies the agent requests the Submission structured schema (not a bare
string), passes the highlighted-variable context through, parses the LLM
response, and records a tracer event.
"""

from __future__ import annotations

import json

import pytest

from reaper.agents.rename_variable import RenameVariableAgent

from fake_harness import RecordingTracer, StubLLM


class FakeContext:
    def __init__(self):
        self.hits = []

    async def for_variable(self, func_address, var_id, include_callee_renames=False):
        self.hits.append((func_address, var_id, include_callee_renames))
        return (
            f"HLIL of function {func_address}:\n"
            f"  0x1000: >>> var_18 <<< = malloc(0x100)"
        )


_RENAME_JSON = json.dumps({
    "renames": [{
        "node_id": "0x1000:var_18",
        "llm_name": "input_buffer",
        "canon_name": "InputBuffer",
        "justification": "assigned the result of malloc and later read",
    }],
    "claims": [],
})


@pytest.mark.asyncio
async def test_run_requests_submission_schema_and_returns_rename():
    ctx = FakeContext()
    tracer = RecordingTracer()
    llm = StubLLM(responses=[_RENAME_JSON], structured_model_name="Submission")
    agent = RenameVariableAgent(
        llm, ctx, tracer, {"thinking_levels": {"pass1_rename": "minimal"}}
    )

    submission = await agent.run("0x1000", "0x1000:var_18")

    assert len(submission.renames) == 1
    rename = submission.renames[0]
    assert rename.node_id == "0x1000:var_18"
    assert rename.llm_name == "input_buffer"
    assert rename.canon_name == "InputBuffer"

    assert ctx.hits == [("0x1000", "0x1000:var_18", False)]
    # The highlighted context string was actually sent to the LLM.
    sent = [c for c in llm.calls if c["op"] == "send"]
    assert sent and ">>> var_18 <<<" in sent[0]["message"]

    # tracer event recorded
    assert tracer.events and tracer.events[0]["event_type"] == "rename_agent"


@pytest.mark.asyncio
async def test_run_forwards_callee_renames_flag():
    ctx = FakeContext()
    llm = StubLLM(responses=[_RENAME_JSON])
    agent = RenameVariableAgent(llm, ctx, None, {"thinking_levels": {}})

    await agent.run("0x1000", "0x1000:arg0", include_callee_renames=True)

    assert ctx.hits == [("0x1000", "0x1000:arg0", True)]


@pytest.mark.asyncio
async def test_run_uses_minimal_thinking_level():
    llm = StubLLM(responses=[_RENAME_JSON])
    agent = RenameVariableAgent(
        llm, FakeContext(), None,
        {"thinking_levels": {"pass1_rename": "minimal"}},
    )
    await agent.run("0x1000", "0x1000:var_18")
    send = [c for c in llm.calls if c["op"] == "send"]
    assert send and send[0]["message"]
