"""Graph rebuild on type recovery — Deliverable 4.3.

After TypeRecoveryAgent (4.2) infers a struct layout, applies the struct to
Binja, re-extracts field-level accesses, and rewrites the affected part of the
Neo4j graph:

  1. build a C struct string from the StructDefinition,
  2. define the type in Binja via ``bndb_writer.set_struct_type``,
  3. MERGE a :Struct node carrying the struct name + C definition,
  4. re-run the StructAccessDetector against the (now struct-aware) HLIL and
     MERGE explicit per-field :Variable nodes with :FIELD_OF edges,
  5. prune old raw offset-access placeholder variables (ids matching
     ``@0x<offset>``) that were created before the type was known,
  6. re-run ``validate_and_order`` so traversal_order reflects new leaves.

Idempotency: every write is MERGE, so re-running with the same struct is safe.
"""

from __future__ import annotations

import logging

from reaper.harness.submission import StructDefinition
from reaper.tools.graph_analysis import validate_and_order
from reaper.tools.struct_detector import StructAccessDetector

log = logging.getLogger(__name__)

# Placeholder convention for PRE-struct "raw offset access" Variable nodes;
# anything matching ``@0x<hex>`` at the end of the id is obsolete once real
# field nodes exist.
_OFFSET_PLACEHOLDER_RE = ".*@0x[0-9a-f]+$"


def _hex(addr) -> str:
    if isinstance(addr, str):
        text = addr.strip()
        value = int(text, 16) if text.lower().startswith("0x") else int(text)
    else:
        value = int(addr)
    return f"0x{value:x}"


