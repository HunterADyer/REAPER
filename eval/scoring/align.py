"""Evidence alignment — turn ground-truth + recovered output into score items.

Pure functions that reduce the two sides (ground truth, recovered output)
into flat, deterministic score items per dimension:

    function-item : {"key": <addr>, "true": <original fn name>, "recovered": <recovered fn name>}
    variable-item : {"key": "<addr>:<ordinal>", "true": ..., "recovered": ...}
    datatype-item : {"key": <struct name>, "name": <true name>, "recovered_name": <recovered name>,
                     "true": <layout list>, "recovered": <layout list>}

Alignment rules (unchanged from the existing evaluator's philosophy):
  - functions and variables are aligned BY ADDRESS (+ ordinal for variables):
    identifiers are gone on stripped binaries; position is the only stable key.
  - datatypes are aligned by struct NAME (type recovery names structs itself).
  - everything is sorted deterministically before return; keys are stable so
    score-caches remain valid across identical inputs.

These functions are intentionally I/O-free so the scheduler and heavy tests
can call them against in-memory fixtures as easily as against JSON files.
"""

from __future__ import annotations

from typing import Any


def _hex(address) -> str:
    if isinstance(address, str):
        return address if address.lower().startswith("0x") else f"0x{int(address):o}"
    return f"0x{int(address):x}"


def _norm_addr(address) -> str:
    if isinstance(address, str):
        text = address.strip()
        if text.lower().startswith("0x"):
            return text.lower()
        return hex(int(text))
    return hex(int(address))


def function_items(ground_truth: dict, recovered: dict) -> list[dict]:
    """[(addr) -> item] aligned by address, deterministic order."""
    out = []
    for addr, gt_fn in ground_truth.items():
        if not isinstance(gt_fn, dict) or not gt_fn.get("function_name"):
            continue
        true_name = str(gt_fn["function_name"]).strip()
        if not true_name:
            continue
        rp = (recovered or {}).get(addr) or {}
        recovered_name = str(
            rp.get("canon_name") or rp.get("llm_name") or ""
        ).strip()
        out.append({"key": _norm_addr(addr), "true": true_name,
                    "recovered": recovered_name})
    out.sort(key=lambda it: int(it["key"], 16))
    return out


def _recovered_vars(rp_fn: dict) -> list[dict]:
    """Ordered recovered variable records: parameters first, then variables."""
    out = []
    for key in ("parameters", "variables"):
        out.extend((rp_fn or {}).get(key) or [])
    return out


def _gt_vars(gt_fn: dict) -> list[dict]:
    """Ordered ground-truth variable records: parameters then local variables."""
    out = []
    for key in ("parameters", "local_variables"):
        out.extend((gt_fn or {}).get(key) or [])
    return out


def variable_items(ground_truth: dict, recovered: dict) -> list[dict]:
    """[(addr, ordinal) -> item] aligned by address + position."""
    out = []
    for addr, gt_fn in ground_truth.items():
        if not isinstance(gt_fn, dict) or not gt_fn.get("function_name"):
            continue
        rp = (recovered or {}).get(addr) or {}
        gt_vars = _gt_vars(gt_fn)
        rp_vars = _recovered_vars(rp)
        for i, rv in enumerate(rp_vars):
            if i >= len(gt_vars):
                break
            recovered_name = str(
                rv.get("canon_name") or rv.get("llm_name") or ""
            ).strip()
            true_name = str(gt_vars[i].get("name") or "").strip()
            if not true_name and not recovered_name:
                continue
            out.append({
                "key": f"{_norm_addr(addr)}:{i}",
                "addr": _norm_addr(addr),
                "ordinal": i,
                "true": true_name,
                "recovered": recovered_name,
            })
    out.sort(key=lambda it: it["key"])
    return out


def _layout(fields) -> list[dict]:
    """Normalize a struct layout to [{offset, name, type_str}]."""
    out = []
    for f in (fields or []):
        if not isinstance(f, dict):
            continue
        out.append({
            "offset": int(f.get("offset", 0) or 0),
            "name": f.get("name") or "",
            "type_str": f.get("type_str") or f.get("type") or "",
        })
    return out


def datatype_items(ground_truth: dict, recovered: dict) -> list[dict]:
    """[(struct name) -> item] aligned by struct name.

    Only structs present in the GROUND TRUTH are scored (recovered-only
    structs are spurious; folded into the coverage notes, not the score).
    """
    gt_structs = (ground_truth or {}).get("structs") or {}
    rp_structs = (recovered or {}).get("structs") or {}
    out = []
    for name, gt_fields in gt_structs.items():
        rp_fields = (rp_structs or {}).get(name) or []
        out.append({
            "key": name,
            "name": name,
            "recovered_name": name if rp_fields else "(missing)",
            "true": _layout(gt_fields),
            "recovered": _layout(rp_fields),
        })
    out.sort(key=lambda it: it["key"])
    return out


def all_dimensions(ground_truth: dict, recovered: dict) -> dict[str, list[dict]]:
    """Return {dimension_id: [items]} for every scoring dimension."""
    return {
        "function-name": function_items(ground_truth, recovered),
        "variable-name": variable_items(ground_truth, recovered),
        "datatype": datatype_items(ground_truth, recovered),
    }


__all__ = [
    "function_items", "variable_items", "datatype_items", "all_dimensions",
]
