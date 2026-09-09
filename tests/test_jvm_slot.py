"""Tests for jvm_slot — advisory exclusive-JVM slot + leak detection.

flock locks are per-open-file-description, and Windows byte-range locks
are per-handle, so two open()+lock attempts on the same path CONFLICT
even within one process — which makes contention testable without
subprocesses on both platforms.
"""

from __future__ import annotations

import sys
import json
import os
from pathlib import Path

import pytest

from comsol_support.jvm_slot import (
    JvmSlotTimeout,
    acquire_jvm_slot,
    scan_leaked_jvms,
    warn_leaked_jvms,
)

posix_only = pytest.mark.skipif(
    os.name not in ("posix", "nt"),
    reason="slot locking needs flock (POSIX) or msvcrt (Windows)",
)


@posix_only
def test_acquire_release_roundtrip(tmp_path):
    lock = tmp_path / "slot.lock"
    slot = acquire_jvm_slot("test purpose", lock_path=lock, enforce=False)
    assert slot.acquired is True
    # Holder info recorded for contenders.
    info = json.loads(lock.read_text().strip())
    assert info["pid"] == os.getpid()
    assert info["purpose"] == "test purpose"
    slot.release()
    # Released: a fresh acquire succeeds.
    slot2 = acquire_jvm_slot("again", lock_path=lock, enforce=False)
    assert slot2.acquired is True
    slot2.release()


@posix_only
def test_contention_warn_mode_proceeds_with_holder_info(tmp_path):
    lock = tmp_path / "slot.lock"
    first = acquire_jvm_slot("first jvm", lock_path=lock, enforce=False)
    assert first.acquired
    second = acquire_jvm_slot("second jvm", lock_path=lock, enforce=False)
    # Advisory default: proceed, but say so and name the holder.
    assert second.acquired is False
    assert second.holder is not None
    assert second.holder["purpose"] == "first jvm"
    second.release()  # idempotent no-op on a non-acquired slot
    first.release()


@posix_only
def test_contention_enforce_mode_times_out(tmp_path):
    lock = tmp_path / "slot.lock"
    first = acquire_jvm_slot("holder", lock_path=lock, enforce=False)
    try:
        with pytest.raises(JvmSlotTimeout, match="holder"):
            acquire_jvm_slot("waiter", lock_path=lock,
                             enforce=True, timeout_s=0.1)
    finally:
        first.release()


@posix_only
def test_slot_event_payload_shape(tmp_path):
    lock = tmp_path / "slot.lock"
    with acquire_jvm_slot("payload test", lock_path=lock,
                          enforce=False) as slot:
        payload = slot.as_event_payload()
        assert payload["acquired"] is True
        assert payload["purpose"] == "payload test"
        assert "waited_ms" in payload


def _fake_proc(root: Path, pid: int, argv: list[str],
               rss_kb: int | None = 1024) -> None:
    d = root / str(pid)
    d.mkdir()
    (d / "cmdline").write_bytes(b"\0".join(a.encode() for a in argv) + b"\0")
    if rss_kb is not None:
        (d / "status").write_text(f"Name:\tjava\nVmRSS:\t{rss_kb} kB\n")


def test_scan_leaked_jvms_detects_comsol_java_only(tmp_path):
    # A COMSOL JVM (matches), a plain java process (no marker), and a
    # non-java process (ignored).
    _fake_proc(tmp_path, 100, ["/opt/jre/bin/java", "-cp",
                               "/opt/comsol64/plugins/x.jar",
                               "ModelExporter"], rss_kb=3_000_000)
    _fake_proc(tmp_path, 101, ["/usr/bin/java", "-jar", "unrelated.jar"])
    _fake_proc(tmp_path, 102, ["/usr/bin/python3", "com.comsol-lookalike"])
    (tmp_path / "not-a-pid").mkdir()

    leaks = scan_leaked_jvms(proc_root=tmp_path)
    assert [x["pid"] for x in leaks] == [100]
    assert leaks[0]["rss_bytes"] == 3_000_000 * 1024
    assert "ModelExporter" in leaks[0]["cmdline_head"]


def test_scan_leaked_jvms_excludes_pids_and_missing_root(tmp_path):
    _fake_proc(tmp_path, 200, ["/opt/jre/bin/java", "com.comsol.Foo"])
    assert scan_leaked_jvms(proc_root=tmp_path, exclude_pids={200}) == []
    assert scan_leaked_jvms(proc_root=tmp_path / "nope") == []


def _fake_ps_result(stdout: str):
    class R:
        pass

    r = R()
    r.stdout = stdout
    return r


