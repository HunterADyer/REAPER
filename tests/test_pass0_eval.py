"""Offline validation of the Pass-0 type-recovery eval corpus (eval/pass0).

No Binja, No LLM, no network: these tests build the small sample binary if
the stripped/unstripped pair is missing, extract ground truth from DWARF via
pyelftools, cross-check the recovered layouts against a FRESH compiled
offsetof oracle, assert stripping survival + no libc noise, and exercise the
metric-5 scorer (identity -> 100%, displaced -> 0%, partial -> partial) with
the real ground truth. See eval/pass0/README.md.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

pytest.importorskip("elftools.elf.elffile")  # ground truth needs pyelftools

from eval.evaluate import compute_metrics, to_report  # noqa: E402
from eval.pass0 import extract_ground_truth as eg     # noqa: E402

PASS0 = Path(__file__).resolve().parent.parent / "eval" / "pass0"

AUTHORED_FUNCS = {
    "main", "on_overflow",
    "cfg_version", "cfg_dump", "cfg_emit",
    "box_area", "box_hit", "pt_dist2",
    "node_kind_name", "node_rank",
    "symtab_load", "symtab_generation",
}

# C reference for offsetof/sizeof — MUST mirror sample.c struct definitions.
_REF_C = r"""
#include <stddef.h>
#include <stdio.h>
#include <stdint.h>
struct cfg { int32_t version; uint16_t mode; uint8_t verbosity; uint8_t pad;
             char logfile[64]; void *user; int (*on_event)(struct cfg*,int);
             double threshold; };
struct point { double x; double y; };
struct box { struct point min; struct point max; uint32_t color; int32_t layer; };
struct gnode { struct gnode *next; uint32_t id; enum {A,B,C,D} kind;
               const char *label; int weight; };
struct symtab { struct gnode **slots; uint32_t slot_count; uint32_t used;
                uint64_t generation; };
#define P(t_,f_) printf("%s.%s %zu %zu\n", #t_, #f_, \
                        offsetof(struct t_, f_), sizeof(((struct t_*)0)->f_))
