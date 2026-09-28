"""Deliverable 2.1 — HLIL text extractor.

Opens a binary in Binary Ninja 6.0 headless and turns HLIL decompilation
into the readable text that agents consume, plus the serialized metadata
the graph builders (2.2-2.4) and context assembler (3.1) rely on.

Binja access is guarded entirely through ``reaper.tools._compat`` so the
module imports cleanly and the class can be unit-tested against the fake
API in tests/fake_binja.py when Binja is absent. When Binja *is* present,
``__init__`` requires a live BinaryView and would fail fast with an
actionable RuntimeError otherwise.

NOTE: the BinaryView is mutable. After type recovery (Phase 4) the HLIL
output for affected functions changes (raw offsets become named fields) —
this is intentional and correct behavior, not a bug.
"""

from __future__ import annotations

import os
from typing import Any

from reaper.harness.events import bus, emit_sync
from reaper.tools import _compat


def _to_int(address: Any) -> int:
    """Normalize str-or-int addresses; str may be '0x...' or decimal."""
    if isinstance(address, int):
        return address
    if isinstance(address, str):
        text = address.strip()
        return int(text, 16) if text.lower().startswith("0x") else int(text)
    return int(address)  # pragma: no cover - Binja always gives int/str


def _hex(address: Any) -> str:
    return f"0x{_to_int(address):x}"