def test_windows_scan_filters_markers_and_excludes(monkeypatch):
    """CIM-path sweep: marker filtering (case-insensitive), exclusion
    list, and field mapping — platform-independent via mocked
    PowerShell output."""
    import comsol_support.jvm_slot as js

    payload = json.dumps([
        {"pid": 100,
         "cmd": r'"C:\Program Files\COMSOL\COMSOL64\...\java.exe" '
                r'-cp "C:\...\plugins\*" ModelExporter run',
         "rss": 3_000_000_000, "age_s": 42.5},
        {"pid": 101, "cmd": r'java.exe -jar unrelated.jar',
         "rss": 1024, "age_s": 1.0},
        {"pid": 102,
         "cmd": r'java.exe -cp "C:\PROGRA~1\COMSOL\x" ModelExporter --input x.mph',
         "rss": 2048, "age_s": 2.0},
    ])
    monkeypatch.setattr(js.subprocess, "run",
                        lambda *a, **k: _fake_ps_result(payload))
    leaks = js._scan_leaked_jvms_windows(exclude=set())
    assert [x["pid"] for x in leaks] == [100, 102]
    assert leaks[0]["rss_bytes"] == 3_000_000_000
    assert leaks[0]["age_s"] == 42.5
    assert "ModelExporter" in leaks[0]["cmdline_head"]

    leaks = js._scan_leaked_jvms_windows(exclude={100})
    assert [x["pid"] for x in leaks] == [102]


def test_windows_scan_single_object_and_malformed(monkeypatch):
    """A single java.exe serializes as a bare JSON object; malformed
    or empty output degrades to []."""
    import comsol_support.jvm_slot as js

    single = json.dumps(
        {"pid": 55, "cmd": "java.exe -cp x com.comsol.Foo",
         "rss": 10, "age_s": 3.0})
    monkeypatch.setattr(js.subprocess, "run",
                        lambda *a, **k: _fake_ps_result(single))
    assert [x["pid"] for x in js._scan_leaked_jvms_windows(set())] == [55]

    monkeypatch.setattr(js.subprocess, "run",
                        lambda *a, **k: _fake_ps_result("not json {"))
    assert js._scan_leaked_jvms_windows(set()) == []

    monkeypatch.setattr(js.subprocess, "run",
                        lambda *a, **k: _fake_ps_result(""))
    assert js._scan_leaked_jvms_windows(set()) == []

    def _boom(*a, **k):
        raise OSError("powershell.exe not found")

    monkeypatch.setattr(js.subprocess, "run", _boom)
    assert js._scan_leaked_jvms_windows(set()) == []


def test_macos_ps_scan_filters_markers_and_excludes(monkeypatch):
    """ps-path sweep (macOS): marker filtering, exclusion list, field
    mapping, and etime parsing — platform-independent via mocked ps
    output."""
    import comsol_support.jvm_slot as js

    ps_out = "\n".join([
        # pid  etime        rss(KiB)  args
        " 100  2-03:04:05   2929687   /Applications/COMSOL64/Multiphysics"
        "/java/macarm64/jre/Contents/Home/bin/java -cp plugins/* "
        "ModelExporter run",
        " 101       05:00      1024   /usr/bin/java -jar unrelated.jar",
        " 102    01:02:03         2   java -cp x ModelExporter --input x.mph",
        " 103       00:10       512   /usr/sbin/mdworker com.comsol.Foo",
        # Marker only via the UPPERCASE install path — the live-leak
        # shape on macOS (plugins/* wildcard, non-marker main class).
        " 104       00:42      4096   java -cp ws:/Applications/COMSOL64"
        "/Multiphysics/plugins/* SlotHarvester run",
        "garbage line",
    ])
    monkeypatch.setattr(js.subprocess, "run",
                        lambda *a, **k: _fake_ps_result(ps_out))
    leaks = js._scan_leaked_jvms_ps(exclude=set())
    assert [x["pid"] for x in leaks] == [100, 102, 104]
    assert leaks[0]["rss_bytes"] == 2929687 * 1024
    assert leaks[0]["age_s"] == 2 * 86400 + 3 * 3600 + 4 * 60 + 5
    assert leaks[1]["age_s"] == 1 * 3600 + 2 * 60 + 3
    assert "ModelExporter" in leaks[0]["cmdline_head"]

    leaks = js._scan_leaked_jvms_ps(exclude={100})
    assert [x["pid"] for x in leaks] == [102, 104]

    def _boom(*a, **k):
        raise OSError("ps not found")

    monkeypatch.setattr(js.subprocess, "run", _boom)
    assert js._scan_leaked_jvms_ps(set()) == []


