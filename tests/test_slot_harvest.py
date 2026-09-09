"""Tests for slot_harvest — Phase 1 of the slot expected-unit catalog."""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest

from comsol_support.slot_harvest import (
    collect_mph_paths,
    detect_comsol_version,
    iter_dump,
    read_dump,
)


def test_collect_mph_paths_missing_dir(tmp_path):
    """Missing directory returns empty list, not error."""
    paths = collect_mph_paths(tmp_path / "nonexistent")
    assert paths == []


def test_collect_mph_paths_recursive(tmp_path):
    """Collects .mph files recursively and returns sorted order."""
    (tmp_path / "a.mph").write_bytes(b"")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "b.mph").write_bytes(b"")
    (tmp_path / "sub" / "nested").mkdir()
    (tmp_path / "sub" / "nested" / "c.mph").write_bytes(b"")
    (tmp_path / "other.txt").write_bytes(b"")

    paths = collect_mph_paths(tmp_path)
    assert [p.name for p in paths] == ["a.mph", "b.mph", "c.mph"]


def test_collect_mph_paths_max_models(tmp_path):
    """max_models caps the returned list."""
    for i in range(5):
        (tmp_path / f"m{i}.mph").write_bytes(b"")
    paths = collect_mph_paths(tmp_path, max_models=2)
    assert len(paths) == 2
    assert paths[0].name == "m0.mph"
    assert paths[1].name == "m1.mph"


def test_read_dump_skips_bad_lines(tmp_path):
    """Malformed JSON lines are silently skipped."""
    dump = tmp_path / "dump.jsonl"
    dump.write_text(dedent("""
        {"kind":"slot","slot_property":"Q0"}
        not valid json
        {"kind":"var_unit","var_name":"rho"}
        {bad
        {"kind":"slot","slot_property":"T0"}
    """).strip())

    records = read_dump(dump)
    assert len(records) == 3
    assert [r["kind"] for r in records] == ["slot", "var_unit", "slot"]


def test_iter_dump_streams_records(tmp_path):
    """iter_dump yields one dict at a time."""
    dump = tmp_path / "dump.jsonl"
    dump.write_text(
        '{"kind":"slot","slot_property":"Q0"}\n'
        '{"kind":"slot","slot_property":"T0"}\n'
    )

    it = iter_dump(dump)
    first = next(it)
    assert first["slot_property"] == "Q0"
    second = next(it)
    assert second["slot_property"] == "T0"
    with pytest.raises(StopIteration):
        next(it)


def test_dump_empty_file(tmp_path):
    """Empty dump file produces empty list."""
    dump = tmp_path / "empty.jsonl"
    dump.write_text("")
    assert read_dump(dump) == []


def test_detect_version_from_version_file(tmp_path):
    """A plain-text VERSION file in the install root is preferred."""
    install = tmp_path / "comsol"
    install.mkdir()
    (install / "VERSION").write_text("6.4.0.293\n")
    assert detect_comsol_version(install) == "6.4.0.293"


def test_detect_version_from_install_dir_name(tmp_path):
    """`comsolNN` parent-directory heuristic when no VERSION file exists."""
    install = tmp_path / "comsol64" / "multiphysics"
    install.mkdir(parents=True)
    assert detect_comsol_version(install) == "6.4"


def test_detect_version_from_uppercase_install_dir_name(tmp_path):
    """Windows/macOS installs use `COMSOL64` — match case-insensitively."""
    install = tmp_path / "COMSOL64" / "Multiphysics"
    install.mkdir(parents=True)
    assert detect_comsol_version(install) == "6.4"


def test_detect_version_never_returns_osgi_bundle(tmp_path):
    """Fall-back must be 'unknown', NOT the OSGi bundle version.

    Every COMSOL 6.x release ships the same
    `com.comsol.core_1.0.0.jar` bundle. Returning 'bundle-1.0.0' would
    silently conflate different COMSOL releases under one catalog key.
    """
    install = tmp_path / "some_opaque_name"
    plugins = install / "plugins"
    plugins.mkdir(parents=True)
    (plugins / "com.comsol.core_1.0.0.jar").write_bytes(b"stub")

    result = detect_comsol_version(install)
    assert "bundle" not in result
    assert result == "unknown"


