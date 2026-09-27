"""Tests for Deliverable 4.1 — StructAccessDetector.

Builds fake HLIL (tests/fake_binja.py) containing the three walk patterns from
design § 4.1 (deref-of-add with constant offset, struct-field access, plus a
write access) and verifies: within-function grouping by base variable,
cross-function grouping ONLY on a meaningful Binja type, conservative handling
of unknown-typed bases (separate candidates, no false-positive merging), the
read/write access_type distinction, and instruction-address reporting.
"""

from __future__ import annotations

from reaper.tools.struct_detector import StructAccessDetector

from fake_binja import (
    FakeExtractor,
    FakeFunction,
    FakeVariable,
    add,
    assign,
    const,
    deref,
    struct_field,
    var,
)


def _cjson_functions():
    """Two functions whose bases share type 'struct cJSON*' + one unknown one."""
    func_a = FakeFunction(
        0x1000, "parse_json",
        instructions=[
            assign(var("item"), deref(add(var("node"), const(0)), address=0x1010),
                   address=0x1010),
            assign(var("tmp"), struct_field(var("node"), member="type", offset=8,
                                            address=0x1020), address=0x1020),
            assign(deref(add(var("item"), const(16)), address=0x1030),
                   var("x"), address=0x1030),
            assign(var("tail"), deref(add(var("node"), const(24)), address=0x1024),
                   address=0x1024),
        ],
        variables=[FakeVariable("node", type_str="struct cJSON*"),
                   FakeVariable("item", type_str="struct cJSON*"),
                   FakeVariable("tmp", type_str="int64_t"),
                   FakeVariable("tail", type_str="struct cJSON*"),
                   FakeVariable("x", type_str="char")],
    )
    func_b = FakeFunction(
        0x2000, "print_json",
        instructions=[
            assign(var("n2"), deref(add(var("node"), const(8)), address=0x2010),
                   address=0x2010),
        ],
        variables=[FakeVariable("node", type_str="struct cJSON*")],
    )
    func_c = FakeFunction(
        0x3000, "opaque_helper",
        instructions=[
            assign(var("v"), deref(add(var("unk"), const(4)), address=0x3010),
                   address=0x3010),
        ],
        variables=[FakeVariable("unk", type_str="$unknown")],
    )
    return FakeExtractor.with_functions([func_a, func_b, func_c])


def test_struct_candidate_detected_with_multiple_offsets():
    detector = StructAccessDetector(_cjson_functions())
    candidates = detector.find_struct_accesses()
    assert candidates, "expected at least one candidate"

    typed = [c for c in candidates if c.base_type_hint == "struct cJSON*"]
    assert typed, "expected a typed candidate for cJSON pointer bases"
    candidate = typed[0]
    offsets = sorted({a.offset for a in candidate.accesses})
    assert offsets == [0, 8, 16, 24], offsets

    # Cross-function grouping: both 0x1000 and 0x2000 share the base type.
    fns = {int(a, 16) for a in candidate.functions_involved}
    assert {0x1000, 0x2000} <= fns

    # The item->child = ... store must be flagged as a WRITE.
    assert any(a.access_type == "write" for a in candidate.accesses)


def test_unknown_typed_bases_stay_separate_candidates():
    detector = StructAccessDetector(_cjson_functions())
    candidates = detector.find_struct_accesses()
    untyped = [c for c in candidates if c.base_type_hint == ""]
    assert untyped, "expected separate candidate(s) for unknown-typed bases"
    # The unknown base lives only in 0x3000.
    assert all(int(a, 16) == 0x3000 for c in untyped for a in c.functions_involved)


def test_detector_reports_instruction_addresses():
    detector = StructAccessDetector(_cjson_functions())
    candidates = detector.find_struct_accesses()
    all_addrs = [a.instruction_address for c in candidates for a in c.accesses]
    assert "0x1030" in all_addrs
    assert "0x2010" in all_addrs


def test_empty_binary_yields_no_candidates():
    detector = StructAccessDetector(FakeExtractor())
    assert detector.find_struct_accesses() == []