def test_parse_etime_forms():
    from comsol_support.jvm_slot import _parse_etime
    assert _parse_etime("00:05") == 5.0
    assert _parse_etime("01:02:03") == 3723.0
    assert _parse_etime("2-00:00:01") == 172801.0
    assert _parse_etime("junk") == -1.0


def test_scan_dispatches_to_ps_on_darwin_default_root(monkeypatch):
    """Default proc_root on macOS uses the ps path; an explicit
    proc_root always uses the /proc-style tree scan."""
    import comsol_support.jvm_slot as js

    called = {}
    monkeypatch.setattr(js, "_scan_leaked_jvms_ps",
                        lambda exclude: called.setdefault("ps", True) or
                        [{"pid": 1, "age_s": 0.0, "rss_bytes": 0,
                          "cmdline_head": "x"}])
    monkeypatch.setattr(js.os, "name", "posix")
    monkeypatch.setattr(js.sys, "platform", "darwin")
    assert js.scan_leaked_jvms() and called.get("ps")

    # Explicit root: tree scan even on a patched darwin platform.
    called.clear()
    assert js.scan_leaked_jvms(proc_root="does_not_exist_dir") == []
    assert "ps" not in called


def test_scan_dispatches_to_cim_on_windows_default_root(monkeypatch):
    """Default proc_root on Windows uses the CIM path; an explicit
    proc_root always uses the /proc-style tree scan."""
    import comsol_support.jvm_slot as js

    called = {}
    monkeypatch.setattr(js, "_scan_leaked_jvms_windows",
                        lambda exclude: called.setdefault("cim", True) or
                        [{"pid": 1, "age_s": 0.0, "rss_bytes": 0,
                          "cmdline_head": "x"}])
    monkeypatch.setattr(js.os, "name", "nt")
    assert js.scan_leaked_jvms() and called.get("cim")


@pytest.mark.skipif(
    sys.version_info < (3, 12) and os.name != "nt",
    reason="faking os.name='nt' on POSIX makes Path() a WindowsPath, which "
           "Python < 3.12 cannot instantiate off-Windows (test artifact)",
)
def test_scan_explicit_root_skips_cim_under_nt(monkeypatch):
    """An explicit proc_root always uses the /proc-style tree scan, even
    when os.name says Windows."""
    import comsol_support.jvm_slot as js

    called = {}
    monkeypatch.setattr(js, "_scan_leaked_jvms_windows",
                        lambda exclude: called.setdefault("cim", True) or [])
    monkeypatch.setattr(js.os, "name", "nt")
    assert js.scan_leaked_jvms(proc_root="does_not_exist_dir") == []
    assert "cim" not in called


def test_warn_leaked_jvms_logs_and_returns(tmp_path, caplog, monkeypatch):
    import comsol_support.jvm_slot as js
    monkeypatch.setattr(js, "scan_leaked_jvms",
                        lambda: [{"pid": 7, "age_s": 99.0,
                                  "rss_bytes": 2_000_000_000,
                                  "cmdline_head": "java com.comsol"}])
    with caplog.at_level("WARNING", logger="comsol_support.jvm_slot"):
        leaks = warn_leaked_jvms()
    assert leaks and leaks[0]["pid"] == 7
    assert any("pre-existing COMSOL JVM" in r.message for r in caplog.records)


def test_proc_scan_matches_markers_case_insensitively(tmp_path):
    """A Linux install at /opt/COMSOL64/... spells the marker in caps.

    With the plugins/* classpath wildcard, that uppercase install path
    can be the only marker on a JVM's command line — a case-sensitive
    /proc sweep missed exactly that JVM on macOS before the Windows and
    macOS sweeps were made case-insensitive. All three now agree.
    """
    _fake_proc(tmp_path, 300, ["/opt/COMSOL64/multiphysics/java/glnxa64/"
                               "jre/bin/java", "-cp",
                               "/opt/COMSOL64/multiphysics/plugins/*",
                               "SlotHarvester"])
    leaks = scan_leaked_jvms(proc_root=tmp_path)
    assert [x["pid"] for x in leaks] == [300]


def test_proc_scan_still_ignores_unrelated_java(tmp_path):
    _fake_proc(tmp_path, 301, ["/usr/bin/java", "-jar", "gradle-wrapper.jar"])
    assert scan_leaked_jvms(proc_root=tmp_path) == []
