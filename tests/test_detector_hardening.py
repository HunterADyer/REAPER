"""Tests for the adversarial detector hardening (real-Binja hand-tuning 2026-09-28).

Covers the fixes derived from running against real stripped binaries:
  - foundation bases (fsbase/gsbase/__return_addr = stack canary + saved
    return address) never become candidates,
  - real access widths are read from expr.size (union detection) instead of
    hard-coded 8, and fake instructions must be able to carry a size,
  - pure-indirection bases (only ever offset-0 with one uniform size, no
    call-context sharing) are dropped, while sharing participants are kept,
  - register SSA temporaries that legitimately carry struct field accesses at
    -O2 are NOT name-filtered away (they may access nonzero offsets).
"""

from __future__ import annotations

import pytest

from reaper.tools.struct_detector import StructAccessDetector

from fake_binja import (
    FakeExtractor,
    FakeFunction,
    FakeVariable,
    add,
    assign,
    call,
    const,
    deref,
    var,
)


def _mk(name, type_str):
    """FakeVariable shorthand."""
    return FakeVariable(name, type_str=type_str)


def test_foundation_bases_never_become_candidates():
    """fsbase + 0x28 (TLS canary) and __return_addr (+0) are noise — ignored."""
    func = FakeFunction(
        0x1000, "guarded",
        instructions=[
            assign(var("c"), deref(add(var("fsbase"), const(0x28)),
                                   address=0x1001, size=8), address=0x1001),
            assign(var("ok"), deref(add(var("ptr"), const(8)),
                                    address=0x1002, size=4), address=0x1002),
            assign(var("r"), deref(add(var("__return_addr"), const(0)),
                                   address=0x1003, size=8), address=0x1003),
        ],
        variables=[_mk("fsbase", "void*"), _mk("ptr", "int32_t*"),
                   _mk("__return_addr", "void* const")],
    )
    detector = StructAccessDetector(FakeExtractor.with_functions([func]))
    candidates = detector.find_struct_accesses()
    # only the real struct access (ptr+8) survives; fsbase/__return_addr dropped
    assert len(candidates) == 1, [c.base_names for c in candidates]
    assert candidates[0].base_names == {"0x1000": ["ptr"]}


def test_real_access_width_from_expr_size_enables_union_detection():
    """Two accesses to the SAME offset with DIFFERENT widths -> overlap_hint."""
    r32 = deref(add(var("buf"), const(0)), address=0x2001, size=4)
    r64 = deref(add(var("buf"), const(0)), address=0x2002, size=8)
    func = FakeFunction(
        0x2000, "unionish",
        instructions=[
            assign(var("a"), r32, address=0x2001),
            assign(var("b"), r64, address=0x2002),
        ],
        variables=[_mk("buf", "void*")],
    )
    detector = StructAccessDetector(FakeExtractor.with_functions([func]))
    candidates = detector.find_struct_accesses()
    assert len(candidates) == 1
    assert candidates[0].overlap_hint is True


def test_pure_indirection_base_dropped_but_sharing_base_kept():
    """A base accessed only at +0 (plain *ptr) is dropped UNLESS it is part of
    a call-context sharing edge (then it is provably the same struct)."""
    dispatcher = FakeFunction(
        0x3000, "dispatch",
        instructions=[
            # reads +0 only on cfg — pure indirection locally
            assign(var("x"), deref(add(var("cfg"), const(0)),
                                   address=0x3001, size=8), address=0x3001),
            call(dest=var("r"), params=[var("cfg")], address=0x3002,
                 target_func={"name": "h", "address": 0x4000}, target_addr=0x4000),
        ],
        variables=[_mk("cfg", "void*")],
    )
    handler = FakeFunction(
        0x4000, "handle",
        instructions=[
            assign(var("y"), deref(add(var("item"), const(8)),
                                   address=0x4001, size=8), address=0x4001),
        ],
        parameters=[_mk("item", "void*")],
    )
    detector = StructAccessDetector(FakeExtractor.with_functions([dispatcher, handler]))
    candidates = detector.find_struct_accesses()
    # dispatcher's cfg is +0-only BUT it feeds the handler — must be kept so
    # the merge can happen; there is exactly one candidate spanning both.
    assert len(candidates) == 1, [c.base_names for c in candidates]
    assert {int(a, 16) for a in candidates[0].functions_involved} == {0x3000, 0x4000}


def test_pure_indirection_without_sharing_dropped():
    """A lone +0-only base with no call context is dropped entirely."""
    func = FakeFunction(
        0x5000, "lonely",
        instructions=[assign(var("x"), deref(var("tmp"), address=0x5001),
                              address=0x5001)],
        variables=[_mk("tmp", "int32_t*")],
    )
    detector = StructAccessDetector(FakeExtractor.with_functions([func]))
    assert detector.find_struct_accesses() == []


def test_register_temp_with_real_offset_not_name_filtered():
    """rcx_1[1] (offset 8 at -O2) must survive — never name-banned."""
    func = FakeFunction(
        0x6000, "o2_style",
        instructions=[assign(var("a"), deref(add(var("rcx_1"), const(8)),
                                              address=0x6001, size=8),
                              address=0x6001)],
        variables=[_mk("rcx_1", "int64_t*")],
    )
    detector = StructAccessDetector(FakeExtractor.with_functions([func]))
    candidates = detector.find_struct_accesses()
    assert len(candidates) == 1
    assert candidates[0].base_names == {"0x6000": ["rcx_1"]}


def test_ssa_temp_normalized_into_family_stem():
    """temp0_2/temp0_11 are the same value — collapse to one base."""
    func = FakeFunction(
        0x7000, "ssa",
        instructions=[
            assign(var("a"), deref(add(var("temp0_2"), const(0)),
                                    address=0x7001, size=8), address=0x7001),
            assign(var("b"), deref(add(var("temp0_11"), const(8)),
                                    address=0x7002, size=8), address=0x7002),
        ],
        variables=[_mk("temp0_2", "void*"), _mk("temp0_11", "void*")],
    )
    detector = StructAccessDetector(FakeExtractor.with_functions([func]))
    candidates = detector.find_struct_accesses()
    assert len(candidates) == 1
    assert candidates[0].base_names == {"0x7000": ["temp0"]}
    assert sorted({a.offset for a in candidates[0].accesses}) == [0, 8]