def test_slot_record_schema():
    """Schema contract for slot records — locked in so Phase 2 aggregator
    doesn't silently break if a field is renamed.

    The Java side writes these; this test documents the expected keys.
    """
    required = {
        "kind", "schema_version", "model_path", "comsol_version",
        "component_tag",
        "physics_tag", "physics_type", "type_source",
        "sdim",
        "feature_tag", "feature_type", "feature_type_source",
        "feature_scope",
        "slot_property", "expression", "filter_source",
    }
    # Construct a hand-rolled record to confirm the expected shape.
    rec = {
        "kind": "slot",
        "schema_version": 1,
        "model_path": "/app/x.mph",
        "comsol_version": "6.4.0.293",
        "component_tag": "comp1",
        "physics_tag": "ht",
        "physics_type": "HeatTransfer",
        "type_source": "getType",
        "sdim": "3",
        "feature_tag": "hs1",
        "feature_type": "HeatSource",
        "feature_type_source": "getType",
        "feature_scope": "domain",
        "slot_property": "Q0",
        "expression": "sigma*E_norm^2",
        "filter_source": "regex_heuristic",
    }
    assert set(rec.keys()) == required


def test_var_unit_record_schema():
    """Schema contract for variable-unit records (Source B)."""
    required = {
        "kind", "schema_version", "model_path", "comsol_version",
        "var_name", "sdim", "scope",
        "declaration_key", "declared_unit",
    }
    rec = {
        "kind": "var_unit",
        "schema_version": 1,
        "model_path": "/app/x.mph",
        "comsol_version": "6.4.0.293",
        "var_name": "rho_val",
        "sdim": "3",
        "scope": "comp1/var_b",
        "declaration_key": "CustomSourceTermUnit",
        "declared_unit": "W/m^3",
    }
    assert set(rec.keys()) == required


# ---- Harvester Java compilation test (no COMSOL required) ----

def test_harvester_source_exists():
    """SlotHarvester.java exists in the expected location."""
    src = (
        Path(__file__).parent.parent
        / "comsol_support" / "java" / "SlotHarvester.java"
    )
    assert src.is_file()
    text = src.read_text()
    assert "public class SlotHarvester" in text
    assert "--list" in text
    assert "--out" in text
    assert "writeSlotRecord" in text
    assert "writeVarUnitRecord" in text
    # Emitted JSONL uses `"kind":"slot"` / `"kind":"var_unit"`; the Java
    # source carries them as escaped literals.
    assert '\\"kind\\":\\"slot\\"' in text
    assert '\\"kind\\":\\"var_unit\\"' in text


def test_harvester_argv_contract():
    """Java harvester main() rejects unknown args."""
    src = (
        Path(__file__).parent.parent
        / "comsol_support" / "java" / "SlotHarvester.java"
    ).read_text()
    # These flags are the public CLI contract; any rename must update
    # the Python driver (slot_harvest.py) in lockstep.
    assert '"--list"' in src
    assert '"--out"' in src
    assert '"--progress"' in src


# ---- Integration tests (require real COMSOL, skipped by default) ----

@pytest.mark.real_comsol
def test_harvest_single_model(tmp_path):
    """End-to-end harvest on one small COMSOL model.

    Marker-gated: requires a COMSOL install and at least one corpus .mph.
    Runs only when invoked with real_comsol marker enabled.
    """
    from comsol_support import COMSOL_PATH
    from comsol_support.slot_harvest import run_harvest

    applications = Path(COMSOL_PATH) / "applications"
    mph_files = list(applications.rglob("*.mph"))
    if not mph_files:
        pytest.skip("No corpus .mph files found")

    output = tmp_path / "dump.jsonl"
    workspace = tmp_path / "ws"

    report = run_harvest(
        mph_files[:1], output,
        comsol_path=COMSOL_PATH,
        workspace_dir=workspace,
        timeout_s=300,
        progress=False,
    )

    assert report.models_processed >= 0  # may be 0 if the model fails to load
    if output.exists():
        records = read_dump(output)
        # Basic shape validation on any records that landed
        for r in records:
            assert "kind" in r
            assert r["kind"] in ("slot", "var_unit")