class GraphRebuilder:
    """Rebuild affected graph regions after a struct type is applied (4.3)."""

    def __init__(self, extractor, bndb_writer, neo4j_driver):
        self.extractor = extractor
        self.bndb_writer = bndb_writer
        self.neo4j_driver = neo4j_driver

    @staticmethod
    def _c_struct(struct_def: StructDefinition) -> str:
        kind = getattr(struct_def, "kind", "struct")
        keyword = "union" if kind == "union" else "struct"
        lines = [f"{keyword} {struct_def.struct_name} {{"]
        for fld in sorted(struct_def.fields, key=lambda f: f.offset):
            lines.append(f"    {fld.type_str} {fld.name};")
        lines.append("};")
        return "\n".join(lines)

    async def _bind_base_types(
        self,
        struct_def: StructDefinition,
        base_names: dict[str, list[str]],
    ) -> None:
        """Retag every involved base variable with the recovered pointer type.

        This is the coherence contract: a recovered struct/union must be
        APPLIED CONSISTENTLY THROUGH THE BNDB — every variable that was shown
        (via call-context sharing or Binja's own typing) to hold the data is
        rebound to ``struct <name> *``, so the BNDB type graph references the
        type instead of leaving it dangling and 3 partial structs in its place.
        """
        if self.bndb_writer is None:
            return
        pointer_type = f"{'union' if struct_def.kind == 'union' else 'struct'} {struct_def.struct_name} *"
        for func_addr, names in (base_names or {}).items():
            for base_name in names:
                node_id = f"{func_addr}:{base_name}"
                try:
                    self.bndb_writer.set_variable_type(
                        func_addr, node_id, pointer_type)
                except Exception:
                    # not every base is guaranteed present in Binja; never fatal
                    log.debug("GraphRebuilder: skip type bind for %s", node_id)

    def _all_function_addresses(self) -> list[str]:
        """Fallback when the caller cannot name affected functions."""
        try:
            return [_hex(getattr(f, "start", 0)) for f in self.extractor.get_functions()]
        except Exception:
            log.exception("GraphRebuilder: could not enumerate functions")
            return []

    async def apply_struct(
        self,
        struct_def: StructDefinition,
        function_addresses: list[str] | None = None,
        base_names: dict[str, list[str]] | None = None,
    ) -> list[str]:
        """Apply a struct definition and rebuild the graph (returns affected).

        ``function_addresses`` may be provided by the type-recovery loop (the
        candidate's ``functions_involved``); when omitted, every function
        known to the extractor is treated as potentially affected.
        ``base_names`` maps function_address -> variable names bound to this
        type (from the candidate); those bases are retagged with the recovered
        pointer type in the BNDB so the type is applied consistently through
        the bndb, not merely defined.
        """
        struct_str = self._c_struct(struct_def)
        if self.bndb_writer is not None:
            try:
                self.bndb_writer.set_struct_type(struct_def.struct_name, struct_str)
            except Exception:
                log.exception("GraphRebuilder: bndb set_struct_type failed")
            await self._bind_base_types(struct_def, base_names or {})

        affected = [a for a in (function_addresses or []) if a] or \
            self._all_function_addresses()
        if not affected:
            log.info("GraphRebuilder: no functions affected — nothing to rebuild")
            return []

        field_by_offset = {}
        for fld in struct_def.fields:
            field_by_offset.setdefault(int(fld.offset), fld.name)

        struct_id = f"struct:{struct_def.struct_name}"
        created: set[str] = set()

        # 1. Register the :Struct node (idempotent MERGE).
        async with self.neo4j_driver.session() as session:
            await session.run(
                "MERGE (s:Struct {id: $id}) "
                "SET s.name = $name, s.c_definition = $cdef",
                {"id": struct_id, "name": struct_def.struct_name, "cdef": struct_str},
            )

        # 2. Re-detect field accesses and materialize per-field nodes + edges.
        detector = StructAccessDetector(self.extractor)
        for candidate in detector.find_struct_accesses():
            for acc in candidate.accesses:
                func_addr = acc.function_address
                if func_addr not in affected:
                    continue
                field_name = field_by_offset.get(int(acc.offset))
                if field_name is None:
                    continue
                node_id = f"{func_addr}:{struct_def.struct_name}.{field_name}"
                created.add(node_id)
                await self._merge_field_node(
                    func_addr, node_id, struct_id, struct_def.struct_name,
                    field_name, acc,
                )

        # 3. Prune obsolete raw-offset placeholder variables (keep created ids).
        await self._prune_obsolete(affected, created)

        # 4. Recompute traversal order — new leaves may change the ordering.
        await validate_and_order(self.neo4j_driver)

        # 5. Report affected functions (what the pipeline needs to re-trace).
        return sorted(set(affected), key=lambda a: int(a, 16))

    # ------------------------------------------------------------------ #
    # Internals
    # ------------------------------------------------------------------ #

    async def _merge_field_node(
        self, func_addr, node_id, struct_id, struct_name, field_name, acc
    ) -> None:
        async with self.neo4j_driver.session() as session:
            await session.run("MERGE (f:Function {address: $fa})", {"fa": func_addr})
            await session.run(
                "MERGE (v:Variable {id: $id}) "
                "SET v.field_offset = $off, v.field_name = $field, "
                "v.struct = $struct, v.access_type = $at, "
                "v.llm_name = $llm_name, v.canon_name = $canon_name",
                {
                    "id": node_id, "off": int(acc.offset),
                    "field": field_name, "struct": struct_name,
                    "at": acc.access_type,
                    "llm_name": f"{struct_name}.{field_name}",
                    "canon_name": field_name,
                },
            )
            await session.run(
                "MATCH (f:Function {address: $fa}), (v:Variable {id: $id}) "
                "MERGE (f)-[:CONTAINS]->(v)",
                {"fa": func_addr, "id": node_id},
            )
            await session.run(
                "MATCH (v:Variable {id: $id}), (s:Struct {id: $sid}) "
                "MERGE (v)-[:FIELD_OF]->(s)",
                {"id": node_id, "sid": struct_id},
            )

    async def _prune_obsolete(self, affected: list[str], keep: set[str]) -> None:
        async with self.neo4j_driver.session() as session:
            await session.run(
                "MATCH (f:Function)-[:CONTAINS]->(v:Variable) "
                "WHERE f.address IN $addrs "
                "AND v.id =~ $pattern "
                "AND NOT v.id IN $keep "
                "DETACH DELETE v",
                {
                    "addrs": list(affected),
                    "pattern": _OFFSET_PLACEHOLDER_RE,
                    "keep": sorted(keep),
                },
            )


__all__ = ["GraphRebuilder"]