class HLILExtractor:
    """Bridge between a stripped binary opened in Binja headless and graph
    construction / agent context. All reads go through the live BinaryView;
    there is no blocking I/O after construction."""

    def __init__(self, binary_path: str, data_dir: str):
        """Open the binary headless, retain ``self.bv`` and save a BNDB on
        first open (``{data_dir}/target.bndb``)."""
        _compat.require_binja("HLILExtractor")
        self.binary_path = binary_path
        self.data_dir = data_dir
        self.bv = _compat.open_view(binary_path)
        self.bndb_path = os.path.join(data_dir, "target.bndb")
        if not os.path.exists(self.bndb_path):
            self.bv.create_database(self.bndb_path)

    def _emit_access(self, method: str, address: Any = None) -> None:
        """Emit a ``tool.access`` record for Binja reads (debug/RL trace).

        No-op when nothing is subscribed; swallows all errors.
        """
        if not bus.has_subscribers:
            return
        try:
            data: dict = {"method": method}
            if address is not None:
                data["address"] = str(address)
            emit_sync("tool.access", "binja", data)
        except Exception:  # noqa: BLE001
            pass

    # -- object access (used by the graph builders, not in the 2.1 doc) ------

    def get_functions(self) -> list:
        """Return every Function object (real Binja) in the view."""
        return list(self.bv.functions)

    def get_function(self, address=None, name=None):
        """Return the Function at an address, or the first matching name."""
        if address is not None:
            return self.bv.get_function_at(_to_int(address))
        if name is not None:
            for f in self.bv.functions:
                if f.name == name:
                    return f
        return None

    # -- serialized interface (documented in design-re-stage.md § 2.1) ------

    def list_functions(self) -> list[dict]:
        """Return [{address, name, size, imported}, ...] for all functions."""
        self._emit_access("list_functions")
        out = []
        for f in self.bv.functions:
            size = getattr(f, "size", None)
            if size is None:
                blocks = getattr(f, "basic_blocks", None) or []
                if blocks:
                    size = max(bb.end for bb in blocks) - f.start
                else:
                    hlil = getattr(getattr(f, "hlil", None), "instructions", None) or []
                    size = len(hlil)
            out.append({
                "address": _hex(f.start),
                "name": f.name,
                "size": size,
                "imported": bool(getattr(f, "_imported", False)),
            })
        return out

    def get_function_hlil(self, func_address: str) -> str:
        """Return the full HLIL decompilation of one function as text.

        Iterates ``func.hlil.instructions`` (NOT ``func.hlil.root``, which
        does not exist in Binja 6.0). One instruction per line, each prefixed
        with the hex address of the instruction.
        """
        self._emit_access("get_function_hlil", func_address)
        f = self.get_function(func_address)
        if f is None:
            return ""
        lines = [f"{_hex(ins.address)}: {ins}" for ins in f.hlil.instructions]
        return "\n".join(lines)

    def get_hlil_range(self, address_start: str, address_end: str) -> str:
        """Return HLIL text for instructions in [start, end].

        Uses ``bv.get_functions_containing(int(start, 16))``. If the range
        spans two functions both sections are returned, separated by a
        function header. Used by the critic to retrieve evidence.
        """
        self._emit_access("get_hlil_range", address_start)
        start = _to_int(address_start)
        end = _to_int(address_end)
        sections: list[str] = []
        for f in self.bv.get_functions_containing(start):
            lines = []
            for ins in f.hlil.instructions:
                if start <= ins.address <= end:
                    lines.append(f"{_hex(ins.address)}: {ins}")
            if lines:
                sections.append(f"// {f.name} @ {_hex(f.start)}")
                sections.extend(lines)
        return "\n".join(sections)

    def get_function_signature(self, func_address: str) -> str:
        """Return the function prototype as Binja renders it, e.g.
        'int64_t sub_1400(int64_t arg1, char* arg2)'."""
        self._emit_access("get_function_signature", func_address)
        f = self.get_function(func_address)
        if f is None:
            return ""
        params = ", ".join(f"{p.type} {p.name}" for p in f.parameter_vars)
        return f"{f.return_type} {f.name}({params})"

    def get_string_refs(self, func_address: str) -> list[dict]:
        """Return [{address, value}, ...] for all string constant references
        within a function.

        Walks every HLIL instruction and looks for ``HighLevelILConstPtr``
        nodes (pointer-sized constants used for addresses into the data
        section) whose constant matches a known string start. Deliberately
        ignores ``HighLevelILConstData`` (variable-sized inline data).
        """
        self._emit_access("get_string_refs", func_address)
        f = self.get_function(func_address)
        if f is None:
            return []
        known = {s.start: s for s in getattr(self.bv, "strings", [])}
        refs: list[dict] = []
        for ins in f.hlil.instructions:

            def _grab(node):
                if node.operation == _compat.Op.HLIL_CONST_PTR and node.constant in known:
                    s = known[node.constant]
                    length = getattr(s, "length", None)
                    if length and hasattr(self.bv, "read"):
                        value = self.bv.read(s.start, length).decode("utf-8", "replace")
                    else:
                        value = getattr(s, "value", "")
                    refs.append({"address": _hex(node.constant), "value": value})

            _compat.walk_expr(ins, _grab)
        return refs

    def get_data_var_at(self, address) -> dict | None:
        """Return ``{name, address, size}`` for the global data variable at or
        CONTAINING ``address`` (Binja data_vars), or None.

        Global struct-member loads are decompiled as ``*(&data_40XXXX + 0)`` —
        a CONST_PTR whose address lies INSIDE a global object. The type
        detector resolves these to a stable global base by finding the
        containing data variable (its start + size), so a common global struct
        is recovered as ONE type across functions instead of being dropped.
        """
        self._emit_access("get_data_var_at", _hex(address))
        addr = _to_int(address)
        for dv in getattr(self.bv, "data_vars", {}).values():
            start = getattr(dv, "start", 0)
            size = getattr(dv, "size", 0) or 1
            if start <= addr < start + size:
                return {
                    "name": getattr(dv, "name", f"global_{_hex(start)}"),
                    "address": start,
                    "size": size,
                }
        return None

    def get_variables(self, func_address: str) -> list[dict]:
        """Return [{name, type, source, identifier}, ...] for all variables
        in ``func.vars`` (Binja 6.0 property)."""
        self._emit_access("get_variables", func_address)
        f = self.get_function(func_address)
        if f is None:
            return []
        out = []
        for v in f.vars:
            src = getattr(v, "source_type", None)
            out.append({
                "name": v.name,
                "type": str(v.type),
                "source": src.name if src is not None else None,
                "identifier": getattr(v, "identifier", None),
            })
        return out

    def get_parameters(self, func_address: str) -> list[dict]:
        """Return [{name, type, index}, ...] for all parameters in
        ``func.parameter_vars``."""
        self._emit_access("get_parameters", func_address)
        f = self.get_function(func_address)
        if f is None:
            return []
        return [
            {"name": p.name, "type": str(p.type), "index": i}
            for i, p in enumerate(f.parameter_vars)
        ]


__all__ = ["HLILExtractor", "_hex", "_to_int"]