int main(void) {
    P(cfg,version);P(cfg,mode);P(cfg,verbosity);P(cfg,pad);P(cfg,logfile);
    P(cfg,user);P(cfg,on_event);P(cfg,threshold);
    P(point,x);P(point,y);
    P(box,min);P(box,max);P(box,color);P(box,layer);
    P(gnode,next);P(gnode,id);P(gnode,kind);P(gnode,label);P(gnode,weight);
    P(symtab,slots);P(symtab,slot_count);P(symtab,used);P(symtab,generation);
    return 0;
}
"""

@pytest.fixture(scope="session")
def pass0_dir():
    d = PASS0
    if not d.exists():
        pytest.fail(f"eval/pass0 missing: {d}")
    binaries = (d / "pass0_sample", d / "pass0_sample_symbols")
    if not all(p.exists() for p in binaries):
        if not (shutil.which("gcc") or shutil.which("cc")):
            pytest.skip("no C compiler to build the pass0 corpus")
        subprocess.run(["bash", str(d / "build.sh")], cwd=d, check=True)
    return d


@pytest.fixture(scope="session")
def ground_truth(pass0_dir):
    return eg.extract()


def _compiler_reference() -> dict:
    """Fresh offsetof/sizeof oracle from the real compiler (no hardcoding)."""
    cc = shutil.which("gcc") or shutil.which("cc")
    if not cc:
        pytest.skip("no C compiler for the offsetof oracle")
    ref_bin = "/tmp/pass0_offsetof_ref"
    subprocess.run([cc, "-x", "c", "-", "-o", ref_bin], input=_REF_C,
                   text=True, capture_output=True, check=True)
    res = subprocess.run([ref_bin], capture_output=True, text=True, check=True)
    ref: dict[str, dict[str, tuple[int, int]]] = {}
    for line in res.stdout.splitlines():
        name, off, size = line.split()
        s, f = name.split(".", 1)          # name = "<struct>.<member>"
        ref.setdefault(s, {})[f] = (int(off), int(size))
    return ref


def test_extracted_layouts_match_compiler_reference(ground_truth):
    """Ground-truth offsets/sizes must equal what the compiler really emitted."""
    ref = _compiler_reference()
    extracted = {s: {m["name"]: (m["offset"], m["size"])
                     for m in ms}
                 for s, ms in ground_truth["structs"].items()}
    assert set(extracted) == set(ref)
    for sname, members in ref.items():
        assert extracted[sname] == members, f"layout mismatch for {sname}"


def test_ground_truth_contains_only_corpus_structs(ground_truth):
    """Metric-5 sees ONLY our structs — libc/FILE/tm noise must be absent."""
    assert set(ground_truth["structs"]) == {"cfg", "point", "box", "gnode",
                                            "symtab"}
    for ms in ground_truth["structs"].values():
        assert all(m["size"] > 0 for m in ms)


def test_ground_truth_functions_survive_stripping(pass0_dir, ground_truth):
    from elftools.elf.elffile import ELFFile

    funcs = {k: v for k, v in ground_truth.items()
             if isinstance(v, dict) and "function_name" in v}
    names = {v["function_name"] for v in funcs.values()}
    assert AUTHORED_FUNCS <= names
    with open(pass0_dir / "pass0_sample", "rb") as fh:
        stripped = ELFFile(fh)
        for addr in funcs:
            assert eg._executable(stripped, int(addr, 16)), addr


def test_stripped_input_is_minimal(pass0_dir):
    """The REAPER input has no symtab and no DWARF — symbols are gone."""
    from elftools.elf.elffile import ELFFile

    with open(pass0_dir / "pass0_sample", "rb") as fh:
        elf = ELFFile(fh)
        # strict=True ignores .eh_frame, which strip deliberately keeps.
        assert not elf.has_dwarf_info(strict=True)
        symtab = elf.get_section_by_name(".symtab")
        assert symtab is None or not list(symtab.iter_symbols())


def test_dwarf_parameters_and_locals_recovered(pass0_dir, ground_truth):
    funcs = {v["function_name"]: v for v in ground_truth.values()
             if isinstance(v, dict) and "function_name" in v}
    cfg_dump = funcs["cfg_dump"]
    assert [p["name"] for p in cfg_dump["parameters"]] == ["c"]
    local_names = {v["name"] for v in cfg_dump["local_variables"]}
    assert {"i", "n"} <= local_names


def test_committed_ground_truth_is_current(pass0_dir, ground_truth):
    committed = json.loads((pass0_dir / "ground_truth.json").read_text())
    assert committed == ground_truth


@pytest.mark.asyncio
async def test_metric5_identity_scores_100(ground_truth):
    rp_structs = {s: [{"offset": m["offset"], "size": m["size"]} for m in ms]
                  for s, ms in ground_truth["structs"].items()}
    m = await compute_metrics(ground_truth, {"structs": rp_structs}, judge=None)
    assert m.matched_struct_fields == m.total_struct_fields > 0
    assert to_report(m)["type_recovery_accuracy"] == 100.0


@pytest.mark.asyncio
async def test_metric5_displaced_layouts_score_zero(ground_truth):
    # +7 displacement provably avoids colliding with any real (offset, size)
    # pair in this corpus, so the scorer must report 0 (exact off+size match).
    rp_structs = {s: [{"offset": m["offset"] + 7, "size": m["size"]}
                      for m in ms]
                  for s, ms in ground_truth["structs"].items()}
    m = await compute_metrics(ground_truth, {"structs": rp_structs}, judge=None)
    assert m.matched_struct_fields == 0
    assert m.total_struct_fields > 0


@pytest.mark.asyncio
async def test_metric5_partial_recovery_scores_partially(ground_truth):
    cfg_fields = ground_truth["structs"]["cfg"]
    rp_structs = {"cfg": [{"offset": m["offset"], "size": m["size"]}
                          for m in cfg_fields]}
    m = await compute_metrics(ground_truth, {"structs": rp_structs}, judge=None)
    assert m.matched_struct_fields == len(cfg_fields)
    assert 0 < m.matched_struct_fields < m.total_struct_fields
