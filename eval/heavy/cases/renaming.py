"""Heavy case — RENAME variable agent (Deliverable 5.1, Pass 1 leaf level).

Seeds a stripped-style function whose local variable is a meaningless `var_18`.
Drives the REAL RenameVariableAgent (not the dispatcher) against the seeded
function + variable node, and verifies it proposes a rename for EXACTLY the
target variable (not a random one). In live mode the returned llm_name /
canon_name is scored against the seeded expected name with the modular
`variable-name` rubric over N independent runs: this is the "functionwise"
scoring you asked for, applied to an individual mechanism rather than to a
full pipeline output.
"""

from __future__ import annotations

import json

from reaper.agents.rename_variable import RenameVariableAgent
from reaper.eval.heavy.base import Fixtures, HeavyCase
from reaper.eval.heavy.runner import register

FUNC = "0x4010"
VAR_ID = f"{FUNC}:var_18"
EXPECTED_NAME = "config_index"
EXPECTED_LLM_NAME = "config_index"


class RenameVariableCase(HeavyCase):
    id = "renaming"
    description = "RenameVariableAgent proposes a rename for the exact target variable"
    mechanisms = ("renaming",)

    async def seed(self, fx: Fixtures) -> None:
        fx.neo4j.add_function(FUNC, name="sub_4010",
                              traversal_order=1, scc_id="scc_0")
        fx.neo4j.add_variable(VAR_ID, name="var_18", source="stack_variable")
        fx.extractor._hlils[FUNC] = (
            "0x4010: var_18 = 0\n"
            "0x4014: buffer[var_18] = byte\n"
            "0x4018: var_18 += 1\n"
            "0x401c: if (var_18 < 0x20) then 0x4014 else 0x4020"
        )
        fx.extractor._signatures[FUNC] = "void sub_4010(char* buffer)"
        fx.extractor._variables[FUNC] = [{
            "name": "var_18", "type": "int64_t", "source": "stack_variable",
            "identifier": VAR_ID,
        }]
        fx.extractor._params[FUNC] = [{
            "name": "buffer", "type": "char*", "index": 0,
        }]
        fx.extractor._refs[FUNC] = []
        fx.expected["var_id"] = VAR_ID
        fx.expected["expected_name"] = EXPECTED_NAME

    def scripted_responses(self, fx: Fixtures) -> list[str]:
        return [json.dumps({
            "renames": [{
                "node_id": VAR_ID,
                "llm_name": EXPECTED_LLM_NAME,
                "canon_name": EXPECTED_NAME,
                "justification": "it indexes into the config buffer",
            }],
            "claims": [],
        })]

    async def run_case(self, fx: Fixtures, llm) -> None:
        agent = RenameVariableAgent(llm, fx.context_asm, fx.tracer, fx.config)
        submission = await agent.run(FUNC, VAR_ID)
        fx.extra["submission"] = submission.model_dump()

    async def assert_expected(self, fx: Fixtures) -> list:
        checks = []
        submission = fx.extra.get("submission") or {}
        renames = submission.get("renames") or []
        if not renames:
            checks.append(self.fail("rename agent returned no renames"))
        else:
            # MUST target exactly the seeded variable node.
            targets = {r.get("node_id") for r in renames}
            if VAR_ID not in targets:
                checks.append(self.fail(
                    f"rename targeted {sorted(targets)} instead of {VAR_ID}"))
            else:
                checks.append(self.ok("rename targets the seeded variable"))

        # Offline: scripted response -> exact expected name. Live: the rubric
        # score (score_items) is the quality gate, not string equality.
        pin = None
        for r in renames:
            if r.get("node_id") == VAR_ID:
                pin = r
        if pin is not None and pin.get("canon_name"):
            fx.extra["recovered_name"] = pin.get("canon_name")
            if fx.mode == "offline" and pin.get("canon_name") != EXPECTED_NAME:
                checks.append(self.fail(
                    f"expected canon_name {EXPECTED_NAME!r}, got "
                    f"{pin.get('canon_name')!r}"))
        return checks or [self.ok("rename agent produced a valid target rename")]

    def score_items(self, fx: Fixtures) -> dict[str, list[dict]]:
        recovered = (fx.extra.get("recovered_name") or "")
        return {"variable-name": [{
            "key": VAR_ID,
            "true": EXPECTED_NAME,
            "recovered": recovered,
        }]} if recovered else {}


register(RenameVariableCase)
