"""Pass-0 corpus ground truth — Binja-independent (DWARF via pyelftools).

Mirror of ``eval/cjson/extract_ground_truth.py`` (Deliverable 8.1) that works
WITHOUT Binary Ninja: it reads the UNSTRIPPED ``pass0_sample_symbols`` DWARF
debug info + symbol table and cross-checks every function address against the
STRIPPED ``pass0_sample`` so only code that SURVIVES stripping is kept — the
same contract the cjson extractor enforces through Binja (its only reason to
need Binja at all).

Output is byte-for-byte compatible with what ``eval/evaluate.py`` consumes:

    {
      "0x<addr>": {"function_name", "parameters": [{"name", "type"}],
                   "local_variables": [{"name", "type"}]},
      "structs": {"<name>": [{"offset", "size", "name", "type_str"}]}
    }

Metric-5 structs faithfully mirror the Binja extractor's filters: only
structures reachable from the recovered functions' parameter/local variable
types (deref pointers, resolve typedefs/qualifiers/arrays), skipping
libc-noise names (empty, underscore-prefixed, or ALLCAPS aliases). Layouts use
the ground-truth DWARF byte sizes (verified against the actual compiled
offsets); pointer widths fall back to 8 (x86-64), matching StructAccessDetector
assumptions.

Usage (from eval/pass0/):
    python3 extract_ground_truth.py     # requires: ./build.sh first
Requires pyelftools (``import elftools``). If unavailable it writes an EMPTY
ground_truth.json and notes the requirement — the pipeline still runs; the
evaluator just reports metric 5 as 0.
"""

from __future__ import annotations

import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SYM_BINARY = os.path.join(HERE, "pass0_sample_symbols")
STRIPPED_BINARY = os.path.join(HERE, "pass0_sample")
OUT_PATH = os.path.join(HERE, "ground_truth.json")

_X_64_PTR_SIZE = 8          # mirror of StructAccessDetector._type_size
_DEPTH_LIMIT = 8


def _import_elftools():
    """Import pyelftools; raises ImportError if unavailable."""
    from elftools.elf.elffile import ELFFile  # noqa: F401
    return ELFFile


def _type_ref(cu, die):
    """Resolve a die's DW_AT_type to a DIE (or None) within the same CU.

    ``DW_FORM_ref_addr`` is section-absolute; all other reference forms are
    CU-relative and need the CU's base offset added. Newer pyelftools returns
    the DIE directly, older versions return a 2-tuple — normalize both.
    """
    attr = die.attributes.get("DW_AT_type")
    if attr is None:
        return None
    try:
        base = int(getattr(cu, "cu_addr", None)
                   or getattr(cu, "cu_offset", 0) or 0)
        form = str(attr.form)
        addr = int(attr.value)
        if form != "DW_FORM_ref_addr":     # CU-relative reference form
            addr = base + addr
        resolved = cu.get_DIE_from_refaddr(addr)
        if isinstance(resolved, tuple):
            resolved = resolved[0]
        return resolved
    except Exception:
        return None


def _walk(die):
    """Depth-first yield of a DIE and all its descendants (top-down order)."""
    yield die
    for child in die.iter_children():
        yield from _walk(child)


def _member_offset(attr):
    """Extract a constant member offset from DW_AT_data_member_location.

    GCC emits a plain integer for constant offsets; guard against the
    exprloc form (DW_OP_plus_uconst / DW_OP_const*) defensively.
    """
    if attr is None:
        return 0
    try:
        form = str(attr.form)
        if "DATA" in form or "UDATA" in form or "SDATA" in form:
            return int(attr.value)
    except Exception:
        pass
    data = getattr(attr, "value", None)
    if isinstance(data, bytes) and data:
        try:
            if data[0] == 0x23:        # DW_OP_plus_uconst
                return int.from_bytes(data[2:3], "little") or 0
            if data[0] == 0x10:        # DW_OP_constu (ULEB)
                n = 1
                while n < len(data) and data[n] & 0x80:
                    n += 1
                return int.from_bytes(data[1:n + 1], "little", signed=False)
        except Exception:
            return 0
    value = getattr(attr, "value", 0)
    if isinstance(value, bytes):
        return 0
    return int(value or 0)


