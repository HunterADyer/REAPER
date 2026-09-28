"""Heavy case — TYPE RECOVERY (Deliverable 4.2/4.3).

Seeds a StructCandidate (the exact model StructAccessDetector produces) with
known field accesses across functions, drives the REAL TypeRecoveryAgent to
infer a StructDefinition, and verifies the struct is (a) returned, (b) recorded
to the ledger for metric-5 export, and (c) a claim is registered per involved
function. Live mode scores the recovered layout against the seeded ground truth
with the `datatype` rubric over N independent runs — this is the "with the
datatype" scoring dimension applied at the mechanism level.
"""

from __future__ import annotations

import json

from reaper.agents.type_recovery import TypeRecoveryAgent
from reaper.eval.heavy.base import Fixtures, HeavyCase
from reaper.eval.heavy.runner import register
from reaper.tools.struct_detector import FieldAccess, StructCandidate

FUNC_A = "0x4300"
FUNC_B = "0x4310"


class TypeRecoveryCase(HeavyCase):
    id = "type_recovery"
    description = "TypeRecoveryAgent infers a struct from seeded field accesses"
    mechanisms = ("type_recovery",)

    async def seed(self, fx: Fixtures) -> None:
        fx.neo4j.add_function(FUNC_A, name="sub_4300")
        fx.neo4j.add_function(FUNC_B, name="sub_4310")
        fx.extractor._hlils[FUNC_A] = (
            "0x4300: r = *(rax + 0x0)\n0x4304: *(rax + 0x8) = r"
        )
        fx.extractor._signatures[FUNC_A] = "void sub_4300(int64_t* rax)"
        fx.extractor._hlils[FUNC_B] = (
            "0x4310: r = *(rax + 0x0)\n0x4314: *(rax + 0x18) = 0x110"
        )
        fx.extractor._signatures[FUNC_B] = "void sub_4310(int64_t* rax)"

        candidate = StructCandidate(
            candidate_id="struct_cand_7",
            base_type_hint="int128_t*",
            accesses=[
                FieldAccess(offset=0x0, size=8, access_type="read",
                            function_address=FUNC_A, instruction_address="0x4300"),
                FieldAccess(offset=0x8, size=8, access_type="write",
                            function_address=FUNC_A, instruction_address="0x4304"),
                FieldAccess(offset=0x0, size=8, access_type="read",
                            function_address=FUNC_B, instruction_address="0x4310"),
                FieldAccess(offset=0x18, size=8, access_type="write",
                            function_address=FUNC_B, instruction_address="0x4314"),
            ],
            functions_involved=[FUNC_A, FUNC_B],
        )
        fx.extra["candidate"] = candidate
        fx.expected["struct_name"] = "value_node"
        fx.expected["gt_layout"] = [
            {"offset": 0x0, "name": "next", "type_str": "struct value_node*", "size": 8},
            {"offset": 0x8, "name": "prev", "type_str": "struct value_node*", "size": 8},
            {"offset": 0x18, "name": "type_flags", "type_str": "uint64_t", "size": 8},
        ]

    def scripted_responses(self, fx: Fixtures) -> list[str]:
        return [json.dumps({
            "accepted": True,
            "rejection_reason": "",
            "struct_name": "value_node",
            "kind": "struct",
            "fields": [
                {"offset": 0, "name": "next", "type_str": "struct value_node*",
                 "size": 8, "confidence": "high_confidence"},
                {"offset": 8, "name": "prev", "type_str": "struct value_node*",
                 "size": 8, "confidence": "inferred"},
                {"offset": 24, "name": "type_flags", "type_str": "uint64_t",
                 "size": 8, "confidence": "high_confidence"},
            ],
        })]

    async def run_case(self, fx: Fixtures, llm) -> None:
        agent = TypeRecoveryAgent(llm, fx.context_asm, fx.config)
        verdict = await agent.run(fx.extra["candidate"])
        fx.extra["verdict_accepted"] = bool(verdict.accepted)
        struct_def = agent.to_struct_def(verdict)
        fx.extra["struct_def"] = (
            struct_def.model_dump() if struct_def is not None else None)
        if struct_def is not None:
            # The pipeline persists each applied StructDefinition for export
            # (run.py Phase 4) — replicate that exact call.
            await fx.ledger.record_struct(struct_def)
        fx.extra["ledger_structs"] = await fx.ledger.get_structs()
        fx.extra["claims_a"] = await fx.ledger.get_claims(FUNC_A)

    async def assert_expected(self, fx: Fixtures) -> list:
        checks = []
        if not fx.extra.get("verdict_accepted"):
            checks.append(self.fail("type-recovery verdict rejected the merge"))
        struct_def = fx.extra.get("struct_def")
        if not struct_def or not struct_def.get("fields"):
            checks.append(self.fail("TypeRecoveryAgent returned no struct"))
            return checks or [self.ok("no struct")]
        name = struct_def.get("struct_name")
        if name != fx.expected.get("struct_name"):
            checks.append(self.fail(
                f"struct_name {name!r} != expected {fx.expected['struct_name']!r}"))
        # Ledger must expose the struct for metric-5 export.
        if name not in (fx.extra.get("ledger_structs") or {}):
            checks.append(self.fail(f"struct {name} not recorded in ledger"))
        # A claim must be registered for the involved functions.
        claims_a = fx.extra.get("claims_a") or []
        if not any("Type recovery" in (c.get("claim_text") or "") for c in claims_a):
            checks.append(self.fail("no type-recovery claim registered for involved fn"))
        return checks or [self.ok("type recovery inferred + recorded a struct")]

    def score_items(self, fx: Fixtures) -> dict[str, list[dict]]:
        name = fx.expected.get("struct_name")
        gt = fx.expected.get("gt_layout") or []
        recovered = (fx.extra.get("ledger_structs") or {}).get(name) or \
            (fx.extra.get("struct_def") or {}).get("fields") or []
        return {"datatype": [{
            "key": name, "name": name, "recovered_name": name,
            "true": gt, "recovered": recovered,
        }]} if recovered else {}


register(TypeRecoveryCase)
