"""REAPER output exporter — Deliverable 8.4.

Serializes the final RE stage state (Neo4j master graph + function ledger +
BNDB-backed extractor) into the JSON shape ``eval/evaluate.py --reaper-output``
consumes:

    {
      "0x<addr>": {
        "canon_name": ...,
        "llm_name": ...,
        "parameters": [{"index", "name", "llm_name", "canon_name", "type"}],
        "variables":  [{"name", "llm_name", "canon_name", "type", "source"}],
        "claims":     [{"claim_text", "truth_level", "submitted_by",
                        "reviewed_by", "evidence": [{"address_start",
                        "address_end", "description"}]}],
        "fields":     []
      },
      "structs": { "<struct_name>": [{"offset", "size", "name",
                                      "type_str", "confidence"}] }
    }

Design notes:
  * The canonical address set / variable ordering come from the *extractor's
    own enumeration* (the same 2.1 code path the 8.1 ground-truth extractor
    uses), so positional variable matching stays consistent between sides.
  * Parameter/variable NAMES are taken from Neo4j (the merge master): the
    latest ``llm_name``/``canon_name`` set by renames. Ordinals recorded at
    graph-build time (2.2) keep ordering deterministic even after the live
    Binja variables were renamed (node ids are immutable by design).
  * Function names prefer Neo4j master ``canon_name``, then ``llm_name``,
    then the ledger row, then the raw Binja name.
  * Struct definitions (8.3 metric 5) are exported from the ledger, where
    run.py records each StructDefinition applied by type recovery.
"""

from __future__ import annotations

import json
import logging

log = logging.getLogger(__name__)


def _hex(address) -> str:
    if isinstance(address, str):
        text = address.strip()
        address = int(text, 16) if text.lower().startswith("0x") else int(text)
    return f"0x{int(address):x}"


def _pick_name(*candidates) -> str:
    """First non-empty candidate, or ''."""
    for c in candidates:
        if c:
            return str(c)
    return ""


async def _function_parts(neo4j_driver) -> dict:
    """{address -> {llm_name, canon_name, name, pinned}} from Neo4j."""
    out: dict = {}
    try:
        async with neo4j_driver.session() as session:
            res = await session.run(
                "MATCH (f:Function) "
                "RETURN f.address AS address, f.llm_name AS llm_name, "
                "f.canon_name AS canon_name, f.name AS name, f.pinned AS pinned"
            )
            for rec in [r async for r in res]:
                address = rec.get("address")
                if address is None:
                    continue
                out[str(address)] = {
                    "llm_name": rec.get("llm_name"),
                    "canon_name": rec.get("canon_name"),
                    "name": rec.get("name"),
                    "pinned": bool(rec.get("pinned")),
                }
    except Exception:
        log.exception("export_report: function-name query failed")
    return out


async def _owned_rows(neo4j_driver, func_addr: str, label: str) -> list[dict]:
    """Ordered Variable/Argument rows owned by a function, by ordinal."""
    try:
        async with neo4j_driver.session() as session:
            res = await session.run(
                f"MATCH (f:Function {{address: $a}})-[:CONTAINS]->(v:{label}) "
                "RETURN v.id AS id, v.ordinal AS ordinal, v.llm_name AS llm_name, "
                "v.canon_name AS canon_name, v.type AS type, v.source AS source "
                "ORDER BY v.ordinal",
                {"a": func_addr},
            )
            rows = [
                {
                    "id": r.get("id"),
                    "ordinal": r.get("ordinal"),
                    "llm_name": r.get("llm_name"),
                    "canon_name": r.get("canon_name"),
                    "type": r.get("type"),
                    "source": r.get("source"),
                }
                for r in [row async for row in res]
            ]
            return rows
    except Exception:
        log.exception("export_report: %s query failed for %s", label, func_addr)
        return []

