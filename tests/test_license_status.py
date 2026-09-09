"""license-status: how the probe's JSON line becomes the CLI verdict.

The Java probe (`LicenseProbe.java`) is exercised for real only where
COMSOL is installed; these tests pin the Python contract around it with
the probe's output mocked — in particular the `no_seat` verdict added
Added when the probe was found to report a seat it did not have
(initStandalone succeeds without one; the seat is taken at model load).
"""

from __future__ import annotations

import argparse
import json
from unittest.mock import patch

import pytest

from comsol_support import license_status as ls
from comsol_support.java_facade import JavaExecutionError


def _run(monkeypatch, tmp_path, rc, out):
    with patch.object(ls.JavaFacade, "run_class", return_value=(rc, out)):
        return ls.license_status(comsol_path=str(tmp_path / "comsol"),
                                 workspace_dir=tmp_path / "ws", timeout_s=5)


def test_seat_available(monkeypatch, tmp_path):
    st = _run(monkeypatch, tmp_path, 0, 'SLF4J: noise\n{"available":true,"checkout_ms":2544}\n')
    assert st == {"available": True, "checkout_ms": 2544}


def test_every_seat_in_use_is_no_seat_not_available(monkeypatch, tmp_path):
    line = json.dumps({"available": False, "reason": "no_seat",
                       "error": "no free seat for COMSOL (ModelUtil.checkoutLicense returned false)",
                       "checkout_ms": 2500})
    st = _run(monkeypatch, tmp_path, 1, line + "\n")
    assert st["available"] is False
    assert st["reason"] == "no_seat"
    assert "checkoutLicense" in st["error"]


def test_blocked_probe_is_timeout(monkeypatch, tmp_path):
    with patch.object(ls.JavaFacade, "run_class", side_effect=JavaExecutionError("timed out")):
        st = ls.license_status(comsol_path=str(tmp_path / "comsol"),
                               workspace_dir=tmp_path / "ws", timeout_s=5)
    assert st == {"available": False, "reason": "timeout", "timeout_s": 5}


def test_probe_without_json_falls_back_to_exit_code(monkeypatch, tmp_path):
    st = _run(monkeypatch, tmp_path, 1, "garbage\n")
    assert st["available"] is False and st["reason"] == "no_probe_output"


@pytest.mark.parametrize("status, expected_exit", [
    ({"available": True, "checkout_ms": 1}, 0),
    ({"available": False, "reason": "no_seat", "error": "x"}, 2),
    ({"available": False, "reason": "timeout", "timeout_s": 5.0}, 2),
])
def test_cli_exit_code_follows_available(status, expected_exit, capsys):
    args = argparse.Namespace(comsol_path=None, workspace=None, timeout=5)
    with patch.object(ls, "license_status", return_value=status):
        rc = ls.cmd_license_status(args)
    assert rc == expected_exit
    assert json.loads(capsys.readouterr().out) == status
