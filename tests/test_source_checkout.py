"""comsol-support is source-checkout-only: pin the detection and the
early, explicit failures an installed-from-wheel copy must produce.

The real installed-wheel smoke (build a wheel, install it into a clean
venv, run `comsol-support doctor` / `gotcha-search`) lives in
scripts/wheel_smoke.sh and runs in CI; these tests cover the same
contract in-process by pointing the checks at a synthetic tree.
"""

from pathlib import Path

import pytest

from comsol_support import _source_checkout as sc
from comsol_support.doctor import FAIL, OK, run_checks
from comsol_support.java_facade import JavaFacade


def _fake_tree(root: Path, *, complete: bool) -> Path:
    for rel in sc.REQUIRED_ASSETS:
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("x")
    if not complete:
        (root / "comsol_support" / "java" / "ModelExporter.java").unlink()
        (root / "docs" / "known-gotchas.md").unlink()
    return root


def test_this_checkout_is_complete():
    """The repository itself must satisfy its own asset list."""
    assert sc.missing_source_assets() == []
    assert sc.is_source_checkout()


def test_missing_assets_are_named(tmp_path):
    root = _fake_tree(tmp_path, complete=False)
    missing = sc.missing_source_assets(root)
    assert missing == [
        "comsol_support/java/ModelExporter.java",
        "docs/known-gotchas.md",
    ]
    with pytest.raises(sc.SourceCheckoutRequired) as e:
        sc.require_source_checkout("feature X", root)
    msg = str(e.value)
    assert "feature X" in msg
    assert "docs/known-gotchas.md" in msg
    assert "INSTALL.md" in msg


def test_complete_tree_passes(tmp_path):
    root = _fake_tree(tmp_path, complete=True)
    assert sc.missing_source_assets(root) == []
    sc.require_source_checkout("feature X", root)  # no raise


def test_doctor_reports_blocking_failure_from_wheel_layout(tmp_path, monkeypatch):
    """`doctor` must name the problem instead of printing "Ready"."""
    root = _fake_tree(tmp_path, complete=False)
    monkeypatch.setattr(sc, "SOURCE_ROOT", root)
    report = run_checks(comsol_path=str(tmp_path / "no-comsol"),
                        db_path=tmp_path / "no.db")
    check = next(c for c in report.checks if c.name == "Source checkout")
    assert check.status == FAIL and check.blocking
    assert "known-gotchas.md" in check.detail
    assert "INSTALL.md" in check.fix
    assert report.blocking  # -> `doctor` exits 1


def test_doctor_ok_on_real_checkout(tmp_path):
    report = run_checks(comsol_path=str(tmp_path / "no-comsol"),
                        db_path=tmp_path / "no.db")
    check = next(c for c in report.checks if c.name == "Source checkout")
    assert check.status == OK


def test_java_facade_refuses_wheel_layout(tmp_path, monkeypatch):
    """Without the java/ tree the facade raises up front, not from javac."""
    from comsol_support import java_facade as jf
    monkeypatch.setattr(jf, "__file__", str(tmp_path / "pkg" / "java_facade.py"))
    with pytest.raises(sc.SourceCheckoutRequired) as e:
        JavaFacade(comsol_path=str(tmp_path), workspace_dir=tmp_path / "ws")
    assert "Java facade" in str(e.value)


def test_java_facade_explicit_source_dir_is_not_guarded(tmp_path):
    """An explicit java_source_dir is the caller's responsibility (tests
    and harnesses pass their own); the guard only covers the default."""
    f = JavaFacade(comsol_path=str(tmp_path), workspace_dir=tmp_path / "ws",
                   java_source_dir=tmp_path / "elsewhere")
    assert f.java_source_dir == tmp_path / "elsewhere"
