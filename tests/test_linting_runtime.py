"""Runtime tests for Layer B (ModelChecker.java).

Gated by COMSOL_E2E=1 because Layer B requires a running COMSOL JVM.
Uses an external reference .mph as the canonical integration fixture.
"""

import json
import os
from pathlib import Path

import pytest

from comsol_support import COMSOL_PATH
from comsol_support.linting import (
    LayerBError,
    run_layer_b,
    summarise_sidecars,
    write_sidecars,
)


# The reference model is an external asset, not a repo fixture: point
# COMSOL_REFERENCE_MPH at a solved .mph to enable these tests; without
# it they skip (see gaps.md, "Some tests need a local reference model").
_REFERENCE_MPH_ENV = os.environ.get("COMSOL_REFERENCE_MPH", "")
REFERENCE_MPH = Path(_REFERENCE_MPH_ENV) if _REFERENCE_MPH_ENV else None


pytestmark = [
    pytest.mark.real_comsol,
    pytest.mark.skipif(
        os.environ.get("COMSOL_E2E", "0") != "1",
        reason="Set COMSOL_E2E=1 to run the real-COMSOL Layer B tests",
    ),
]


@pytest.fixture(scope="module")
def reference_mph():
    if not Path(COMSOL_PATH).is_dir():
        pytest.skip(f"COMSOL not installed at {COMSOL_PATH}")
    # exists() raises rather than returning False when the asset lives
    # under a home directory this user cannot traverse.
    try:
        present = REFERENCE_MPH is not None and REFERENCE_MPH.exists()
    except OSError:
        present = False
    if not present:
        pytest.skip(
            "reference model not available (set COMSOL_REFERENCE_MPH)"
        )
    return REFERENCE_MPH


def test_layer_b_runs_on_reference_and_emits_valid_schema(
    reference_mph, tmp_path,
):
    units, descrs, symbols = run_layer_b(
        reference_mph, workspace_dir=tmp_path, timeout_s=300,
    )
    # Symbols sidecar — smoke check that Layer C input exists.
    assert "params" in symbols
    assert "variables" in symbols
    assert len(symbols["params"]) >= 10

    # Schema shape
    assert "layer_b" in units and "errors" in units["layer_b"]
    assert "layer_b" in descrs
    assert "missing_runtime" in descrs["layer_b"]
    assert "placeholder" in descrs["layer_b"]

    # The reference builder gives no descriptions, so its parameters and
    # variables must all appear in missing_runtime. Exact counts vary by
    # model, so assert a lower bound only.
    missing = descrs["layer_b"]["missing_runtime"]
    assert len(missing) >= 10, f"expected ≥10 missing, got {len(missing)}"
    assert any(m["kind"] == "param" for m in missing)
    assert any(m["kind"] == "variable" for m in missing)

    # Structural, not model-specific: parameters report by name.
    names = {m["name"] for m in missing if m["kind"] == "param"}
    assert len(names) >= 4
    assert all(isinstance(n, str) and n for n in names)

    # provenance
    assert "model_sha256" in units
    assert len(units["model_sha256"]) == 64


def test_write_sidecars_layer_b_only_produces_stable_schema(
    reference_mph, tmp_path,
):
    units_b, descr_b, _ = run_layer_b(reference_mph, workspace_dir=tmp_path)

    # Write sidecars to a scratch location (don't touch the real .mph's
    # neighbors in cross-comparisons).
    scratch_mph = tmp_path / "reference_scratch.mph"
    scratch_mph.write_bytes(b"fake")  # only the path is used for naming
    units_path, descr_path = write_sidecars(
        scratch_mph, layer_b_units=units_b, layer_b_descriptions=descr_b,
    )
    assert units_path.exists()
    assert descr_path.exists()

    units_doc = json.loads(units_path.read_text())
    descr_doc = json.loads(descr_path.read_text())

    # layer_a and layer_b must both appear even when one layer was not
    # run — the sidecar schema is stable.
    for doc in (units_doc, descr_doc):
        assert "layer_a" in doc
        assert "layer_b" in doc
        assert "generated_at" in doc


def test_layer_b_rejects_missing_mph():
    with pytest.raises(LayerBError):
        run_layer_b(Path("/tmp/does_not_exist.mph"))


def test_layer_c_detects_w1_w2_w3(reference_mph, tmp_path):
    """The reference model's three documented GUI warnings via Layer C.

    W1 — non-integer exponent on rho_val in var_b (intrinsic, no
    overrides needed). W2/W3 — physics-slot expected-unit mismatches
    (caller supplies the PDE-form expected units).
    """
    from comsol_support.dimensional import analyze_model, findings_to_sidecar

    _, _, symbols = run_layer_b(reference_mph, workspace_dir=tmp_path)
    overrides = {
        "variable:comp1/var_post:Ez_val": "1/m",  # Ga slot expectation
        "param:param:mu0_val": "s/m^2",           # da slot expectation
    }
    findings = analyze_model(symbols, overrides)
    sc = findings_to_sidecar(findings)

    w1 = any("rho_val" in w["id"] and w["kind"] == "non_integer_power"
             for w in sc["warnings"])
    w2 = any("Ez_val" in w["id"] and w["kind"] == "expected_mismatch"
             for w in sc["warnings"])
    w3 = any("mu0_val" in w["id"] and w["kind"] == "expected_mismatch"
             for w in sc["warnings"])
    assert w1, "W1 (non-integer exponent on rho_val) not detected"
    assert w2, "W2 (Ez_val vs 1/m) not detected"
    assert w3, "W3 (mu0_val vs s/m^2) not detected"


def test_summary_counts_from_layer_b_only(reference_mph, tmp_path):
    units_b, descr_b, _ = run_layer_b(reference_mph, workspace_dir=tmp_path)
    s = summarise_sidecars(None, units_b, descr_b)
    # All keys present, integer counts.
    for k in ("units_warnings", "units_errors",
              "descr_missing", "descr_placeholder"):
        assert isinstance(s[k], int)
    # No param unit errors expected.
    assert s["units_errors"] == 0
    # Many missing descriptions expected.
    assert s["descr_missing"] >= 10
