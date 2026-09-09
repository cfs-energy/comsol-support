"""Tests for partial-state detection in `comsol-support check`.

Pure-Python: exercises the classifier on path/sidecar inputs and the
CLI skip-on-partial behavior. No COMSOL JVM involved.
"""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

from comsol_support.cli import build_parser
from comsol_support.linting import classify_mph_state, cmd_check


# ---- Classifier ------------------------------------------------------------


@pytest.mark.parametrize("name, expected", [
    ("model.mph", "complete"),
    ("model.partial.mph", "partial"),
    ("model.pre_solve.mph", "pre_solve"),
    ("MyBuilder_unsolved.mph", "pre_solve"),
    ("some_solved_output.mph", "complete"),
])
def test_classify_by_filename(tmp_path, name, expected):
    p = tmp_path / name
    p.write_bytes(b"")
    assert classify_mph_state(p) == expected


def test_classify_from_mphgen_sidecar(tmp_path):
    """If a co-located .mphgen.json reports solved=false, that's pre_solve."""
    p = tmp_path / "model.mph"
    p.write_bytes(b"")
    sidecar = tmp_path / "model.mph.mphgen.json"
    sidecar.write_text(json.dumps({"solved": False, "study": None}))
    assert classify_mph_state(p) == "pre_solve"


def test_classify_solved_sidecar_does_not_override(tmp_path):
    """solved=true sidecar → complete (the default)."""
    p = tmp_path / "model.mph"
    p.write_bytes(b"")
    (tmp_path / "model.mph.mphgen.json").write_text(
        json.dumps({"solved": True, "study": "std1"}))
    assert classify_mph_state(p) == "complete"


def test_classify_partial_suffix_wins_over_sidecar(tmp_path):
    """Filename suffix is the most authoritative signal."""
    p = tmp_path / "model.partial.mph"
    p.write_bytes(b"")
    (tmp_path / "model.partial.mph.mphgen.json").write_text(
        json.dumps({"solved": True}))
    assert classify_mph_state(p) == "partial"


def test_classify_malformed_sidecar_falls_back_to_complete(tmp_path):
    p = tmp_path / "model.mph"
    p.write_bytes(b"")
    (tmp_path / "model.mph.mphgen.json").write_text("{not json")
    # No raise — just falls through to "complete".
    assert classify_mph_state(p) == "complete"


# ---- CLI behavior ---------------------------------------------------------


def _make_valid_mph_zip(path: Path) -> None:
    """Produce a .mph that passes `validate_mph` (zip with required members,
    > 1024 bytes). Path may include `.partial.mph` or other suffixes."""
    bulk = "x" * 2048
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("dmodel.xml", "<dummy/>" + bulk)
        zf.writestr("model.xml", "<dummy/>" + bulk)
        zf.writestr("fileversion", "1")


def test_cmd_check_skips_partial_by_default(tmp_path, capsys):
    """Default behavior: partial .mph → message printed, exit 0, no Layer B
    invocation. The Layer-B side won't even be attempted (we verify via
    the absence of a layer-b-error exit code: cmd_check returns 0 cleanly
    without any JVM activity, even when no COMSOL is configured)."""
    p = tmp_path / "model.partial.mph"
    _make_valid_mph_zip(p)

    parser = build_parser()
    args = parser.parse_args(["check", str(p)])
    rc = cmd_check(args)
    out = capsys.readouterr().out
    assert rc == 0
    assert "partial-state" in out
    assert "--allow-partial" in out


def test_cmd_check_pre_solve_does_not_skip(tmp_path, monkeypatch, capsys):
    """pre_solve is the common mphgen-default state — must not be skipped.

    We don't have COMSOL in the test environment, so a real run would
    fail at run_layer_b. Stub run_layer_b to a known shape and verify the
    sidecar gets `model_state: "pre_solve"`.
    """
    p = tmp_path / "model_unsolved.mph"
    _make_valid_mph_zip(p)

    # Stub run_layer_b to return empty-but-valid Layer B sidecars.
    import comsol_support.linting as lint_mod
    monkeypatch.setattr(lint_mod, "run_layer_b",
                        lambda *a, **kw: ({"layer_b": {"errors": []}},
                                          {"layer_b": {"missing_runtime": [],
                                                       "placeholder": []}},
                                          {"params": [], "variables": []}))

    parser = build_parser()
    args = parser.parse_args(["check", str(p), "--skip-layer-c"])
    rc = cmd_check(args)
    out = capsys.readouterr().out
    assert rc == 0
    # Sidecar should be annotated.
    units_sidecar = p.with_suffix(".mph.units.json")
    assert units_sidecar.exists()
    data = json.loads(units_sidecar.read_text())
    assert data.get("model_state") == "pre_solve"
    # Stdout summary should include the state tag.
    assert "[pre_solve]" in out


def test_cmd_check_allow_partial_runs_anyway(tmp_path, monkeypatch, capsys):
    """--allow-partial bypasses the default skip and runs Layer B."""
    p = tmp_path / "model.partial.mph"
    _make_valid_mph_zip(p)

    import comsol_support.linting as lint_mod
    monkeypatch.setattr(lint_mod, "run_layer_b",
                        lambda *a, **kw: ({"layer_b": {"errors": []}},
                                          {"layer_b": {"missing_runtime": [],
                                                       "placeholder": []}},
                                          {"params": [], "variables": []}))

    parser = build_parser()
    args = parser.parse_args(["check", str(p), "--allow-partial",
                              "--skip-layer-c"])
    rc = cmd_check(args)
    out = capsys.readouterr().out
    assert rc == 0
    assert "partial-state" not in out  # the skip message was NOT printed
    assert "[partial]" in out  # but the state-tag IS in the summary

    sidecar = p.with_suffix(".mph.units.json")
    data = json.loads(sidecar.read_text())
    assert data.get("model_state") == "partial"


def test_cmd_check_complete_mph_unaffected(tmp_path, monkeypatch, capsys):
    """Default-named .mph with no sidecar → 'complete', no state tag,
    no model_state field on sidecars."""
    p = tmp_path / "regular.mph"
    _make_valid_mph_zip(p)

    import comsol_support.linting as lint_mod
    monkeypatch.setattr(lint_mod, "run_layer_b",
                        lambda *a, **kw: ({"layer_b": {"errors": []}},
                                          {"layer_b": {"missing_runtime": [],
                                                       "placeholder": []}},
                                          {"params": [], "variables": []}))

    parser = build_parser()
    args = parser.parse_args(["check", str(p), "--skip-layer-c"])
    rc = cmd_check(args)
    out = capsys.readouterr().out
    assert rc == 0
    assert "[partial]" not in out
    assert "[pre_solve]" not in out
    sidecar = p.with_suffix(".mph.units.json")
    data = json.loads(sidecar.read_text())
    # complete → no annotation added
    assert "model_state" not in data
