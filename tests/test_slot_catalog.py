"""Tests for slot_catalog — Phase 2 of the slot expected-unit catalog.

No real COMSOL required. Wolfram (wolframscript) is also mocked — we
construct synthetic DimensionalFinding objects to drive the aggregator
through known outcomes.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from comsol_support.db import (
    count_slot_catalog,
    init_db,
    lookup_expected_unit,
    lookup_variable_declared_units,
)
from comsol_support.dimensional import DimensionalFinding
from comsol_support.slot_catalog import (
    N_HIGH,
    MODAL_HIGH,
    classify_confidence,
    build_global_symbol_table,
    aggregate_dump,
    scattered_keys,
    high_confidence_entries,
    suggest_thresholds,
    _deduce_outcome,
)


# ---- Confidence classifier ----

def test_classify_confidence_high():
    """High tier requires N_HIGH samples, MODAL_HIGH purity,
    COVERAGE_HIGH resolve rate, AND a non-trivial modal unit."""
    conf, cov, mod = classify_confidence(
        n_attempts=N_HIGH, n_resolved=N_HIGH,
        modal_count=int(N_HIGH * MODAL_HIGH),
        modal_unit="W/m^3",
    )
    assert conf == "high"


def test_classify_confidence_dimensionless_demoted():
    """A dimensionless modal unit can never reach 'high' or 'medium'
    (demotion: dimensionless doesn't prescribe anything).
    """
    for unit in ("DimensionlessUnit", '"DimensionlessUnit"', "1", "", None):
        conf, _, _ = classify_confidence(
            n_attempts=N_HIGH, n_resolved=N_HIGH,
            modal_count=N_HIGH,
            modal_unit=unit,
        )
        assert conf == "low", f"expected low for modal_unit={unit!r}"


def test_classify_confidence_low_coverage_blocks_high():
    """Even with purity, low coverage keeps us out of high tier."""
    # 100 attempts, only 10 resolved (coverage 0.1) — cannot be high.
    conf, cov, mod = classify_confidence(
        n_attempts=100, n_resolved=10, modal_count=10, modal_unit="m",
    )
    assert conf != "high"
    assert cov == pytest.approx(0.1)


def test_classify_confidence_medium():
    """Medium tier for 5+ samples with 0.7+ modal purity and non-trivial unit."""
    conf, cov, mod = classify_confidence(
        n_attempts=10, n_resolved=10, modal_count=8, modal_unit="kg/m^3",
    )
    assert conf == "medium"


def test_classify_confidence_low():
    """Scattered distribution falls to low."""
    conf, cov, mod = classify_confidence(
        n_attempts=10, n_resolved=10, modal_count=4, modal_unit="kg",
    )
    assert conf == "low"


def test_classify_confidence_zero_attempts():
    """No attempts → low, no division by zero."""
    conf, cov, mod = classify_confidence(
        n_attempts=0, n_resolved=0, modal_count=0, modal_unit=None,
    )
    assert conf == "low"
    assert cov == 0.0
    assert mod == 0.0


# ---- Deduce outcome ----

def test_deduce_outcome_resolved():
    """Resolved finding with a unit yields ('resolved', normalised_unit)."""
    f = DimensionalFinding(
        id="r0", expression="5[W/m^3]",
        resolved=True, deduced_unit="W/m^3",
    )
    outcome, unit = _deduce_outcome(f)
    assert outcome == "resolved"
    assert unit  # non-empty


def test_deduce_outcome_unresolved():
    """Not resolved → unresolved."""
    f = DimensionalFinding(
        id="r0", expression="unknown",
        resolved=False,
    )
    assert _deduce_outcome(f) == ("unresolved", "")


def test_deduce_outcome_translation_error():
    """Translation error gets its own bucket."""
    f = DimensionalFinding(
        id="r0", expression="!@#$",
        translation_error="parse fail",
    )
    outcome, unit = _deduce_outcome(f)
    assert outcome == "translation_error"
    assert unit == ""


def test_deduce_outcome_non_scalar():
    """Tensor/list results are flagged as non_scalar."""
    f = DimensionalFinding(
        id="r0", expression="{a,b,c}",
        resolved=True, deduced_unit="{m, s}",
    )
    outcome, _ = _deduce_outcome(f)
    assert outcome == "non_scalar"


def test_deduce_outcome_inconsistent_arithmetic():
    """Inconsistent arithmetic counts as unresolved, not resolved."""
    f = DimensionalFinding(
        id="r0", expression="x+y",
        resolved=True, deduced_unit="m",
        inconsistent_arithmetic=True,
    )
    assert _deduce_outcome(f) == ("unresolved", "")


# ---- Aggregation end-to-end (synthetic dump, mocked analyze) ----

def _write_dump(tmp_path: Path, records: list[dict]) -> Path:
    """Write a synthetic JSONL dump for aggregation tests."""
    p = tmp_path / "dump.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in records) + "\n")
    return p


def _make_slot(
    physics_type: str = "ht",
    sdim: str = "3",
    feature_type: str = "HeatSource",
    feature_scope: str = "domain",
    slot_property: str = "Q0",
    expression: str = "5[W/m^3]",
    comsol_version: str = "6.4",
    model_path: str = "/a.mph",
    type_source: str = "getType",
    feature_type_source: str = "getType",
    filter_source: str = "regex_heuristic",
    schema_version: int = 1,
) -> dict:
    return {
        "kind": "slot",
        "schema_version": schema_version,
        "model_path": model_path,
        "comsol_version": comsol_version,
        "physics_tag": "ht",
        "physics_type": physics_type,
        "type_source": type_source,
        "sdim": sdim,
        "feature_tag": "hs1",
        "feature_type": feature_type,
        "feature_type_source": feature_type_source,
        "feature_scope": feature_scope,
        "slot_property": slot_property,
        "expression": expression,
        "filter_source": filter_source,
    }


def _mock_analyze_all_resolved_to(unit: str):
    """Return an `analyze` mock that resolves every request to `unit`."""
    def _fake(requests, table, *, wolframscript_path=None, timeout_s=None):
        return [
            DimensionalFinding(
                id=r.id, expression=r.expression,
                resolved=True, deduced_unit=unit,
            )
            for r in requests
        ]
    return _fake


def test_aggregate_single_bucket_high_confidence(tmp_path):
    """20+ resolved records in one bucket → high confidence."""
    records = [
        _make_slot(model_path=f"/m{i}.mph") for i in range(25)
    ]
    dump = _write_dump(tmp_path, records)
    conn = init_db(tmp_path / "test.db")

    with patch(
        "comsol_support.slot_catalog.analyze",
        side_effect=_mock_analyze_all_resolved_to("W/m^3"),
    ):
        report = aggregate_dump(dump, conn)

    assert report.slots_read == 25
    assert report.slots_resolved == 25
    assert report.catalog_keys == 1
    assert report.catalog_high == 1

    row = lookup_expected_unit(
        conn,
        physics_type="ht", sdim="3",
        feature_type="HeatSource", feature_scope="domain",
        slot_property="Q0", comsol_version="6.4",
    )
    assert row is not None
    assert row["confidence"] == "high"
    assert row["n_attempts"] == 25
    assert row["n_resolved"] == 25
    assert row["coverage_frac"] == pytest.approx(1.0)
    assert row["modal_unit"] == "W/m^3" or row["modal_unit"].endswith("m^3")
    assert row["modal_frac"] == pytest.approx(1.0)
    assert row["version_mismatch"] is False


def test_aggregate_scattered_distribution_low_modal(tmp_path):
    """Mixed resolved units → distribution captured, low modal_frac."""
    records = [_make_slot(model_path=f"/a{i}.mph") for i in range(10)]
    records += [_make_slot(model_path=f"/b{i}.mph") for i in range(10)]
    dump = _write_dump(tmp_path, records)
    conn = init_db(tmp_path / "test.db")

    # First 10 resolve to W/m^3, next 10 resolve to W/m^2 — scatter.
    def fake(requests, table, *, wolframscript_path=None, timeout_s=None):
        out = []
        for i, r in enumerate(requests):
            unit = "W/m^3" if i < 10 else "W/m^2"
            out.append(DimensionalFinding(
                id=r.id, expression=r.expression,
                resolved=True, deduced_unit=unit,
            ))
        return out

    with patch("comsol_support.slot_catalog.analyze", side_effect=fake):
        report = aggregate_dump(dump, conn)

    assert report.catalog_keys == 1
    row = conn.execute(
        "SELECT * FROM slot_expected_units"
    ).fetchone()
    dist = json.loads(row["distribution"])
    assert set(dist.keys()) >= {"W/m^3", "W/m^2"}
    assert sum(dist.values()) == 20
    assert row["modal_frac"] == pytest.approx(0.5)
    # Not high (modal_frac < MODAL_HIGH), not medium (modal_frac < MODAL_MED).
    assert row["confidence"] == "low"


def test_aggregate_subvariant_keys_separate(tmp_path):
    """Same feature at different sdim → distinct catalog entries."""
    records = [
        _make_slot(sdim="3", model_path=f"/3d{i}.mph") for i in range(5)
    ] + [
        _make_slot(sdim="2", model_path=f"/2d{i}.mph") for i in range(5)
    ]
    dump = _write_dump(tmp_path, records)
    conn = init_db(tmp_path / "test.db")

    def fake(requests, table, *, wolframscript_path=None, timeout_s=None):
        return [
            DimensionalFinding(
                id=r.id, expression=r.expression,
                resolved=True,
                deduced_unit="W/m^3" if "3d" in r.expression else "W/m^2",
            )
            for r in requests
        ]

    # Drive different units by expression-string difference.
    for i, r in enumerate(records[:5]):
        r["expression"] = f"3d_expr_{i}"
    for i, r in enumerate(records[5:]):
        r["expression"] = f"2d_expr_{i}"
    dump = _write_dump(tmp_path, records)

    with patch("comsol_support.slot_catalog.analyze", side_effect=fake):
        aggregate_dump(dump, conn)

    keys = conn.execute(
        "SELECT sdim, COUNT(*) as n, modal_unit FROM slot_expected_units "
        "GROUP BY sdim"
    ).fetchall()
    by_sdim = {r["sdim"]: (r["n"], r["modal_unit"]) for r in keys}
    assert "3" in by_sdim and "2" in by_sdim


def test_aggregate_low_coverage_suppresses_high(tmp_path):
    """20 attempts, only 4 resolved → coverage_frac < COVERAGE_HIGH → not high."""
    records = [_make_slot(model_path=f"/m{i}.mph") for i in range(25)]
    dump = _write_dump(tmp_path, records)
    conn = init_db(tmp_path / "test.db")

    def fake(requests, table, *, wolframscript_path=None, timeout_s=None):
        out = []
        for i, r in enumerate(requests):
            # Only first 4 resolve
            if i < 4:
                out.append(DimensionalFinding(
                    id=r.id, expression=r.expression,
                    resolved=True, deduced_unit="W/m^3",
                ))
            else:
                out.append(DimensionalFinding(
                    id=r.id, expression=r.expression,
                    resolved=False,
                ))
        return out

    with patch("comsol_support.slot_catalog.analyze", side_effect=fake):
        aggregate_dump(dump, conn)

    row = conn.execute("SELECT * FROM slot_expected_units").fetchone()
    assert row["confidence"] != "high"
    assert row["coverage_frac"] < 0.5


def test_aggregate_skips_unknown_schema_versions(tmp_path):
    """Records with schema_version greater than EXPECTED are skipped and
    surfaced in the report's errors list. Prevents silent ingestion of
    a dump produced by a newer harvester than the aggregator knows."""
    from comsol_support.slot_catalog import EXPECTED_SCHEMA_VERSION

    records = [
        _make_slot(schema_version=EXPECTED_SCHEMA_VERSION + 5,
                   model_path="/future.mph"),
        _make_slot(model_path="/now.mph"),  # default schema_version=1
    ]
    dump = _write_dump(tmp_path, records)
    conn = init_db(tmp_path / "test.db")

    with patch(
        "comsol_support.slot_catalog.analyze",
        side_effect=_mock_analyze_all_resolved_to("W/m^3"),
    ):
        report = aggregate_dump(dump, conn)

    # Only the current-schema record contributed.
    assert report.slots_read == 1
    assert any(
        "schema_version" in e for e in report.errors
    ), f"expected schema_version error in {report.errors}"


def test_aggregate_skips_records_without_schema_version(tmp_path):
    """Pre-schema legacy dumps (records without schema_version) are
    skipped — safer than guessing their shape."""
    records = [
        _make_slot(model_path="/modern.mph"),  # schema_version=1
    ]
    rec_legacy = _make_slot(model_path="/legacy.mph")
    del rec_legacy["schema_version"]
    records.append(rec_legacy)
    dump = _write_dump(tmp_path, records)
    conn = init_db(tmp_path / "test.db")

    with patch(
        "comsol_support.slot_catalog.analyze",
        side_effect=_mock_analyze_all_resolved_to("W/m^3"),
    ):
        report = aggregate_dump(dump, conn)

    assert report.slots_read == 1  # only the modern record ingested


def test_aggregate_translation_error_isolated(tmp_path):
    """Translation errors bucketed separately, do not inflate n_resolved."""
    records = [_make_slot(model_path=f"/m{i}.mph") for i in range(5)]
    dump = _write_dump(tmp_path, records)
    conn = init_db(tmp_path / "test.db")

    def fake(requests, table, *, wolframscript_path=None, timeout_s=None):
        return [
            DimensionalFinding(
                id=r.id, expression=r.expression,
                translation_error="parse fail",
            )
            for r in requests
        ]

    with patch("comsol_support.slot_catalog.analyze", side_effect=fake):
        report = aggregate_dump(dump, conn)

    assert report.slots_translation_error == 5
    assert report.slots_resolved == 0
    row = conn.execute("SELECT * FROM slot_expected_units").fetchone()
    assert row["n_attempts"] == 5
    assert row["n_resolved"] == 0


# ---- Source B ----

def test_aggregate_variable_declared_units(tmp_path):
    """var_unit records populate variable_declared_units with occurrence counts."""
    records = [
        {
            "kind": "var_unit",
            "schema_version": 1,
            "model_path": f"/m{i}.mph",
            "comsol_version": "6.4",
            "var_name": "rho_val",
            "sdim": "3",
            "scope": "comp1/var_b",
            "declaration_key": "CustomSourceTermUnit",
            "declared_unit": "kg/m^3",
        }
        for i in range(4)
    ]
    # Add a rarer alternative for the same variable
    records.append({
        "kind": "var_unit",
        "schema_version": 1,
        "model_path": "/m99.mph",
        "comsol_version": "6.4",
        "var_name": "rho_val",
        "sdim": "3",
        "scope": "comp1/var_b",
        "declaration_key": "CustomSourceTermUnit",
        "declared_unit": "g/cm^3",
    })
    dump = _write_dump(tmp_path, records)
    conn = init_db(tmp_path / "test.db")

    with patch(
        "comsol_support.slot_catalog.analyze", return_value=[],
    ):
        report = aggregate_dump(dump, conn)

    assert report.var_unit_records == 5
    rows = lookup_variable_declared_units(
        conn, "rho_val", physics_type="unknown", sdim="3"
    )
    # Physics type is "unknown" because these var_unit records don't
    # carry physics_type directly (they're model-global).
    assert len(rows) == 2
    by_unit = {r["declared_unit"]: r["n_occurrences"] for r in rows}
    assert by_unit["kg/m^3"] == 4
    assert by_unit["g/cm^3"] == 1


def test_build_global_symbol_table_majority_only(tmp_path):
    """Symbol table includes vars whose majority unit exceeds 90%."""
    conn = init_db(tmp_path / "test.db")
    # rho: 19 occurrences kg/m^3 vs 1 occurrence g/cm^3 — majority 95%.
    conn.execute(
        "INSERT INTO variable_declared_units VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("rho", "ht", "3", "Custom", "kg/m^3", 19, "6.4"),
    )
    conn.execute(
        "INSERT INTO variable_declared_units VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("rho", "ht", "3", "Custom", "g/cm^3", 1, "6.4"),
    )
    # ambiguous_var: 50/50 split — excluded
    conn.execute(
        "INSERT INTO variable_declared_units VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("ambiguous", "x", "3", "C", "A", 5, "6.4"),
    )
    conn.execute(
        "INSERT INTO variable_declared_units VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("ambiguous", "x", "3", "C", "B", 5, "6.4"),
    )
    conn.commit()

    table = build_global_symbol_table(conn)
    # rho is dominant enough to appear
    assert "rho" in table
    # ambiguous is not
    assert "ambiguous" not in table


# ---- Version fallback ----

def test_lookup_version_fallback(tmp_path):
    """If no row for requested version, fall back to highest available
    and mark version_mismatch=True."""
    conn = init_db(tmp_path / "test.db")
    # Seed one row at version 6.3
    conn.execute(
        """
        INSERT INTO slot_expected_units VALUES (
            'ht', '3', 'HeatSource', 'domain', 'Q0', '6.3',
            25, 25, 1.0,
            'W/m^3', 25, 1.0,
            '{"W/m^3": 25}', 'high', 'getType', datetime('now')
        )
        """
    )
    conn.commit()

    # Ask for 6.4
    row = lookup_expected_unit(
        conn,
        physics_type="ht", sdim="3",
        feature_type="HeatSource", feature_scope="domain",
        slot_property="Q0", comsol_version="6.4",
    )
    assert row is not None
    assert row["version_mismatch"] is True
    assert row["comsol_version"] == "6.3"
    assert row["modal_unit"] == "W/m^3"


def test_lookup_exact_version_preferred(tmp_path):
    """When an exact match exists, version_mismatch=False."""
    conn = init_db(tmp_path / "test.db")
    conn.execute(
        """
        INSERT INTO slot_expected_units VALUES (
            'ht', '3', 'HeatSource', 'domain', 'Q0', '6.4',
            25, 25, 1.0,
            'W/m^3', 25, 1.0,
            '{"W/m^3": 25}', 'high', 'getType', datetime('now')
        )
        """
    )
    conn.commit()

    row = lookup_expected_unit(
        conn,
        physics_type="ht", sdim="3",
        feature_type="HeatSource", feature_scope="domain",
        slot_property="Q0", comsol_version="6.4",
    )
    assert row is not None
    assert row["version_mismatch"] is False


def test_lookup_confidence_filter(tmp_path):
    """min_confidence filter excludes lower-tier rows."""
    conn = init_db(tmp_path / "test.db")
    conn.execute(
        """
        INSERT INTO slot_expected_units VALUES (
            'ht', '3', 'Low', 'domain', 'Q0', '6.4',
            5, 5, 1.0,
            'W/m', 5, 1.0,
            '{"W/m": 5}', 'medium', 'getType', datetime('now')
        )
        """
    )
    conn.commit()

    # Default min_confidence='high' filters this medium row out
    assert lookup_expected_unit(
        conn,
        physics_type="ht", sdim="3", feature_type="Low",
        feature_scope="domain", slot_property="Q0",
        comsol_version="6.4",
    ) is None

    # Relax to medium — now hits
    row = lookup_expected_unit(
        conn,
        physics_type="ht", sdim="3", feature_type="Low",
        feature_scope="domain", slot_property="Q0",
        comsol_version="6.4",
        min_confidence="medium",
    )
    assert row is not None


# ---- Scatter/coverage reports (Phase 4 hooks) ----

def test_scattered_keys_report(tmp_path):
    """scattered_keys returns entries with n_attempts>=N_MED and low modal."""
    conn = init_db(tmp_path / "test.db")
    # High-purity row — NOT scattered
    conn.execute(
        """
        INSERT INTO slot_expected_units VALUES (
            'ht', '3', 'A', 'domain', 'Q0', '6.4',
            20, 20, 1.0, 'W/m^3', 20, 1.0,
            '{"W/m^3": 20}', 'high', 'getType', datetime('now')
        )
        """
    )
    # Scattered row
    conn.execute(
        """
        INSERT INTO slot_expected_units VALUES (
            'ht', '3', 'B', 'domain', 'Q0', '6.4',
            10, 10, 1.0, 'W/m^3', 5, 0.5,
            '{"W/m^3": 5, "W/m^2": 5}', 'low', 'getType', datetime('now')
        )
        """
    )
    conn.commit()

    scattered = scattered_keys(conn)
    types = {r["feature_type"] for r in scattered}
    assert "B" in types
    assert "A" not in types


def test_count_slot_catalog(tmp_path):
    """Catalog summary counts by confidence tier."""
    conn = init_db(tmp_path / "test.db")
    for feat, conf in [("A", "high"), ("B", "high"), ("C", "medium"), ("D", "low")]:
        conn.execute(
            """
            INSERT INTO slot_expected_units VALUES (
                'ht', '3', ?, 'domain', 'Q0', '6.4',
                20, 20, 1.0, 'W/m', 20, 1.0,
                '{"W/m": 20}', ?, 'getType', datetime('now')
            )
            """, (feat, conf),
        )
    conn.commit()

    counts = count_slot_catalog(conn)
    assert counts["total"] == 4
    assert counts["high"] == 2
    assert counts["medium"] == 1
    assert counts["low"] == 1


def test_suggest_thresholds_insufficient_data(tmp_path):
    """Empty / tiny catalog returns current constants with a 'too few'
    rationale note rather than suggesting aggressive changes."""
    conn = init_db(tmp_path / "test.db")
    # Seed 3 rows only — below the 20-row promotable floor.
    for feat in ("A", "B", "C"):
        conn.execute(
            """
            INSERT INTO slot_expected_units VALUES (
                'ht', '3', ?, 'domain', 'Q0', '6.4',
                10, 10, 1.0, 'W/m^3', 10, 1.0,
                '{"W/m^3": 10}', 'medium', 'getType', datetime('now')
            )
            """, (feat,),
        )
    conn.commit()

    result = suggest_thresholds(conn)
    assert result["suggested"] == result["current"]
    assert any("too few" in r.lower() for r in result["rationale"])


def test_suggest_thresholds_returns_valid_values(tmp_path):
    """Given a catalog with 40+ promotable rows, suggestions are bounded
    and monotonic (N_MED < N_HIGH, both ≥ current floors)."""
    conn = init_db(tmp_path / "test.db")
    for i in range(40):
        conn.execute(
            """
            INSERT INTO slot_expected_units VALUES (
                'ht', '3', ?, 'domain', 'Q0', '6.4',
                ?, ?, 1.0, 'W/m^3', ?, 1.0,
                '{"W/m^3": 10}', 'medium', 'getType', datetime('now')
            )
            """, (f"F{i}", 5 + i * 2, 5 + i * 2, 5 + i * 2),
        )
    conn.commit()

    result = suggest_thresholds(conn, target_high_count=5, target_medium_count=20)
    sug = result["suggested"]
    cur = result["current"]

    # Never weakens the bar.
    assert sug["N_HIGH"] >= cur["N_HIGH"]
    assert sug["MODAL_HIGH"] >= cur["MODAL_HIGH"]
    assert sug["N_MED"] >= cur["N_MED"]

    # Monotonic: medium floor is below high floor.
    assert sug["N_MED"] < sug["N_HIGH"]

    # Coverage clamped into sensible range.
    assert 0.3 <= sug["COVERAGE_HIGH"] <= 0.7


def test_suggest_thresholds_ignores_dimensionless_rows(tmp_path):
    """Dimensionless rows are excluded from the promotable-row pool
    (they can't reach high regardless of threshold choice)."""
    conn = init_db(tmp_path / "test.db")
    # 25 dimensionless rows (noise).
    for i in range(25):
        conn.execute(
            """
            INSERT INTO slot_expected_units VALUES (
                'ht', '3', ?, 'domain', 'enum', '6.4',
                100, 100, 1.0, 'DimensionlessUnit', 100, 1.0,
                '{"DimensionlessUnit": 100}', 'low', 'getType', datetime('now')
            )
            """, (f"D{i}",),
        )
    conn.commit()

    result = suggest_thresholds(conn)
    # Promotable rows count should be 0, triggering the "too few" path.
    assert result["observed"]["promotable_rows"] == 0
    assert result["suggested"] == result["current"]


def test_high_confidence_entries_ordered_by_samples(tmp_path):
    """high_confidence_entries returns only 'high' rows, sorted desc by n_attempts."""
    conn = init_db(tmp_path / "test.db")
    for feat, n in [("A", 25), ("B", 50), ("C", 30)]:
        conn.execute(
            """
            INSERT INTO slot_expected_units VALUES (
                'ht', '3', ?, 'domain', 'Q0', '6.4',
                ?, ?, 1.0, 'W/m', ?, 1.0,
                '{"W/m": 25}', 'high', 'getType', datetime('now')
            )
            """, (feat, n, n, n),
        )
    conn.commit()

    entries = high_confidence_entries(conn)
    types = [e["feature_type"] for e in entries]
    assert types == ["B", "C", "A"]
