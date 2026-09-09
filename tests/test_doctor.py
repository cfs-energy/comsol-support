"""Tests for the cross-platform install preflight (comsol_support/doctor.py).

The installers on all three platforms delegate their environment checks
here, so these tests are the guard against per-OS drift. Everything is
built from a synthetic COMSOL tree under tmp_path — no real install, no
license, no network — so the suite behaves identically on Linux, macOS,
and Windows.
"""

from __future__ import annotations

import json
import os
import sqlite3

import pytest

from comsol_support import doctor
from comsol_support.doctor import FAIL, OK, WARN, render, run_checks
from comsol_support.java_facade import _platform_subdir


def _exe_names(tool: str) -> list[str]:
    """Spellings find_java_executable() probes on this platform."""
    return [f"{tool}.exe", tool] if os.name == "nt" else [tool]


@pytest.fixture
def fake_comsol(tmp_path):
    """A structurally complete COMSOL 6.4 install."""
    root = tmp_path / "comsol64" / "multiphysics"
    plat = _platform_subdir(root)

    (root / "plugins").mkdir(parents=True)
    for name in ("com.comsol.api_1.0.0.jar", "com.comsol.model_6.4.0.jar"):
        (root / "plugins" / name).touch()

    (root / "lib" / plat).mkdir(parents=True)
    (root / "ext" / "cadimport" / plat).mkdir(parents=True)

    jdk_bin = root / "java" / plat / "jre" / "bin"
    jdk_bin.mkdir(parents=True)
    for tool in ("java", "javac"):
        for name in _exe_names(tool):
            (jdk_bin / name).touch()

    (root / "about.txt").write_text(
        "SOFTWARE COMPONENTS IN COMSOL 6.4\n", encoding="utf-8")

    api = (root / "doc" / "help" / "wtpwebapps" / "ROOT" / "doc"
           / "com.comsol.help.comsol" / "api")
    api.mkdir(parents=True)
    (api / "index.html").write_text("<html/>", encoding="utf-8")

    pdf = root / "doc" / "pdf" / "COMSOL_Multiphysics"
    pdf.mkdir(parents=True)
    (pdf / "COMSOL_ProgrammingReferenceManual.pdf").write_bytes(b"%PDF-1.4")

    apps = root / "applications" / "Heat_Transfer_Module"
    apps.mkdir(parents=True)
    (apps / "full_model.mph").write_bytes(b"x" * 200_000)
    (apps / "preview_stub.mph").write_bytes(b"x" * 9_000)
    return root


def _by_name(report, name):
    for c in report.checks:
        if c.name == name:
            return c
    raise AssertionError(f"no check named {name!r}: "
                         f"{[c.name for c in report.checks]}")


# ---- happy path ------------------------------------------------------

def test_complete_install_has_no_blocking_problems(fake_comsol, tmp_path):
    report = run_checks(comsol_path=str(fake_comsol),
                        db_path=tmp_path / "absent.db")
    assert report.blocking == [], [c.as_dict() for c in report.blocking]
    assert _by_name(report, "COMSOL").status == OK
    assert _by_name(report, "Native libraries").status == OK
    assert _by_name(report, "Java (JDK)").status == OK
    assert _by_name(report, "COMSOL plugin JARs").status == OK


def test_reports_version_and_preview_stubs(fake_comsol, tmp_path):
    report = run_checks(comsol_path=str(fake_comsol),
                        db_path=tmp_path / "absent.db")
    assert "6.4" in _by_name(report, "COMSOL").detail
    models = _by_name(report, "Application models")
    # Both counted, stubs called out — they are the reason `scrape
    # corpus` legitimately skips models.
    assert "2 .mph" in models.detail and "1 preview stub" in models.detail


# ---- blocking failures ----------------------------------------------

def test_missing_comsol_blocks_and_skips_dependent_checks(tmp_path):
    report = run_checks(comsol_path=str(tmp_path / "nope"),
                        db_path=tmp_path / "absent.db")
    comsol = _by_name(report, "COMSOL 6.4")
    assert comsol.status == FAIL and comsol.blocking
    assert comsol.fix, "a blocking failure must carry a remedy"
    # Checks that need a COMSOL root must not fire misleading failures.
    assert not any(c.name == "COMSOL plugin JARs" for c in report.checks)


def test_missing_jars_and_libs_block(fake_comsol, tmp_path):
    for jar in (fake_comsol / "plugins").iterdir():
        jar.unlink()
    plat = _platform_subdir(fake_comsol)
    (fake_comsol / "lib" / plat).rmdir()

    report = run_checks(comsol_path=str(fake_comsol),
                        db_path=tmp_path / "absent.db")
    assert _by_name(report, "COMSOL plugin JARs").blocking
    assert _by_name(report, "Native libraries").blocking
    assert len(report.blocking) == 2


