"""Tests for Deliverable 2.1 — HLILExtractor (HLIL text / metadata extraction).

Binja is NOT installed in the test environment, so these tests exercise the
REAL ``HLILExtractor`` against a monkeypatched fake ``binaryninja`` module
(injected into ``reaper.tools._compat``) backed by tests/fake_binja.py. This
proves the extractor's Binja-touching code paths (open_view, BNDB save,
function/HLIL/signature/string-ref/variable/parameter reads) work against the
documented Binja 6.0 API surface.
"""

from __future__ import annotations

import types

import pytest

from reaper.tools.hlil_extract import HLILExtractor

from fake_binja import (
    FakeBinaryView,
    FakeExtractor,
    FakeFunction,
    FakeStringRef,
    FakeVariable,
    assign,
    call,
    const,
    const_data,
    const_ptr,
    hlil_ret,
    var,
    var_init,
)


def _make_bv():
    """A FakeBinaryView with one malloc-like function + an "error" string."""
    err_ref = const_ptr(0x5000, address=0x1040)
    instructions = [
        var_init("var_18", call(const_ptr(0x4000), (const(0x100),), address=0x1011,
                               target_func="malloc", target_addr=0x4000),
                 address=0x1010, type_str="char*"),
        assign(var("var_18", address=0x1020), const(0, address=0x1021), address=0x1020),
        assign(var("var_20", address=0x1030), err_ref, address=0x1030),
        hlil_ret(var("var_18", address=0x1050), address=0x1050),
    ]
    func = FakeFunction.simple(
        0x1000, "sub_1000", instructions=instructions,
        variables=[FakeVariable("var_18", type_str="char*"),
                   FakeVariable("var_20", type_str="char*")],
        parameters=[FakeVariable("arg1", type_str="int64_t")],
        return_type="char*",
    )
    bv = FakeBinaryView(functions=[func], strings=[FakeStringRef(0x5000, "error: out of memory\n")])
    return bv, func


def _patch_binja(monkeypatch, tmp_path, bv):
    """Wire a fake `binaryninja` module into _compat and build an extractor."""
    recorded_bndb = []

    def fake_create_database(path):
        recorded_bndb.append(path)
        # Binja's create_database really persists the file; our fake does too.
        with open(path, "w") as fh:
            fh.write("fake-bndb")

    monkeypatch.setattr(bv, "create_database", fake_create_database, raising=False)
    fake_bn = types.SimpleNamespace(open_view=lambda path: bv)
    monkeypatch.setattr("reaper.tools._compat.binaryninja", fake_bn)
    monkeypatch.setattr("reaper.tools._compat.BINJA_AVAILABLE", True)
    extractor = HLILExtractor("stripped.bin", str(tmp_path))
    return extractor, recorded_bndb


def test_requires_binja_when_absent(monkeypatch, tmp_path):
    monkeypatch.setattr("reaper.tools._compat.BINJA_AVAILABLE", False)
    monkeypatch.setattr("reaper.tools._compat.binaryninja", None)
    with pytest.raises(RuntimeError, match="Binary Ninja headless"):
        HLILExtractor("stripped.bin", str(tmp_path))


def test_opens_view_and_saves_bndb_on_first_open(monkeypatch, tmp_path):
    bv, _ = _make_bv()
    extractor, recorded = _patch_binja(monkeypatch, tmp_path, bv)
    assert extractor.bv is bv
    assert extractor.bndb_path == str(tmp_path / "target.bndb")
    assert recorded == [str(tmp_path / "target.bndb")]

    # Second construction sees an existing BNDB -> no re-save.
    _extractor2, recorded2 = _patch_binja(monkeypatch, tmp_path, bv)
    assert recorded2 == []


def test_get_function_hlil_returns_address_prefixed_text(monkeypatch, tmp_path):
    bv, _ = _make_bv()
    extractor, _ = _patch_binja(monkeypatch, tmp_path, bv)
    text = extractor.get_function_hlil("0x1000")
    assert text
    for line in text.splitlines():
        assert line.startswith("0x")
    assert "0x1010:" in text
    assert "HLIL_VAR_INIT" in text


def test_get_hlil_range_returns_subset(monkeypatch, tmp_path):
    bv, _ = _make_bv()
    extractor, _ = _patch_binja(monkeypatch, tmp_path, bv)
    subset = extractor.get_hlil_range("0x1030", "0x1031")
    assert "0x1030:" in subset
    assert "0x1010:" not in subset


def test_get_hlil_range_spans_two_functions(monkeypatch, tmp_path):
    _, func_a = _make_bv()
    func_b = FakeFunction.simple(
        0x1040, "sub_1040",
        instructions=[assign(var("var_30", address=0x1060),
                             const(1, address=0x1061), address=0x1060)],
    )
    bv = FakeBinaryView(functions=[func_a, func_b])
    # Fake get_functions_containing is start<=addr; force it to return both.
    bv.get_functions_containing = lambda addr: [func_a, func_b]
    extractor, _ = _patch_binja(monkeypatch, tmp_path, bv)
    text = extractor.get_hlil_range("0x1010", "0x1070")
    assert text.count("// ") == 2  # both function sections present
    assert "sub_1000" in text and "sub_1040" in text


def test_get_function_signature(monkeypatch, tmp_path):
    bv, _ = _make_bv()
    extractor, _ = _patch_binja(monkeypatch, tmp_path, bv)
    assert extractor.get_function_signature("0x1000") == "char* sub_1000(int64_t arg1)"


def test_get_string_refs_finds_const_ptr_not_const_data(monkeypatch, tmp_path):
    bv, _ = _make_bv()
    # Add an inline const-data instruction that must be IGNORED.
    func = bv.functions[0]
    func.hlil.append(const_data(b"INLINE", address=0x1070))
    extractor, _ = _patch_binja(monkeypatch, tmp_path, bv)
    refs = extractor.get_string_refs("0x1000")
    assert refs == [{"address": "0x5000", "value": "error: out of memory\n"}]


def test_get_variables_and_parameters(monkeypatch, tmp_path):
    bv, _ = _make_bv()
    extractor, _ = _patch_binja(monkeypatch, tmp_path, bv)
    vars_ = extractor.get_variables("0x1000")
    names = {v["name"] for v in vars_}
    assert {"var_18", "var_20"} <= names
    assert all(v["type"] == "char*" for v in vars_)
    params = extractor.get_parameters("0x1000")
    assert params == [{"name": "arg1", "type": "int64_t", "index": 0}]


def test_list_functions_metadata(monkeypatch, tmp_path):
    bv, _ = _make_bv()
    extractor, _ = _patch_binja(monkeypatch, tmp_path, bv)
    listing = extractor.list_functions()
    assert len(listing) == 1
    assert listing[0]["address"] == "0x1000"
    assert listing[0]["name"] == "sub_1000"
    assert listing[0]["size"] >= 0
    assert listing[0]["imported"] is False


def test_interface_parity_with_extractor_contract():
    """The REAL extractor exposes the full 2.1 serialized surface; the graph
    builders additionally need the object-level accessors, which both the real
    extractor and the FakeExtractor test double implement."""
    serialized = ("list_functions", "get_function_hlil", "get_hlil_range",
                  "get_function_signature", "get_string_refs",
                  "get_variables", "get_parameters")
    builder_used = ("get_functions", "get_function", "list_functions",
                    "get_variables", "get_parameters", "get_string_refs")
    for method in (*serialized, *builder_used):
        assert hasattr(HLILExtractor, method), method
    for method in builder_used:
        assert hasattr(FakeExtractor, method), method