def _base_size(cu, die, depth=0):
    """Ground-truth width of a member type DIE (bytes), else None.

    Pointers fall back to 8 (x86-64), mirroring StructAccessDetector._type_size
    so inferred sizes are compared apples-to-apples on the only supported
    target. Arrays resolve their element size x declared count; typedefs and
    qualifiers are chased to the underlying type.
    """
    if die is None or depth > _DEPTH_LIMIT:
        return None
    if "DW_AT_byte_size" in die.attributes:
        try:
            return int(die.attributes["DW_AT_byte_size"].value)
        except Exception:
            return None
    tag = die.tag
    if tag == "DW_TAG_pointer_type":
        return _X_64_PTR_SIZE
    if tag in ("DW_TAG_typedef", "DW_TAG_const_type", "DW_TAG_volatile_type",
               "DW_TAG_restrict_type"):
        return _base_size(cu, _type_ref(cu, die), depth + 1)
    if tag == "DW_TAG_array_type":
        el = _base_size(cu, _type_ref(cu, die), depth + 1) or 0
        count = 0
        for child in die.iter_children():
            if child.tag != "DW_TAG_subrange_type":
                continue
            if "DW_AT_count" in child.attributes:
                try:
                    count = int(child.attributes["DW_AT_count"].value)
                    break
                except Exception:
                    pass
            if "DW_AT_upper_bound" in child.attributes:
                try:
                    count = int(child.attributes["DW_AT_upper_bound"].value) + 1
                    break
                except Exception:
                    pass
        return (el * count) if el and count else None
    return None


def _type_str(cu, die, depth=0) -> str:
    """A readable type string (informational; not scored by metric 5)."""
    if die is None or depth > _DEPTH_LIMIT:
        return "<unknown>"
    tag = die.tag
    name = die.attributes.get("DW_AT_name", None)
    label = name.value.decode() if name is not None else ""
    if tag in ("DW_TAG_typedef", "DW_TAG_const_type", "DW_TAG_volatile_type",
               "DW_TAG_restrict_type"):
        return _type_str(cu, _type_ref(cu, die), depth + 1)
    if tag == "DW_TAG_pointer_type":
        return (_type_str(cu, _type_ref(cu, die), depth + 1) or "void") + " *"
    if tag == "DW_TAG_structure_type":
        return "struct " + (label or "?")
    if tag == "DW_TAG_union_type":
        return "union " + (label or "?")
    if tag == "DW_TAG_enumeration_type":
        return "enum " + (label or "?")
    if tag == "DW_TAG_subroutine_type":
        return "function"
    if tag == "DW_TAG_base_type":
        return label or "?"
    if tag == "DW_TAG_array_type":
        return _type_str(cu, _type_ref(cu, die), depth + 1) + "[]"
    return label or tag


def _record_structs_from(cu, die, reachable: set) -> None:
    """Collect structure names reachable from a param/local type DIE.

    Faithful mirror of the Binja extractor's pointer-deref walk: bytes for
    pointer->struct, typedef->target, array->element chains add every
    structure encountered to ``reachable`` (subject to the name filter).
    """
    seen = set()

    def _walk_types(t, depth=0):
        if t is None or depth > _DEPTH_LIMIT or id(t) in seen:
            return
        seen.add(id(t))
        tag = t.tag
        if tag == "DW_TAG_pointer_type":
            _walk_types(_type_ref(cu, t), depth + 1)
        elif tag in ("DW_TAG_typedef", "DW_TAG_const_type",
                     "DW_TAG_volatile_type", "DW_TAG_restrict_type"):
            _walk_types(_type_ref(cu, t), depth + 1)
        elif tag == "DW_TAG_array_type":
            _walk_types(_type_ref(cu, t), depth + 1)
        elif tag == "DW_TAG_structure_type":
            name = t.attributes.get("DW_AT_name", None)
            name = name.value.decode() if name is not None else ""
            if name and not name.startswith("_") and not name.isupper():
                reachable.add(name)

    _walk_types(die)


def _executable(stripped_elf, addr: int) -> bool:
    """True if ``addr`` lies in an executable PT_LOAD segment of stripped bin.

    Reproduces the 8.1 'survived stripping' decision (the cjson extractor uses
    Binja's get_function_at) without Binja: ``strip`` only removes symbol
    tables, so any address that is code in the symbols binary and still inside
    an executable mapping of the stripped file is considered survivable.
    """
    for seg in stripped_elf.iter_segments():
        if seg["p_type"] != "PT_LOAD":
            continue
        if not (seg["p_flags"] & 1):      # PF_X
            continue
        vaddr = seg["p_vaddr"]
        if vaddr <= addr < vaddr + seg["p_filesz"]:
            return True
    return False


