"""Structural integrity tests for the adversarial type-recovery corpus.

Runs WITHOUT Binary Ninja (the deep detector/verdict coverage needs Binja and
is driven by `scripts/run_type_test_bins.py --check`). These guard the corpus
itself: every source has built binaries, every binary is covered by the
manifest, the manifest parses and its invariants are well-formed, and the
build script picks up two-digit test numbers.
"""

from __future__ import annotations

import glob
import json
import os
import re

import pytest

from tests.fake_binja import FakeExtractor

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CORPUS = os.path.join(ROOT, "data", "corpora", "type_tests")


def _sources():
    return sorted(glob.glob(os.path.join(CORPUS, "type_test_*.c")))


def _manifest():
    with open(os.path.join(CORPUS, "manifest.json"), encoding="utf-8") as fh:
        return json.load(fh)


def test_corpus_has_25_binaries_each_with_both_opt_levels():
    sources = _sources()
    assert len(sources) >= 25, f"expected >=25 corpus sources, got {len(sources)}"
    for src in sources:
        base = src[:-2]
        assert os.path.exists(base + "_o0"), f"missing -O0 build for {src}"
        assert os.path.exists(base + "_o2"), f"missing -O2 build for {src}"


def test_every_binary_is_covered_by_manifest():
    manifest = _manifest()
    tests = manifest["tests"]
    for src in _sources():
        num = os.path.basename(src).split("_")[2]
        assert num in tests, f"{src} has no manifest entry for {num}"


def test_manifest_is_well_formed_json_with_valid_checks():
    manifest = _manifest()
    assert manifest["version"] == 1
    valid_keys = {"max_candidates", "need_offsets_in_one",
                  "min_functions_in_some", "min_distinct_offsets",
                  "need_overlap"}
    for num, spec in manifest["tests"].items():
        assert "checks" in spec, f"{num}: no checks"
        for opt, checks in spec["checks"].items():
            assert opt in ("both", "o0", "o2"), f"{num}/{opt}: bad opt key"
            for k in checks:
                assert k in valid_keys, f"{num}/{opt}: unknown check {k!r}"
            for off in checks.get("need_offsets_in_one", []):
                assert isinstance(off, int), f"{num}: offset must be int (no 0x literals)"


def test_build_script_glob_supports_two_digit_numbers():
    with open(os.path.join(ROOT, "scripts", "build_type_test_bins.sh"),
              encoding="utf-8") as fh:
        script = fh.read()
    # must NOT be limited to type_test_0* (that excluded 10-25)
    assert re.search(r"type_test_\*\.c", script)


def test_harness_number_filter_in_manifest_range():
    # every manifest key must be addressable by the harness's number filter
    manifest = _manifest()
    for num in manifest["tests"]:
        assert re.fullmatch(r"\d{2}", num), f"manifest key {num!r} not two-digit"
    src_nums = {os.path.basename(s).split("_")[2] for s in _sources()}
    assert src_nums == set(manifest["tests"].keys()), \
        "source numbering and manifest keys must match 1:1"


def test_fake_extractor_exposes_data_var_lookup_for_global_base_tests():
    # the type detector's global-base path calls get_data_var_at; the fake must
    # provide it so the behaviour is testable everywhere (not only with Binja)
    ex = FakeExtractor()
    assert callable(getattr(ex, "get_data_var_at", None))
