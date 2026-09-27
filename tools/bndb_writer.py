"""BNDB writeback — Deliverable 3.4.

Writes renames and types back to the Binja BNDB file through the live
BinaryView. Reuses the HLILExtractor's BinaryView (never reopens the binary).

Node-ID discipline (design § 3.4): Neo4j node ids are STABLE (based on original
variable names). Renames change ``canon_name``/``llm_name`` on the Neo4j node
but never the node's ``id``. Binja, however, only has ONE name per variable:
after the first rename the original name from the node_id suffix no longer
matches Binja's state. Maintains ``self._rename_map`` (node_id -> the name
Binja currently holds) so second and subsequent renames of the same variable
still find it.

Binja availability: the ``apply`` methods are duck-typed and work against the
fake extractor (tests) even when Binary Ninja is absent; tags are best-effort
metadata and never fatal. ``save()`` genuinely needs a live BinaryView to write
the ``.bndb``, so it calls ``require_binja()`` and raises a clear error instead
of crashing with an AttributeError deep inside the call.
"""

from __future__ import annotations

import logging
from pathlib import Path

from reaper.tools._compat import require_binja

log = logging.getLogger(__name__)

_TAG_TYPE_NAME = "reaper_llm"
_TAG_TYPE_ICON = "🏷"


class BNDBWriter:
    """Applies renames/types to the live BinaryView and saves the BNDB."""

    def __init__(self, extractor):
        self.extractor = extractor
        self.bv = extractor.bv
        # node_id -> the name Binja CURRENTLY has for that variable.
        # Updated on every successful variable rename.
        self._rename_map: dict[str, str] = {}
        self._tag_type = None

    def rename_function(self, address: int, llm_name: str, canon_name: str):
        """Set the function's name and store the LLM name as a user tag."""
        address = self._to_int(address)
        func = self.bv.get_function_at(address)
        if func is None:
            raise KeyError(f"no function at 0x{address:x} in BinaryView")
        func.name = canon_name
        self._store_llm_tag(func, address, llm_name)
        return func

    def rename_variable(
        self,
        func_address: int,
        var_node_id: str,
        llm_name: str,
        canon_name: str,
    ):
        """Rename a variable identified by its STABLE node_id.

        Lookup chain (design § 3.4):
          1. ``_rename_map[var_node_id]`` — the name Binja currently holds
             (required for second+ renames; differs from the node_id suffix).
          2. otherwise parse the original name from the node_id suffix.
        Set ``var.name = canon_name``, record the map, store llm_name as a tag.
        """
        func_address = self._to_int(func_address)
        func = self.bv.get_function_at(func_address)
        if func is None:
            raise KeyError(f"no function at 0x{func_address:x} in BinaryView")

        current_name = self._resolve_current_var_name(var_node_id)
        var = self._find_var(func, current_name)
        if var is None:
            raise KeyError(
                f"variable {current_name!r} (node {var_node_id!r}) not found in "
                f"function 0x{func_address:x}"
            )

        var.name = canon_name
        self._rename_map[var_node_id] = canon_name
        self._store_llm_tag(func, func_address, llm_name)
        return var

    def set_type(self, address: int, type_str: str):
        """Parse ``type_str`` and apply it to the function at ``address``.

        ``bv.parse_type_string`` is a real-Binja API; when Binja is absent we
        degrade to storing the raw string on the duck-typed function object so
        the flow never crashes (Binja-absent flows must not crash).
        """
        address = self._to_int(address)
        func = self.bv.get_function_at(address)
        if func is None:
            raise KeyError(f"no function at 0x{address:x} in BinaryView")

        try:
            parsed = self.bv.parse_type_string(type_str)
            new_type = parsed[0] if isinstance(parsed, tuple) else parsed
            if hasattr(func, "set_user_type"):
                func.set_user_type(new_type)
            else:
                func.return_type = new_type
        except AttributeError:
            # Binja absent — keep the type string on the duck-typed object.
            log.debug("set_type(%s): Binja absent — storing string fallback", hex(address))
            func.return_type = type_str
        return func

    def save(self) -> str:
        """Persist the BinaryView to the BNDB file (bv.save). Requires Binja."""
        require_binja(
            "BNDBWriter.save() — writing the .bndb needs a live BinaryView. "
            "The rename/type apply methods work without Binja (fake extractor), "
            "but persisting to disk does not."
        )
        bndb_path = self._bndb_path()
        self.bv.save(bndb_path)
        log.info("BNDBWriter saved %s", bndb_path)
        return bndb_path

    async def close(self) -> None:
        """Async lifecycle hook (symmetry with other harness components).

        Holds no async resources; safe to call at any time.
        """
        return None

    def _resolve_current_var_name(self, var_node_id: str) -> str:
        """Lookup chain 1 → 2 per design § 3.4."""
        if var_node_id in self._rename_map:
            return self._rename_map[var_node_id]
        # Fall back to the original name embedded in the stable node id suffix.
        return str(var_node_id).split(":")[-1]

    def _find_var(self, func, name: str):
        for var in getattr(func, "vars", None) or []:
            if getattr(var, "name", None) == name:
                return var
        return None

    def _store_llm_tag(self, func, address: int, llm_name: str) -> None:
        """Best-effort: LLM name as a user tag on the address.

        Fake views have no tag API — silently no-op (tags are metadata only).
        """
        try:
            tag_type = self._ensure_tag_type()
            func.create_user_address_tag(address, tag_type, llm_name)
        except AttributeError:
            log.debug("no tag API on %s — skipping llm tag (Binja absent?)", type(func).__name__)
        except Exception:
            log.exception("failed to store llm tag on 0x%x", address)

    def _ensure_tag_type(self):
        if self._tag_type is None:
            self._tag_type = self.bv.create_tag_type(_TAG_TYPE_NAME, _TAG_TYPE_ICON)
        return self._tag_type

    def _bndb_path(self) -> str:
        """Resolve the .bndb path from the extractor (defensively)."""
        direct = getattr(self.extractor, "bndb_path", None)
        if direct:
            return str(direct)
        data_dir = getattr(self.extractor, "data_dir", None)
        if data_dir:
            return str(Path(data_dir) / "target.bndb")
        return "target.bndb"

    @staticmethod
    def _to_int(address):
        if isinstance(address, int):
            return address
        if isinstance(address, str):
            return int(address, 16)
        return int(address)


__all__ = ["BNDBWriter"]

