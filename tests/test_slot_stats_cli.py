"""Phase 4 — slot-stats CLI report tests.

Model-agnostic: tests drive the report against a synthetic catalog
rather than any real reference model.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path



def _seed_synthetic_catalog(db_path: Path) -> None:
    """Seed a catalog with a mix of high/medium/low/scattered entries."""
    from comsol_support.db import init_db, upsert_slot_expected_unit

    conn = init_db(db_path)
    # High confidence — clean distribution, non-trivial unit
    upsert_slot_expected_unit(
        conn, physics_type="ht", sdim="3",
        feature_type="HeatSource", feature_scope="domain",
        slot_property="Q0", comsol_version="6.4",
        n_attempts=50, n_resolved=50, coverage_frac=1.0,
        modal_unit="W/m^3", modal_count=50, modal_frac=1.0,
        distribution={"W/m^3": 50},
        confidence="high", type_source="getType",
    )
    # Medium confidence — smaller sample
    upsert_slot_expected_unit(
        conn, physics_type="es", sdim="3",
        feature_type="Terminal", feature_scope="boundary",
        slot_property="V0", comsol_version="6.4",
        n_attempts=8, n_resolved=8, coverage_frac=1.0,
        modal_unit="V", modal_count=8, modal_frac=1.0,
        distribution={"V": 8},
        confidence="medium", type_source="getType",
    )
    # Low confidence / scattered — bimodal distribution
    upsert_slot_expected_unit(
        conn, physics_type="ht", sdim="3",
        feature_type="HeatSource", feature_scope="boundary",
        slot_property="Qb", comsol_version="6.4",
        n_attempts=30, n_resolved=30, coverage_frac=1.0,
        modal_unit="W/m^2", modal_count=15, modal_frac=0.5,
        distribution={"W/m^2": 15, "W/m": 15},
        confidence="low", type_source="getType",
    )
    # Low coverage
    upsert_slot_expected_unit(
        conn, physics_type="ht", sdim="2",
        feature_type="HeatSource", feature_scope="domain",
        slot_property="Q0", comsol_version="6.4",
        n_attempts=40, n_resolved=5, coverage_frac=0.125,
        modal_unit="W/m^2", modal_count=5, modal_frac=1.0,
        distribution={"W/m^2": 5},
        confidence="low", type_source="getType",
    )
    conn.commit()
    conn.close()


def _run_cli(args: list[str]) -> subprocess.CompletedProcess:
    """Invoke comsol-support via the CLI module directly."""
    return subprocess.run(
        [sys.executable, "-m", "comsol_support.cli", *args],
        capture_output=True, text=True, timeout=30,
    )


def test_slot_stats_default_summary(tmp_path):
    """Default invocation prints a catalog summary."""
    db = tmp_path / "cat.db"
    _seed_synthetic_catalog(db)

    r = _run_cli(["slot-stats", "--db", str(db)])
    assert r.returncode == 0
    assert "Slot expected-unit catalog" in r.stdout
    assert "4 entries" in r.stdout  # total
    assert "high: 1" in r.stdout
    assert "medium: 1" in r.stdout
    assert "low: 2" in r.stdout


def test_slot_stats_high_confidence_view(tmp_path):
    """--show-high-confidence lists only high rows."""
    db = tmp_path / "cat.db"
    _seed_synthetic_catalog(db)

    r = _run_cli([
        "slot-stats", "--db", str(db), "--show-high-confidence"
    ])
    assert r.returncode == 0
    assert "high_confidence" in r.stdout
    assert "HeatSource/Q0" in r.stdout
    # Medium & low not listed in this view
    assert "Terminal/V0" not in r.stdout
    assert "Qb" not in r.stdout


def test_slot_stats_scattered_view(tmp_path):
    """--show-scattered surfaces subvariant-discovery candidates."""
    db = tmp_path / "cat.db"
    _seed_synthetic_catalog(db)

    r = _run_cli([
        "slot-stats", "--db", str(db), "--show-scattered"
    ])
    assert r.returncode == 0
    assert "scattered" in r.stdout
    # The bimodal Qb entry is scattered
    assert "Qb" in r.stdout


def test_slot_stats_coverage_view(tmp_path):
    """--show-coverage surfaces low-coverage_frac candidates."""
    db = tmp_path / "cat.db"
    _seed_synthetic_catalog(db)

    r = _run_cli([
        "slot-stats", "--db", str(db), "--show-coverage"
    ])
    assert r.returncode == 0
    assert "low_coverage" in r.stdout
    # The 2D HeatSource/Q0 entry has coverage_frac=0.125
    assert "HeatSource/Q0" in r.stdout


def test_slot_stats_json_output(tmp_path):
    """--json mode returns parseable JSON with view + rows."""
    db = tmp_path / "cat.db"
    _seed_synthetic_catalog(db)

    r = _run_cli([
        "slot-stats", "--db", str(db),
        "--show-high-confidence", "--json",
    ])
    assert r.returncode == 0
    data = json.loads(r.stdout)
    assert data["view"] == "high_confidence"
    assert len(data["rows"]) == 1
    assert data["rows"][0]["feature_type"] == "HeatSource"
    # Distribution is expanded from JSON-blob to dict in the helper.
    assert data["rows"][0]["distribution"] == {"W/m^3": 50}


def test_slot_stats_mutually_exclusive_views(tmp_path):
    """Only one view flag may be set."""
    db = tmp_path / "cat.db"
    _seed_synthetic_catalog(db)

    r = _run_cli([
        "slot-stats", "--db", str(db),
        "--show-high-confidence", "--show-scattered",
    ])
    assert r.returncode != 0
    # argparse mutually_exclusive_group emits an error
    assert "not allowed" in r.stderr or "--show" in r.stderr