async def _claim_dicts(ledger, func_addr: str) -> list[dict]:
    """Ledger claims for a function, each with its evidence links."""
    out: list[dict] = []
    try:
        for claim in await ledger.get_claims(func_addr):
            evidence = []
            try:
                evidence = [
                    {
                        "address_start": e["address_start"],
                        "address_end": e["address_end"],
                        "description": e.get("description"),
                    }
                    for e in await ledger.get_evidence(claim["id"])
                ]
            except Exception:
                log.debug("export_report: evidence failed for claim %s", claim["id"])
            out.append({
                "claim_text": claim.get("claim_text"),
                "truth_level": claim.get("truth_level"),
                "submitted_by": claim.get("submitted_by"),
                "reviewed_by": claim.get("reviewed_by"),
                "evidence": evidence,
            })
    except Exception:
        log.exception("export_report: claims query failed for %s", func_addr)
    return out


async def extract_reaper_output(neo4j_driver, ledger, extractor) -> dict:
    """Build the evaluator-consumable dict from the pipeline's final state."""
    functions = await _function_parts(neo4j_driver)
    out: dict = {}
    try:
        metas = extractor.list_functions()
    except Exception:
        log.exception("export_report: extractor.list_functions failed")
        metas = []
    for meta in metas:
        address = _hex(meta.get("address"))
        fn = functions.get(address) or {}
        try:
            ledger_fn = await ledger.get_function(address) or {}
        except Exception:
            ledger_fn = {}
        out[address] = {
            "canon_name": _pick_name(
                fn.get("canon_name"), fn.get("llm_name"),
                ledger_fn.get("canon_name"), ledger_fn.get("llm_name"),
                fn.get("name"), meta.get("name"),
            ),
            "llm_name": _pick_name(
                fn.get("llm_name"), ledger_fn.get("llm_name"),
                fn.get("name"), meta.get("name"),
            ),
            "pinned": bool(fn.get("pinned", False) or ledger_fn.get("pinned", False)),
            "parameters": [],
            "variables": [],
            "claims": await _claim_dicts(ledger, address),
            "fields": [],
        }

        arg_rows = await _owned_rows(neo4j_driver, address, "Argument")
        if arg_rows:
            for r in arg_rows:
                out[address]["parameters"].append({
                    "index": r.get("ordinal"),
                    "name": r.get("name") if r.get("name") else None,
                    "llm_name": r.get("llm_name"),
                    "canon_name": r.get("canon_name"),
                    "type": r.get("type"),
                })
        else:
            # No graph rows (e.g. graph phase skipped) — fall back to Binja.
            try:
                for p in extractor.get_parameters(meta.get("address")):
                    out[address]["parameters"].append({
                        "index": p.get("index"),
                        "name": p.get("name"),
                        "llm_name": None,
                        "canon_name": None,
                        "type": p.get("type"),
                    })
            except Exception:
                log.debug("export_report: get_parameters failed for %s", address)

        var_rows = await _owned_rows(neo4j_driver, address, "Variable")
        if var_rows:
            for r in var_rows:
                out[address]["variables"].append({
                    "name": r.get("name") if r.get("name") else None,
                    "llm_name": r.get("llm_name"),
                    "canon_name": r.get("canon_name"),
                    "type": r.get("type"),
                    "source": r.get("source"),
                })
        else:
            try:
                for v in extractor.get_variables(meta.get("address")):
                    out[address]["variables"].append({
                        "name": v.get("name"),
                        "llm_name": None,
                        "canon_name": None,
                        "type": v.get("type"),
                        "source": v.get("source"),
                    })
            except Exception:
                log.debug("export_report: get_variables failed for %s", address)

    try:
        structs = await ledger.get_structs()
    except Exception:
        log.exception("export_report: ledger.get_structs failed")
        structs = {}
    out["structs"] = structs
    return out


def write_reaper_output(data: dict, path: str) -> None:
    """Write the exporter payload as pretty JSON; creates parent dirs."""
    from pathlib import Path

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with open(target, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, sort_keys=True)


__all__ = ["extract_reaper_output", "write_reaper_output"]

