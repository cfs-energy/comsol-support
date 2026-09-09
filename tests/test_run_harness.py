"""Tests for run_harness — CLI adapter behavior.

The compile/run mechanics live in JavaFacade (covered by
test_lint_selftest's compile sweep and the facade tests); these tests
pin the harness-level evidence-discipline contract added after a
post-campaign audit: an EMPTY output log must be
announced loudly, never left to be misread as a measurement (the
campaign's fabricated-OOM narrative was built on a 0-byte log —
audit finding F-T2 / lesson L-1).
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import comsol_support.run_harness as rh


def _args(log_path: Path) -> argparse.Namespace:
    return argparse.Namespace(
        harness="Whatever.java", harness_args=[],
        comsol_path=None, workspace=None,
        log=str(log_path), timeout=None, quiet=True, jvm_arg=[],
    )


def test_cmd_run_harness_warns_on_empty_log(tmp_path, capsys, monkeypatch):
    log = tmp_path / "h.harness.log"
    log.touch()  # 0 bytes — process emitted nothing
    monkeypatch.setattr(rh, "run_harness", lambda **kw: (0, "", log))
    rc = rh.cmd_run_harness(_args(log))
    captured = capsys.readouterr()
    assert rc == 0
    assert "EMPTY" in captured.err
    assert "Do not cite absence of output" in captured.err
    payload = json.loads(captured.out)
    assert payload["log_bytes"] == 0


def test_cmd_run_harness_no_warning_when_log_has_content(
        tmp_path, capsys, monkeypatch):
    log = tmp_path / "h.harness.log"
    log.write_text("JVM said something\n")
    monkeypatch.setattr(
        rh, "run_harness", lambda **kw: (0, "JVM said something\n", log))
    rc = rh.cmd_run_harness(_args(log))
    captured = capsys.readouterr()
    assert rc == 0
    assert "EMPTY" not in captured.err
    payload = json.loads(captured.out)
    assert payload["log_bytes"] == log.stat().st_size


def test_cmd_run_harness_warns_when_log_missing(tmp_path, capsys, monkeypatch):
    log = tmp_path / "never_created.log"  # does not exist
    monkeypatch.setattr(rh, "run_harness", lambda **kw: (1, "", log))
    rc = rh.cmd_run_harness(_args(log))
    captured = capsys.readouterr()
    assert rc == 1
    assert "EMPTY" in captured.err