def extract() -> dict:
    ELFFile = _import_elftools()

    with open(SYM_BINARY, "rb") as fh:
        sym_elf = ELFFile(fh)
        with open(STRIPPED_BINARY, "rb") as fh2:
            stripped_elf = ELFFile(fh2)

            # 1. Functions that SURVIVE stripping (symtab -> code map).
            funcs: dict[int, str] = {}
            symtab = sym_elf.get_section_by_name(".symtab")
            for sym in symtab.iter_symbols():
                if sym["st_info"]["type"] != "STT_FUNC":
                    continue
                if sym["st_shndx"] == "SHN_UNDEF":
                    continue
                name = sym.name or ""
                if not name or name.startswith("_"):
                    continue          # mirror 8.1: skip sub_* / runtime frames
                addr = sym["st_value"]
                if _executable(stripped_elf, addr):
                    funcs[addr] = name

            # 2. DWARF: per-function params/locals + reachable struct names.
            reachable: set = set()
            dwarf = sym_elf.get_dwarf_info()
            subprograms: dict[int, list] = {}   # addr -> [(cu, die)]
            struct_dies: dict[str, list] = {}   # name -> [(cu, die)]
            for cu in dwarf.iter_CUs():
                for die in _walk(cu.get_top_DIE()):
                    if die.tag == "DW_TAG_subprogram":
                        lp = die.attributes.get("DW_AT_low_pc", None)
                        if lp is not None:
                            subprograms.setdefault(int(lp.value), []).append((cu, die))
                    elif die.tag in ("DW_TAG_structure_type", "DW_TAG_union_type"):
                        nm = die.attributes.get("DW_AT_name", None)
                        if nm is not None:
                            struct_dies.setdefault(nm.value.decode(), []).append((cu, die))

            ground_truth: dict = {}
            for addr, func_name in sorted(funcs.items()):
                params, locals_ = [], []
                for cu, die in subprograms.get(addr, []):
                    for child in die.iter_children():
                        if child.tag == "DW_TAG_formal_parameter":
                            cname = child.attributes.get("DW_AT_name", None)
                            params.append({
                                "name": cname.value.decode() if cname is not None else "",
                                "type": _type_str(cu, _type_ref(cu, child)),
                            })
                            _record_structs_from(cu, _type_ref(cu, child), reachable)
                        elif child.tag == "DW_TAG_variable":
                            cname = child.attributes.get("DW_AT_name", None)
                            locals_.append({
                                "name": cname.value.decode() if cname is not None else "",
                                "type": _type_str(cu, _type_ref(cu, child)),
                            })
                            _record_structs_from(cu, _type_ref(cu, child), reachable)
                ground_truth[hex(addr)] = {
                    "function_name": func_name,
                    "parameters": params,
                    "local_variables": locals_,
                }

            # 3. Struct layouts for metric 5 (reachable, non-libc names only).
            def _structure_layout(cu, die) -> list[dict]:
                rows = []
                for m in die.iter_children():
                    if m.tag != "DW_TAG_member":
                        continue
                    mname = m.attributes.get("DW_AT_name", None)
                    mtarget = _type_ref(cu, m)
                    rows.append({
                        "offset": _member_offset(m.attributes.get(
                            "DW_AT_data_member_location", None)),
                        "size": _base_size(cu, mtarget) or 0,
                        "name": mname.value.decode() if mname is not None else "",
                        "type_str": _type_str(cu, mtarget),
                    })
                return rows

            layouts: dict[str, list[dict]] = {}
            for sname in sorted(reachable):
                rows = None
                for cu, die in struct_dies.get(sname, []):
                    attempt = _structure_layout(cu, die)
                    if attempt and rows is None:
                        rows = attempt
                        break
                if rows:
                    layouts[sname] = rows
            ground_truth["structs"] = layouts
            return ground_truth


def main() -> int:
    if not os.path.exists(SYM_BINARY) or not os.path.exists(STRIPPED_BINARY):
        print("missing binaries — run ./build.sh first (in eval/pass0/)",
              file=sys.stderr)
        return 1
    try:
        ground_truth = extract()
    except ImportError:
        print("pyelftools not importable — writing EMPTY ground truth "
              "(pip install pyelftools, or set PYTHONPATH=$HOME/binja-headless/"
              "python to extract via Binja instead).", file=sys.stderr)
        ground_truth = {}
    with open(OUT_PATH, "w", encoding="utf-8") as fh:
        json.dump(ground_truth, fh, indent=2)
    n_funcs = sum(1 for v in ground_truth.values()
                  if isinstance(v, dict) and "function_name" in v)
    n_structs = len(ground_truth.get("structs") or {})
    print(f"Extracted {n_funcs} functions + {n_structs} structs -> {OUT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
