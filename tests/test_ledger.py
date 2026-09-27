"""Tests for Deliverable 1.5 — Ledger (per-function SQLite claim store).

Uses the real Ledger with tmp_path SQLite files and the FakeNeo4jDriver from
conftest. Verifies the claim lifecycle (NULL -> evaluated), idempotent graph
registration, and cross-store coverage checks.
"""

from __future__ import annotations

import pytest

from reaper.harness.ledger import Ledger

from conftest import FakeNeo4jDriver, RecordingNeo4jDriver


@pytest.mark.asyncio
async def test_claim_lifecycle_and_coverage(tmp_path):
    driver = FakeNeo4jDriver(["0x1400", "0x2000"])
    ledger = Ledger(str(tmp_path / "ledger.db"), driver)
    await ledger.init()
    try:
        await ledger.register_functions_from_graph()

        # no claims yet -> not covered
        assert await ledger.all_functions_have_claims() is False

        for i in range(3):
            await ledger.add_claim(
                function_address="0x1400",
                claim_text=f"claim {i}",
                submitted_by="agent_a",
                evidence=[{"address_start": "0x1400", "address_end": "0x1410",
                           "description": "d"}],
            )
        # claims exist with truth_level NULL -> still not covered
        claims = await ledger.get_claims("0x1400")
        assert len(claims) == 3
        assert all(c["truth_level"] is None for c in claims)
        assert await ledger.all_functions_have_claims() is False

        # evaluate all three claims
        for c in claims:
            await ledger.set_truth_level(c["id"], "mid_confidence", "critic_1")

        claims = await ledger.get_claims("0x1400")
        assert all(c["truth_level"] == "mid_confidence" for c in claims)
        # 0x2000 still has no claims -> not covered overall
        assert await ledger.all_functions_have_claims() is False

        # cover the remaining function
        await ledger.add_claim(
            function_address="0x2000",
            claim_text="ready",
            submitted_by="agent_b",
            evidence=[],
        )
        c2 = await ledger.get_claims("0x2000")
        await ledger.set_truth_level(c2[0]["id"], "high_confidence", "critic_1")
        assert await ledger.all_functions_have_claims() is True
    finally:
        await ledger.close()


@pytest.mark.asyncio
async def test_function_not_in_ledger_blocks_coverage(tmp_path):
    # Neo4j has a function that was never registered/claimed in SQLite
    driver = FakeNeo4jDriver(["0x1400", "0x9999"])
    ledger = Ledger(str(tmp_path / "ledger.db"), driver)
    await ledger.init()
    try:
        await ledger.register_functions_from_graph()
        await ledger.add_claim("0x1400", "c", "agent_a", [])
        await ledger.set_truth_level(
            (await ledger.get_claims("0x1400"))[0]["id"], "inferred", "critic"
        )
        assert await ledger.all_functions_have_claims() is False
        # explicitly register+claim the straggler
        await ledger.register_functions_from_graph()
        await ledger.add_claim("0x9999", "c2", "agent_a", [])
        await ledger.set_truth_level(
            (await ledger.get_claims("0x9999"))[0]["id"], "low_confidence", "critic"
        )
        assert await ledger.all_functions_have_claims() is True
    finally:
        await ledger.close()


@pytest.mark.asyncio
async def test_invalid_truth_level_rejected(tmp_path):
    ledger = Ledger(str(tmp_path / "ledger.db"), FakeNeo4jDriver([]))
    await ledger.init()
    try:
        cid = await ledger.add_claim("0x1", "c", "a", [])
        with pytest.raises(ValueError):
            await ledger.set_truth_level(cid, "banana", "critic")
    finally:
        await ledger.close()


@pytest.mark.asyncio
async def test_update_and_delete_claim(tmp_path):
    ledger = Ledger(str(tmp_path / "ledger.db"), FakeNeo4jDriver([]))
    await ledger.init()
    try:
        cid = await ledger.add_claim("0x2", "old text", "a", [])
        await ledger.update_claim_text(cid, "new text")
        claims = await ledger.get_claims("0x2")
        assert claims[0]["claim_text"] == "new text"
        await ledger.delete_claim(cid)
        assert await ledger.get_claims("0x2") == []
    finally:
        await ledger.close()


@pytest.mark.asyncio
async def test_function_names_and_summary(tmp_path):
    ledger = Ledger(str(tmp_path / "ledger.db"), FakeNeo4jDriver(["0x1400"]))
    await ledger.init()
    try:
        await ledger.register_functions_from_graph()
        fn = await ledger.get_function("0x1400")
        assert fn is not None and fn["llm_name"] is None
        await ledger.set_function_names("0x1400", "parse_json_config", "cJSON Parse")
        await ledger.set_function_summary("0x1400", "Parses config JSON blobs.")
        fn = await ledger.get_function("0x1400")
        assert fn["llm_name"] == "parse_json_config"
        assert fn["canon_name"] == "cJSON Parse"
        assert fn["summary"] == "Parses config JSON blobs."
        assert await ledger.get_function("0xdead") is None
    finally:
        await ledger.close()


