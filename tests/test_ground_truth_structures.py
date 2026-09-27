"""Headless unit tests for the metric-5 struct ground-truth extractor.

``extract_ground_truth._struct_layouts`` imports binaryninja at call time, so we
can exercise it with a FAKE ``binaryninja`` module injected into sys.modules —
this validates the audit fix (audit 2026-09-27) without a live Binja install:

  * resolves pointers: param/local var types -> underlying structure type
  * records real member offsets + sizes
  * uses the repo's x86-64 pointer-width assumption (pointers => 8)
  * strips the 'struct ' prefix Binja's str() uses
  * excludes libc/blob noise (ALLCAPS aliases, underscore-prefixed, anonymous)
"""

from __future__ import annotations

import enum
import os
import sys
import types
from types import SimpleNamespace

import pytest


class FakeTypeClass(enum.Enum):
    PointerTypeClass = "PointerTypeClass"
    StructureTypeClass = "StructureTypeClass"
    IntegerTypeClass = "IntegerTypeClass"
    FloatTypeClass = "FloatTypeClass"


class FakeMember:
    def __init__(self, name, offset, ftype):
        self.name = name
        self.offset = offset
        self.type = ftype


def _fake_binja():
    bn = types.ModuleType("binaryninja")
    bn.TypeClass = FakeTypeClass
    return bn


class FakeBV:
    def __init__(self, functions):
        self.functions = functions


def _make_struct(name, members):
    t = SimpleNamespace()
    t.type_class = FakeTypeClass.StructureTypeClass
    t.name = name
    t.members = members
    return t


def _make_ptr(target):
    t = SimpleNamespace()
    t.type_class = FakeTypeClass.PointerTypeClass
    t.target = target
    t.name = None
    return t


def _make_int(width):
    t = SimpleNamespace()
    t.type_class = FakeTypeClass.IntegerTypeClass
    t.width = width
    t.name = None
    return t


def _make_float():
    t = SimpleNamespace()
    t.type_class = FakeTypeClass.FloatTypeClass
    t.width = 8
    t.name = None
    return t


def _run_layouts(functions):
    sys.modules["binaryninja"] = _fake_binja()
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    sys.path.insert(0, os.path.join(repo, "eval", "cjson"))

    from extract_ground_truth import _struct_layouts  # noqa: PLC0415

    return _struct_layouts(FakeBV(functions))


class _V:
    def __init__(self, ftype):
        self.type = ftype


class _F:
    def __init__(self, types):
        # types: list of variable objects
        self.parameter_vars = types
        self.vars = []


def test_extracts_cjson_layout_via_pointer_param():
    """cJSON is referenced through a cJSON* param; offsets/sizes must be real."""
    cjson = _make_struct(
        "cJSON",
        [
            FakeMember("next", 0, _make_ptr(_make_struct("cJSON", []))),
            FakeMember("prev", 8, _make_ptr(_make_struct("cJSON", []))),
            FakeMember("child", 16, _make_ptr(_make_struct("cJSON", []))),
            FakeMember("type", 24, _make_int(4)),
            FakeMember("valuestring", 32, _make_ptr(_make_struct("cJSON", []))),
            FakeMember("valueint", 40, _make_int(4)),
            FakeMember("valuedouble", 48, _make_float()),
            FakeMember("string", 56, _make_ptr(_make_struct("cJSON", []))),
        ],
    )
    fn = _F([_V(_make_ptr(cjson))])
    layouts = _run_layouts([fn])

    assert set(layouts) == {"cJSON"}
    fields = {f["name"]: f for f in layouts["cJSON"]}
    assert fields["next"]["offset"] == 0 and fields["next"]["size"] == 8
    assert fields["prev"]["offset"] == 8 and fields["prev"]["size"] == 8
    assert fields["type"]["offset"] == 24 and fields["type"]["size"] == 4
    assert fields["valueint"]["offset"] == 40 and fields["valueint"]["size"] == 4
    assert fields["valuedouble"]["offset"] == 48 and fields["valuedouble"]["size"] == 8
    assert fields["string"]["offset"] == 56 and fields["string"]["size"] == 8


def test_strips_struct_prefix_and_double_pointer():
    """'struct Foo' naming and pointer-in-pointer must both resolve."""
    inner = _make_struct(
        "Foo",
        [FakeMember("a", 0, _make_int(4)), FakeMember("b", 4, _make_int(4))],
    )
    inner.name = "struct Foo"  # Binja str() style qualified name
    fn = _F([_V(_make_ptr(_make_ptr(inner)))])  # Foo**
    layouts = _run_layouts([fn])
    assert set(layouts) == {"Foo"}
    assert len(layouts["Foo"]) == 2


def test_excludes_libc_and_anonymous_noise():
    """ALLCAPS, underscore-prefixed, and anonymous structs must not pollute."""
    file_t = _make_struct(
        "FILE", [FakeMember("f", 0, _make_int(4))]
    )
    io_file = _make_struct(
        "_IO_FILE", [FakeMember("g", 0, _make_int(4))]
    )
    anon = _make_struct("", [FakeMember("h", 0, _make_int(4))])
    fn = _F([_V(file_t), _V(io_file), _V(anon)])
    layouts = _run_layouts([fn])
    assert layouts == {}


def test_pointer_width_fallback_is_8():
    """A member whose type has unknown width but is a pointer => size 8."""
    ptr = SimpleNamespace()
    ptr.type_class = FakeTypeClass.PointerTypeClass
    ptr.width = 0  # Binja sometimes reports unsigned widths for pointers
    inner = _make_struct("Bar", [FakeMember("p", 0, ptr)])
    fn = _F([_V(inner)])
    layouts = _run_layouts([fn])
    assert layouts["Bar"][0]["size"] == 8