def test_missing_jdk_blocks(fake_comsol, tmp_path, monkeypatch):
    # Empty the bundled JDK and hide any system one.
    jdk_bin = fake_comsol / "java" / _platform_subdir(fake_comsol) / "jre" / "bin"
    for f in jdk_bin.iterdir():
        f.unlink()
    monkeypatch.setattr(doctor.shutil, "which", lambda *_a, **_k: None)
    monkeypatch.delenv("JAVA_HOME", raising=False)

    report = run_checks(comsol_path=str(fake_comsol),
                        db_path=tmp_path / "absent.db")
    assert _by_name(report, "Java (JDK)").blocking


# ---- optional assets warn, never block -------------------------------

def test_missing_docs_warn_but_do_not_block(fake_comsol, tmp_path):
    import shutil as _sh
    _sh.rmtree(fake_comsol / "doc")
    _sh.rmtree(fake_comsol / "applications")

    report = run_checks(comsol_path=str(fake_comsol),
                        db_path=tmp_path / "absent.db")
    assert report.blocking == []
    assert _by_name(report, "Javadoc tree").status == WARN
    assert _by_name(report, "Reference Manual PDF").status == WARN
    assert _by_name(report, "Application models").status == WARN


# ---- knowledge base --------------------------------------------------

def test_absent_knowledge_base_warns_and_is_not_created(fake_comsol, tmp_path):
    db = tmp_path / "not_yet.db"
    report = run_checks(comsol_path=str(fake_comsol), db_path=db)
    kb = _by_name(report, "Knowledge base")
    assert kb.status == WARN and "scrape javadoc" in kb.fix
    # A diagnostic must never create the thing it is diagnosing: an
    # accidentally-created empty DB is exactly the silent failure mode
    # that makes later commands look broken.
    assert not db.exists()


def test_populated_knowledge_base_reports_counts(fake_comsol, tmp_path):
    db = tmp_path / "comsol.db"
    conn = sqlite3.connect(db)
    for table in ("knowledge", "fragments", "slot_expected_units",
                  "native_interfaces"):
        conn.execute(f"CREATE TABLE {table} (id INTEGER)")
    conn.execute("INSERT INTO knowledge VALUES (1)")
    conn.execute("INSERT INTO native_interfaces VALUES (1)")
    conn.commit()
    conn.close()

    report = run_checks(comsol_path=str(fake_comsol), db_path=db)
    kb = _by_name(report, "Knowledge base")
    assert "knowledge 1" in kb.detail
    # Ontology present but corpus/slots empty -> actionable warning.
    assert kb.status == WARN
    assert "scrape corpus" in kb.fix and "scrape slots" in kb.fix


# ---- CLI surface -----------------------------------------------------

def test_exit_codes(fake_comsol, tmp_path, capsys):
    import argparse

    ok_args = argparse.Namespace(comsol_path=str(fake_comsol),
                                 db=str(tmp_path / "absent.db"),
                                 json=False, strict=False)
    assert doctor.cmd_doctor(ok_args) == 0

    # --strict turns the optional-asset warnings into a non-zero exit.
    strict_args = argparse.Namespace(comsol_path=str(fake_comsol),
                                     db=str(tmp_path / "absent.db"),
                                     json=False, strict=True)
    assert doctor.cmd_doctor(strict_args) == 1

    bad_args = argparse.Namespace(comsol_path=str(tmp_path / "nope"),
                                  db=str(tmp_path / "absent.db"),
                                  json=False, strict=False)
    assert doctor.cmd_doctor(bad_args) == 1
    capsys.readouterr()


def test_json_output_is_machine_readable(fake_comsol, tmp_path, capsys):
    import argparse

    args = argparse.Namespace(comsol_path=str(fake_comsol),
                              db=str(tmp_path / "absent.db"),
                              json=True, strict=False)
    doctor.cmd_doctor(args)
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["blocking"] == 0
    assert {"name", "status", "detail", "fix", "blocking"} <= set(
        payload["checks"][0])


def test_render_is_ascii_only(fake_comsol, tmp_path):
    """Legacy Windows consoles are cp1252; the first command a new user
    runs must not print replacement characters."""
    report = run_checks(comsol_path=str(fake_comsol),
                        db_path=tmp_path / "absent.db")
    text = render(report, color=False)
    assert text.isascii(), [ln for ln in text.splitlines()
                            if not ln.isascii()]


def test_doctor_is_registered_as_a_subcommand():
    from comsol_support.cli import build_parser

    args = build_parser().parse_args(["doctor", "--json"])
    assert args.command == "doctor" and args.json is True
