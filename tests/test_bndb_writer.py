"""Tests for Deliverable 3.4 — BNDBWriter.

Exercises rename_function / rename_variable (including SECOND renames via the
_rename_map contract), set_type, save() (require_binja behavior), and the async
close lifecycle hook. Binja is NOT installed, so all apply paths run against
the fake extractor (Binja-absent flows must not crash); save() must raise the
clear require_binja error, and our simulated-Binja test confirms the real save
path issues ``bv.save(bndb_path)``.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import reaper.tools.bndb_writer as bw_module
from reaper.tools.bndb_writer import BNDBWriter
from tests import fake_binja as fb
from tests.fake_harness import TagRecordingBinaryView, make_extractor_with_tags


def _func_with_var(name="var_18", type_str="int64_t"):
    var = fb.FakeVariable(name, type_str=type_str)
    func = fb.FakeFunction.simple(0x1400, "sub_1400", variables=[var], return_type="int64_t")
    return func, var


def _writer(*functions):
    return BNDBWriter(make_extractor_with_tags(functions=[*functions]))


def test_rename_function_sets_name_and_attempts_llm_tag():
    func, _ = _func_with_var()
    writer = _writer(func)
    writer.rename_function(0x1400, "parse_json_data", "ParseJsonData")
    assert func.name == "ParseJsonData"
    # tag type creation was attempted (best-effort; FakeFunction has no tag API
    # so the actual address-tag call is skipped without crashing).
    assert ("create_tag_type", "reaper_llm", "🏷") in writer.bv.tags


def test_rename_function_accepts_hex_string_address():
    func, _ = _func_with_var()
    writer = _writer(func)
    writer.rename_function("0x1400", "parse_json_data", "ParseJsonData")
    assert func.name == "ParseJsonData"


def test_rename_function_unknown_address_raises():
    writer = BNDBWriter(make_extractor_with_tags())
    with pytest.raises(KeyError):
        writer.rename_function(0x9999, "a", "b")


def test_rename_variable_first_rename_via_node_id_suffix():
    func, var = _func_with_var()
    writer = _writer(func)
    writer.rename_variable(0x1400, "0x1400:var_18", "input_data", "InputData")
    assert var.name == "InputData"
    assert writer._rename_map == {"0x1400:var_18": "InputData"}


def test_second_rename_of_same_variable_uses_rename_map():
    """The node_id suffix no longer matches after the first rename; the
    _rename_map MUST be consulted so the second rename still finds the var."""
    func, var = _func_with_var()
    writer = _writer(func)
    writer.rename_variable(0x1400, "0x1400:var_18", "input_data", "InputData")
    writer.rename_variable(0x1400, "0x1400:var_18", "json_input", "JsonInput")
    assert var.name == "JsonInput"
    assert writer._rename_map == {"0x1400:var_18": "JsonInput"}
    # a THIRD rename (stable node id NEVER changes) must still work
    writer.rename_variable(0x1400, "0x1400:var_18", "user_buffer", "UserBuffer")
    assert var.name == "UserBuffer"


def test_rename_variable_unknown_var_raises():
    func, _ = _func_with_var()
    writer = _writer(func)
    with pytest.raises(KeyError):
        writer.rename_variable(0x1400, "0x1400:var_99", "a", "b")


def test_rename_variable_records_llm_tag_when_func_supports_tags():
    func, var = _func_with_var()
    recorded = []
    func.create_user_address_tag = lambda addr, tag_type, name: recorded.append(
        (addr, tag_type, name)
    )
    writer = _writer(func)
    writer.rename_variable(0x1400, "0x1400:var_18", "input_data", "InputData")
    assert var.name == "InputData"
    assert recorded and recorded[0][1] == "tagtype:reaper_llm"
    assert recorded[0][2] == "input_data"


def test_set_type_applies_parsed_type_when_view_supports_parsing():
    func, _ = _func_with_var()
    ex = make_extractor_with_tags(functions=[func])  # TagRecording view parses
    writer = BNDBWriter(ex)
    ret = writer.set_type(0x1400, "void (int32_t)")
    assert ret is func
    assert str(func.return_type) == "void (int32_t)"


def test_set_type_without_parse_api_degrades_without_crashing():
    """Binja-absent flow: no parse_type_string on a plain fake view must not
    crash and must still store the type string on the duck-typed object."""
    func, _ = _func_with_var()
    ex = fb.FakeExtractor(fb.FakeBinaryView(functions=[func]))
    writer = BNDBWriter(ex)
    writer.set_type(0x1400, "int32_t ()")
    assert func.return_type == "int32_t ()"


def test_set_type_unknown_address_raises():
    writer = BNDBWriter(make_extractor_with_tags())
    with pytest.raises(KeyError):
        writer.set_type(0x1234, "int")


def test_save_without_binja_raises_clear_error():
    """save() needs a live BinaryView -> clear require_binja RuntimeError when
    Binja is absent (it is not installed here)."""
    writer = BNDBWriter(make_extractor_with_tags())
    with pytest.raises(RuntimeError, match="requires Binary Ninja"):
        writer.save()


def test_save_issues_bv_save_when_binja_available(monkeypatch):
    saved = []

    class _View:
        def save(self, path):
            saved.append(str(path))

    ex = SimpleNamespace(bv=_View(), bndb_path="/tmp/fake/target.bndb")
    writer = BNDBWriter(ex)
    monkeypatch.setattr(bw_module, "require_binja", lambda feature=None: None)
    result = writer.save()
    assert result == "/tmp/fake/target.bndb"
    assert saved == ["/tmp/fake/target.bndb"]


def test_save_falls_back_to_data_dir_when_no_bndb_path(monkeypatch):
    saved = []

    class _View:
        def save(self, path):
            saved.append(str(path))

    ex = SimpleNamespace(bv=_View(), data_dir="/tmp/fake-out")
    writer = BNDBWriter(ex)
    monkeypatch.setattr(bw_module, "require_binja", lambda feature=None: None)
    writer.save()
    assert saved == ["/tmp/fake-out/target.bndb"]


@pytest.mark.asyncio
async def test_close_is_async_lifecycle_hook():
    writer = BNDBWriter(make_extractor_with_tags())
    assert await writer.close() is None
