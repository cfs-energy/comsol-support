"""Real-COMSOL integration tests for the slot expected-unit catalog.

Model-agnostic: picks the first corpus .mph that loads successfully
rather than hard-coding a specific benchmark. The paired-test contract
requires that catalog-driven auto-population produces
findings that are a SUPERSET of a baseline run with no catalog.

Gated behind `@pytest.mark.real_comsol` — runs only when COMSOL + its
license are available in the environment. CI without COMSOL skips
cleanly.
"""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.mark.real_comsol
def test_paired_catalog_vs_baseline_is_superset(tmp_path):
    """Phase 3 paired-test contract on a real corpus model.

    The contract (model-agnostic form): when a catalog
    auto-populates `expected_overrides`, the findings reported by
    Layer C must be a SUPERSET of what an empty-overrides baseline
    reports. The catalog should never SUPPRESS findings that the
    baseline would have produced.

    This mirrors the synthetic `test_catalog_overrides_are_superset_of_manual_overrides`
    test but uses an actual COMSOL model and a real catalog populated
    by the Phase 1+2 pipeline on that same model. Any model in the
    corpus works — we pick whichever loads cleanly first.
    """
    from comsol_support import COMSOL_PATH
    from comsol_support.db import init_db
    from comsol_support.dimensional import analyze_model
    from comsol_support.linting import run_layer_b
    from comsol_support.slot_catalog import (
        aggregate_dump, build_expected_overrides_from_slots,
    )
    from comsol_support.slot_harvest import run_harvest

    applications = Path(COMSOL_PATH) / "applications"
    candidates = sorted(applications.rglob("*.mph"))
    if not candidates:
        pytest.skip("No corpus .mph files found")

    # Phase 1: harvest a handful of models into a dump.
    dump = tmp_path / "dump.jsonl"
    workspace = tmp_path / "ws"
    report = run_harvest(
        candidates[:6], dump,
        comsol_path=COMSOL_PATH,
        workspace_dir=workspace,
        timeout_s=600,
        progress=False,
        version="6.4",
    )
    if report.models_processed == 0:
        pytest.skip(
            "No corpus models loaded successfully — COMSOL environment "
            "cannot produce a meaningful catalog for this test"
        )

    # Phase 2: aggregate into a fresh catalog db.
    db_path = tmp_path / "catalog.db"
    conn = init_db(db_path)
    aggregate_dump(dump, conn)
    catalog_total = conn.execute(
        "SELECT COUNT(*) FROM slot_expected_units"
    ).fetchone()[0]

    if catalog_total == 0:
        pytest.skip(
            "Catalog aggregation produced no keys from this harvest"
        )

    # Pick a model that loaded successfully for the paired check.
    model_path = None
    from comsol_support.slot_harvest import read_dump
    for rec in read_dump(dump):
        if rec.get("kind") == "slot":
            model_path = Path(rec["model_path"])
            break
    if model_path is None or not model_path.exists():
        pytest.skip("No usable model from harvest for paired test")

    # Run ModelChecker with --slots-out to get symbols + slots in
    # one JVM session (exercises fix #2).
    result = run_layer_b(
        model_path,
        comsol_path=str(COMSOL_PATH),
        workspace_dir=workspace,
        timeout_s=600,
        emit_slots=True,
        slots_version="6.4",
    )
    assert len(result) == 4, "run_layer_b(emit_slots=True) must return 4-tuple"
    units_b, descr_b, symbols_b, slot_records = result

    # Baseline: run analyze_model with NO catalog overrides. (Layer C
    # has been pure Python — no Wolfram gate needed.)
    baseline = analyze_model(
        symbols_b,
        expected_overrides=None,
        slot_records=slot_records,
        timeout_s=120,
    )
    baseline_ids = {f.id for f in baseline}

    # Catalog-driven: populate overrides from the db we just built.
    expected_overrides, annotations = build_expected_overrides_from_slots(
        slot_records, conn, comsol_version="6.4",
    )
    catalog_driven = analyze_model(
        symbols_b,
        expected_overrides=expected_overrides or None,
        slot_records=slot_records,
        timeout_s=120,
    )
    catalog_ids = {f.id for f in catalog_driven}

    # Superset check. The catalog path may produce ADDITIONAL findings
    # (expected/deduced mismatches surfaced by CompatibleUnitQ) but
    # must never drop any baseline finding.
    missing = baseline_ids - catalog_ids
    assert not missing, (
        f"Catalog-driven run dropped {len(missing)} findings the "
        f"baseline produced: {sorted(missing)[:5]}"
    )

    conn.close()
