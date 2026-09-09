"""Tests for the shipped mesh probe library.

The probes' COMSOL-API behavior is compile-verified by
test_lint_selftest.py::test_all_java_sources_compile (which includes
comsol_support/java/probes/) and exercised for real under the gated
real_comsol suite. These tests pin the library's contract surface:
every shipped probe exposes the query contract, and `query-mph
--query <BareName>` resolves to the shipped source.
"""

from __future__ import annotations


import pytest

from comsol_support.edit_mph import has_query_contract
from comsol_support.query_mph import PROBES_DIR, resolve_query_source

EXPECTED_PROBES = {
    "MeshStatsProbe",       # stats + census, cheap by default (F-D10-11)
    "FeatureProblemProbe",  # per-feature build records survive the throw
    "MeshSelectionProbe",   # feature + CHILD selections, sourceface readback
}


def test_probes_dir_ships_expected_roster():
    shipped = {p.stem for p in PROBES_DIR.glob("*.java")}
    missing = EXPECTED_PROBES - shipped
    assert not missing, f"shipped probe(s) missing: {sorted(missing)}"


def test_every_shipped_probe_exposes_query_contract():
    for p in sorted(PROBES_DIR.glob("*.java")):
        assert has_query_contract(p), (
            f"{p.name} lacks the query(Model, Map) contract — it would "
            "be rejected by edit-mph's pre-JVM gate")


def test_every_shipped_probe_class_name_matches_filename():
    # compile_stage_code derives the class file from the public class
    # name; a mismatch would compile to a surprise location.
    from comsol_support.mphgen import extract_public_class_name
    for p in sorted(PROBES_DIR.glob("*.java")):
        assert extract_public_class_name(p) == p.stem


def test_resolve_query_source_bare_name_and_suffix():
    assert resolve_query_source("MeshStatsProbe").name == "MeshStatsProbe.java"
    assert (resolve_query_source("MeshStatsProbe.java").name
            == "MeshStatsProbe.java")


def test_resolve_query_source_existing_path_wins(tmp_path):
    # A real file shadows the shipped roster — campaign-local probes
    # keep working even if they share a name.
    local = tmp_path / "MeshStatsProbe.java"
    local.write_text("// local override")
    assert resolve_query_source(str(local)) == local


def test_resolve_query_source_unknown_name_lists_roster():
    with pytest.raises(FileNotFoundError) as ei:
        resolve_query_source("NoSuchProbe")
    msg = str(ei.value)
    assert "MeshStatsProbe" in msg  # roster surfaces in the error
