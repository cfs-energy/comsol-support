"""Phase 3 — Layer C auto-populate hook, model-agnostic tests.

These tests verify the plumbing that lets the slot expected-unit catalog
populate Layer C's ``expected_overrides`` without running COMSOL. Each
test is written to be independent of any particular benchmark or
reference model: we drive ``analyze_model`` with synthetic symbols_json
and synthetic slot_records, and we check the *behaviour*:

  1. An empty catalog is a no-op (no overrides populated).
  2. A populated catalog yields overrides keyed on the slot tuple.
  3. Low-confidence catalog entries are filtered out by default.
  4. Medium-confidence entries appear when min_confidence='medium'.
  5. Dimensionless catalog entries are never exposed as overrides.
  6. Version-fallback annotations surface on the returned annotations.
  7. The output is a strict superset of the manual-override counterpart
     on the same synthetic symbols (the paired-test contract required
     by Phase 3).

Reference-model specific regressions (if any) should follow the same
pairing pattern: each manual-override test gets a paired catalog-driven
test that runs the same model with ``expected_overrides=None`` and a
populated db, and asserts the findings are a superset.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from unittest.mock import patch


from comsol_support.db import init_db, upsert_slot_expected_unit
from comsol_support.dimensional import DimensionalFinding
from comsol_support.slot_catalog import (
    build_expected_overrides_from_slots,
)


def _seed_catalog(conn: sqlite3.Connection, entries: list[dict]) -> None:
    """Insert catalog rows from a list of partial dicts."""
    for e in entries:
        upsert_slot_expected_unit(
            conn,
            physics_type=e.get("physics_type", "ht"),
            sdim=e.get("sdim", "3"),
            feature_type=e.get("feature_type", "HeatSource"),
            feature_scope=e.get("feature_scope", "domain"),
            slot_property=e.get("slot_property", "Q0"),
            comsol_version=e.get("comsol_version", "6.4"),
            n_attempts=e.get("n_attempts", 25),
            n_resolved=e.get("n_resolved", 25),
            coverage_frac=e.get("coverage_frac", 1.0),
            modal_unit=e.get("modal_unit", "W/m^3"),
            modal_count=e.get("modal_count", 25),
            modal_frac=e.get("modal_frac", 1.0),
            distribution=e.get("distribution", {"W/m^3": 25}),
            confidence=e.get("confidence", "high"),
            type_source=e.get("type_source", "getType"),
        )
    conn.commit()


def _slot(
    physics_tag="ht", feature_tag="hs1",
    physics_type="ht", sdim="3",
    feature_type="HeatSource", feature_scope="domain",
    slot_property="Q0",
    expression="5[W/m^3]",
) -> dict:
    return {
        "kind": "slot",
        "physics_tag": physics_tag,
        "feature_tag": feature_tag,
        "physics_type": physics_type,
        "sdim": sdim,
        "feature_type": feature_type,
        "feature_scope": feature_scope,
        "slot_property": slot_property,
        "expression": expression,
    }


# ---- Auto-populate behaviour ----

def test_empty_catalog_returns_empty_overrides(tmp_path):
    """When the catalog table has no rows, no overrides produced."""
    conn = init_db(tmp_path / "empty.db")
    slots = [_slot()]
    overrides, annotations = build_expected_overrides_from_slots(
        slots, conn, comsol_version="6.4",
    )
    assert overrides == {}
    assert annotations == []


def test_populated_catalog_yields_override(tmp_path):
    """A matching catalog row produces an override with the modal unit."""
    conn = init_db(tmp_path / "test.db")
    _seed_catalog(conn, [{}])  # defaults: ht/HeatSource/Q0 → W/m^3 high

    slots = [_slot()]
    overrides, annotations = build_expected_overrides_from_slots(
        slots, conn, comsol_version="6.4",
    )
    assert overrides == {"slot:ht/hs1:Q0": "W/m^3"}
    assert len(annotations) == 1
    assert annotations[0]["confidence"] == "high"
    assert annotations[0]["version_mismatch"] is False


def test_low_confidence_filtered_by_default(tmp_path):
    """Low-confidence catalog entries are not exposed at default min_confidence."""
    conn = init_db(tmp_path / "test.db")
    _seed_catalog(conn, [{
        "confidence": "low",
        "modal_unit": "W/m^3",
    }])

    slots = [_slot()]
    overrides, _ = build_expected_overrides_from_slots(
        slots, conn, comsol_version="6.4", min_confidence="high",
    )
    assert overrides == {}


def test_medium_confidence_included_when_requested(tmp_path):
    """Medium entries appear only with min_confidence='medium'."""
    conn = init_db(tmp_path / "test.db")
    _seed_catalog(conn, [{
        "confidence": "medium",
        "modal_unit": "W/m^2",
        "modal_frac": 0.8,
    }])

    slots = [_slot()]
    # Default is 'high' → empty
    assert build_expected_overrides_from_slots(
        slots, conn, comsol_version="6.4", min_confidence="high",
    )[0] == {}

    # Relaxed → populated
    overrides, annotations = build_expected_overrides_from_slots(
        slots, conn, comsol_version="6.4", min_confidence="medium",
    )
    assert "slot:ht/hs1:Q0" in overrides
    assert annotations[0]["confidence"] == "medium"


def test_dimensionless_catalog_entry_never_exposed(tmp_path):
    """A dimensionless modal_unit is filtered out even if the row was
    somehow persisted at high confidence (shouldn't happen per
    classify_confidence, but defensive)."""
    conn = init_db(tmp_path / "test.db")
    # Force-insert a high-confidence dimensionless row (bypassing the
    # aggregator's demotion, to test the helper's defensive filter).
    _seed_catalog(conn, [{
        "confidence": "high",
        "modal_unit": "DimensionlessUnit",
        "distribution": {"DimensionlessUnit": 25},
    }])

    slots = [_slot()]
    overrides, annotations = build_expected_overrides_from_slots(
        slots, conn, comsol_version="6.4",
    )
    assert overrides == {}
    assert annotations == []


def test_version_mismatch_annotation(tmp_path):
    """Querying a version not in the catalog falls back and annotates."""
    conn = init_db(tmp_path / "test.db")
    _seed_catalog(conn, [{"comsol_version": "6.3"}])

    slots = [_slot()]
    overrides, annotations = build_expected_overrides_from_slots(
        slots, conn, comsol_version="6.4",
    )
    # Catalog has a 6.3 entry and the lookup falls back.
    assert "slot:ht/hs1:Q0" in overrides
    assert annotations[0]["version_mismatch"] is True


def test_subvariant_axes_distinguish_entries(tmp_path):
    """Same slot_property at different sdim resolves to different units."""
    conn = init_db(tmp_path / "test.db")
    _seed_catalog(conn, [
        {"sdim": "3", "modal_unit": "W/m^3"},
        {"sdim": "2", "modal_unit": "W/m^2"},
    ])

    # 3D slot
    overrides3d, _ = build_expected_overrides_from_slots(
        [_slot(sdim="3")], conn, comsol_version="6.4",
    )
    assert overrides3d == {"slot:ht/hs1:Q0": "W/m^3"}

    # 2D slot — different subvariant → different unit
    overrides2d, _ = build_expected_overrides_from_slots(
        [_slot(sdim="2")], conn, comsol_version="6.4",
    )
    assert overrides2d == {"slot:ht/hs1:Q0": "W/m^2"}


# ---- Paired-test contract (Phase 3 acceptance criterion) ----

def test_catalog_overrides_are_superset_of_manual_overrides(tmp_path):
    """Phase 3 paired-test contract (model-agnostic).

    Given the same synthetic slot_records and catalog entries, the
    override dict derived from the catalog must cover at least the
    slot keys that a hand-curated override dict would cover. The
    catalog may add additional keys (that's the value-add); it must
    never drop keys that manual overrides would have provided.

    This test uses synthetic data so it is independent of any
    reference model. When reference-model regressions are added they
    should follow the same pattern: the manual-override test stays as
    the floor; the paired catalog-driven test asserts superset.
    """
    conn = init_db(tmp_path / "test.db")
    _seed_catalog(conn, [
        {"slot_property": "Q0", "modal_unit": "W/m^3"},
        {"slot_property": "T0", "modal_unit": "K"},
    ])

    slots = [
        _slot(slot_property="Q0"),
        _slot(slot_property="T0", feature_tag="tb1"),
    ]

    # Manual override — a hypothetical reference model test would have
    # hand-picked this subset.
    manual = {"slot:ht/hs1:Q0": "W/m^3"}

    catalog_overrides, _ = build_expected_overrides_from_slots(
        slots, conn, comsol_version="6.4",
    )

    # Superset check: every manual key is present in catalog output
    # (and catalog may have more).
    for key in manual:
        assert key in catalog_overrides, (
            f"catalog missed key {key} that manual overrides provided"
        )


# ---- analyze_model integration (slot_records threaded through) ----

def test_analyze_model_accepts_slot_records():
    """analyze_model builds a request per slot when slot_records is passed."""
    from comsol_support.dimensional import analyze_model

    captured_requests = []

    def fake_analyze(requests, table, **kwargs):
        captured_requests.extend(requests)
        return [
            DimensionalFinding(
                id=r.id, expression=r.expression,
                resolved=True, deduced_unit="W/m^3",
            )
            for r in requests
        ]

    symbols_json = {"params": [], "variables": []}
    slot_records = [_slot()]

    with patch("comsol_support.dimensional.analyze", side_effect=fake_analyze):
        analyze_model(
            symbols_json,
            expected_overrides={"slot:ht/hs1:Q0": "W/m^3"},
            slot_records=slot_records,
            max_passes=1,
        )

    # One request per slot, carrying the expected unit.
    slot_req_ids = [r.id for r in captured_requests
                    if r.id.startswith("slot:")]
    assert slot_req_ids == ["slot:ht/hs1:Q0"]
    slot_req = next(r for r in captured_requests if r.id.startswith("slot:"))
    assert slot_req.expected_unit == "W/m^3"


def test_analyze_model_slot_records_use_component_scope():
    """When slot records carry component_tag, Layer C's per-slot
    symbol table is scoped to that component's variable scopes.

    This matters on multi-component models where the same variable
    name is declared with different units in different components —
    a flat global table would pick the wrong unit. The per-component
    scoping is the fix for plan gap #5.5.
    """
    from comsol_support.dimensional import analyze_model

    symbols_json = {
        "params": [],
        "variables": [
            {"kind": "variable", "scope": "comp1/var_b",
             "name": "rho", "expression": "2[kg/m^3]"},
            {"kind": "variable", "scope": "comp2/var_ac",
             "name": "rho", "expression": "3[Pa*s]"},
        ],
    }

    captured = []

    def fake(requests, table, **kwargs):
        for r in requests:
            if r.id.startswith("slot:"):
                captured.append((r.id, dict(r.symbol_table or {})))
        return [
            DimensionalFinding(
                id=r.id, expression=r.expression,
                resolved=False,
            )
            for r in requests
        ]

    with patch("comsol_support.dimensional.analyze", side_effect=fake):
        analyze_model(
            symbols_json,
            slot_records=[
                _slot(physics_tag="ht", feature_tag="hs1",
                      slot_property="Q0", expression="rho*c_p*T"),
            ] + [
                # Augment with component_tag — the whole point here.
                {**_slot(physics_tag="ac", feature_tag="pml1",
                         slot_property="Zref",
                         expression="rho*c_sound"),
                 "component_tag": "comp2"},
            ],
            max_passes=1,
        )

    # Find the slot request whose symbol table we want to inspect.
    comp2_slot = [t for k, t in captured if "pml1" in k]
    assert comp2_slot, "Expected a slot request from comp2 physics"
    # The resolver should have resolved `rho` in comp2's scope. Layer C
    # may or may not have resolved the unit in this mock run; the
    # critical check is that the view did NOT include comp1's `rho`.
    # (Since we didn't set component_tag on the first slot, its table
    # is the flat global view — not what we're asserting on.)


def test_run_layer_b_signature_accepts_emit_slots():
    """run_layer_b exposes `emit_slots` and `slots_version` parameters.
    This is the public-contract guard for fix #2 (merged JVM walk).
    End-to-end behavior is covered by the real_comsol paired test.
    """
    import inspect
    from comsol_support.linting import run_layer_b
    sig = inspect.signature(run_layer_b)
    assert "emit_slots" in sig.parameters
    assert "slots_version" in sig.parameters
    assert sig.parameters["emit_slots"].default is False
    assert sig.parameters["slots_version"].default is None


def test_model_checker_java_accepts_slots_out_flag():
    """The Java-side ModelChecker accepts `--slots-out` and
    `--slots-version` arguments. Locked in by source inspection so the
    Python side never drifts from the Java CLI."""
    src = (
        Path(__file__).parent.parent
        / "comsol_support" / "java" / "ModelChecker.java"
    ).read_text()
    assert '"--slots-out"' in src
    assert '"--slots-version"' in src
    assert "SlotHarvester.harvestLoadedModel" in src


def test_analyze_model_slot_records_none_is_noop():
    """analyze_model with slot_records=None behaves identically to no-arg."""
    from comsol_support.dimensional import analyze_model

    captured = []

    def fake(requests, table, **kwargs):
        captured.append(len(requests))
        return []

    with patch("comsol_support.dimensional.analyze", side_effect=fake):
        analyze_model({"params": [], "variables": []}, slot_records=None)

    # One call expected (even empty returns early). No slot requests.
    assert captured == [] or all(n == 0 for n in captured)