@pytest.mark.asyncio
async def test_not_initialized_raises(tmp_path):
    ledger = Ledger(str(tmp_path / "db.db"), FakeNeo4jDriver([]))
    with pytest.raises(RuntimeError):
        await ledger.get_function("0x1")


@pytest.mark.asyncio
async def test_sweep_null_claims_deletes_unevaluated(tmp_path):
    ledger = Ledger(str(tmp_path / "ledger.db"), FakeNeo4jDriver(["0x1400"]))
    await ledger.init()
    try:
        await ledger.add_claim("0x1400", "unevaluated 1", "a",
                               [{"address_start": "0x1400", "address_end": "0x1410",
                                 "description": "d"}])
        await ledger.add_claim("0x1400", "unevaluated 2", "a", [])
        cid = await ledger.add_claim("0x1400", "evaluated", "a", [])
        await ledger.set_truth_level(cid, "mid_confidence", "critic")

        swept = await ledger.sweep_null_claims()
        assert swept == 2
        claims = await ledger.get_claims("0x1400")
        assert len(claims) == 1
        assert claims[0]["truth_level"] == "mid_confidence"
        assert await ledger.sweep_null_claims() == 0  # idempotent
    finally:
        await ledger.close()


@pytest.mark.asyncio
async def test_claim_coverage_stats_breakdown(tmp_path):
    driver = FakeNeo4jDriver(["0x1400", "0x2000", "0x3000"])
    ledger = Ledger(str(tmp_path / "ledger.db"), driver)
    await ledger.init()
    try:
        c1 = await ledger.add_claim("0x1400", "solid", "a", [])
        await ledger.set_truth_level(c1, "high_confidence", "critic")
        c2 = await ledger.add_claim("0x2000", "guess", "a", [])
        await ledger.set_truth_level(c2, "speculation", "critic")

        stats = await ledger.claim_coverage_stats()
        assert stats["total_graph_functions"] == 3
        assert stats["renamable_functions"] == 3
        assert stats["pinned_functions"] == 0
        assert stats["with_claims"] == 2
        assert stats["with_mid_confidence_or_above"] == 1
        assert stats["speculation_only"] == 1
        assert stats["no_claims"] == 1
    finally:
        await ledger.close()


@pytest.mark.asyncio
async def test_all_functions_have_claims_can_exclude_pinned(tmp_path):
    def respond(query, params):
        if "RETURN f.address AS address" in query:
            if "pinned" in query:      # the non-pinned scope query
                return [{"address": "0x2000"}]
            return [{"address": "0x1000"}, {"address": "0x2000"}]
        return []

    driver = RecordingNeo4jDriver(respond)
    ledger = Ledger(str(tmp_path / "ledger.db"), driver)
    await ledger.init()
    try:
        await ledger.register_functions_from_graph()  # registers both
        await ledger.add_claim("0x2000", "c", "a", [])
        await ledger.set_truth_level(
            (await ledger.get_claims("0x2000"))[0]["id"], "inferred", "critic"
        )
        # renamable scope is covered (pinned 0x1000 excluded)
        assert await ledger.all_functions_have_claims(include_pinned=False) is True
        # the default all-functions scope still sees the pinned gap
        assert await ledger.all_functions_have_claims() is False
    finally:
        await ledger.close()


@pytest.mark.asyncio
async def test_record_and_get_structs(tmp_path):
    from reaper.harness.submission import StructDefinition, StructField
    ledger = Ledger(str(tmp_path / "ledger.db"), FakeNeo4jDriver([]))
    await ledger.init()
    try:
        sd = StructDefinition(struct_name="cJSON", fields=[
            StructField(offset=0, name="next", type_str="struct cJSON*", size=8,
                        confidence="high_confidence"),
            StructField(offset=8, name="prev", type_str="struct cJSON*", size=8,
                        confidence="high_confidence"),
        ])
        await ledger.record_struct(sd)
        structs = await ledger.get_structs()
        assert set(structs) == {"cJSON"}
        assert len(structs["cJSON"]) == 2
        assert structs["cJSON"][0]["offset"] == 0
        assert structs["cJSON"][0]["size"] == 8
        assert structs["cJSON"][0]["confidence"] == "high_confidence"

        # Re-recording the same struct name REPLACES its fields (idempotent).
        sd2 = StructDefinition(struct_name="cJSON", fields=[
            StructField(offset=0, name="only", type_str="int", size=4,
                        confidence="inferred"),
        ])
        await ledger.record_struct(sd2)
        assert len((await ledger.get_structs())["cJSON"]) == 1
    finally:
        await ledger.close()
