"""Compile + behavior tests for SolverHeartbeat.java.

The utility is a pure-Java standalone — it does not depend on COMSOL
classes — so we can compile and run it against the bundled JDK without
the full COMSOL classpath. This lets the test run on any machine with
a JDK, and exercises the actual runtime behavior (scheduling, daemon
flag, idempotent stop) rather than just declaration shape.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
import textwrap
from pathlib import Path

import pytest

from comsol_support.java_facade import JavaFacade
from comsol_support import COMSOL_PATH


HEARTBEAT_SRC = (Path(__file__).resolve().parent.parent
                 / "comsol_support" / "java" / "SolverHeartbeat.java")


def _javac_or_skip() -> str:
    """Find a usable javac — prefer COMSOL's bundled JDK, fall back to PATH."""
    if Path(COMSOL_PATH).is_dir():
        try:
            with tempfile.TemporaryDirectory() as wd:
                facade = JavaFacade(comsol_path=COMSOL_PATH, workspace_dir=wd)
                return facade.find_java_executable("javac")
        except Exception:
            pass
    on_path = shutil.which("javac")
    if not on_path:
        pytest.skip("no javac available (neither COMSOL JDK nor PATH)")
    return on_path


def _java_or_skip(javac: str) -> str:
    # Reuse the facade's discovery so the Windows `.exe` suffix is honoured
    # (a bare `Path(javac).parent / "java"` probe skips every Windows run).
    if Path(COMSOL_PATH).is_dir():
        try:
            with tempfile.TemporaryDirectory() as wd:
                facade = JavaFacade(comsol_path=COMSOL_PATH, workspace_dir=wd)
                return facade.find_java_executable("java")
        except Exception:
            pass
    on_path = shutil.which("java")
    if not on_path:
        pytest.skip(f"no `java` runtime (neither COMSOL JDK sibling of {javac} nor PATH)")
    return on_path


def test_solver_heartbeat_compiles_standalone(tmp_path):
    """SolverHeartbeat.java compiles with no classpath dependencies.

    This is the leverage point of the utility: it's lift-and-reuse-able
    in any campaign harness without forcing them to set up the full
    COMSOL classpath just to import the heartbeat.
    """
    javac = _javac_or_skip()
    out = tmp_path / "classes"
    out.mkdir()
    result = subprocess.run(
        [javac, "-d", str(out), str(HEARTBEAT_SRC)],
        capture_output=True, text=True, timeout=60,
    )
    if result.returncode != 0:
        pytest.fail(f"javac failed:\n{result.stderr}")
    assert (out / "SolverHeartbeat.class").exists()


def test_solver_heartbeat_actually_beats(tmp_path):
    """Spawn a tiny driver: start a heartbeat, sleep, stop, count beats.

    A 1.6 s window with 200 ms interval should produce 6–10 beats. If
    it produces 0 (scheduling broken) or >> 10 (somehow firing too
    fast), this fails.
    """
    javac = _javac_or_skip()
    java = _java_or_skip(javac)

    driver = tmp_path / "HeartbeatProbe.java"
    driver.write_text(textwrap.dedent("""
        import java.util.concurrent.atomic.AtomicInteger;
        public class HeartbeatProbe {
            public static void main(String[] args) throws Exception {
                AtomicInteger n = new AtomicInteger(0);
                SolverHeartbeat hb = SolverHeartbeat.start(
                    () -> n.incrementAndGet(), 200L);
                Thread.sleep(1600);
                hb.stop();
                // Second .stop() must be a no-op.
                hb.stop();
                int beatsBeforeSleep = n.get();
                // Sleep another window — no new beats should occur.
                Thread.sleep(500);
                int beatsAfterSleep = n.get();
                System.out.println("BEATS=" + beatsBeforeSleep
                    + " STILL=" + beatsAfterSleep);
            }
        }
        """), encoding="utf-8")

    # Compile both together
    out = tmp_path / "classes"
    out.mkdir()
    r = subprocess.run(
        [javac, "-d", str(out), str(HEARTBEAT_SRC), str(driver)],
        capture_output=True, text=True, timeout=60,
    )
    assert r.returncode == 0, r.stderr

    # Run
    r = subprocess.run(
        [java, "-cp", str(out), "HeartbeatProbe"],
        capture_output=True, text=True, timeout=10,
    )
    assert r.returncode == 0, r.stderr
    # Parse "BEATS=N STILL=M"
    line = [l for l in r.stdout.splitlines()
            if l.startswith("BEATS=")][-1]
    parts = dict(t.split("=") for t in line.split())
    beats = int(parts["BEATS"])
    still = int(parts["STILL"])

    # 200 ms interval over 1600 ms window → ~8 beats (first beat fires
    # at t=200ms, last possible at t=1600ms). Allow a generous range.
    assert 5 <= beats <= 12, f"unexpected beat count: {beats} (stdout={r.stdout!r})"
    # After .stop(), the counter must be frozen (idempotency + clean
    # cancellation).
    assert still == beats, f"beats fired after stop: {still} vs {beats}"


def test_solver_heartbeat_rejects_invalid_args(tmp_path):
    """null Runnable and non-positive intervalMs must throw IAE."""
    javac = _javac_or_skip()
    java = _java_or_skip(javac)

    driver = tmp_path / "RejectProbe.java"
    driver.write_text(textwrap.dedent("""
        public class RejectProbe {
            public static void main(String[] args) {
                int ok = 0;
                try {
                    SolverHeartbeat.start(null, 1000L);
                } catch (IllegalArgumentException e) {
                    ok |= 1;
                }
                try {
                    SolverHeartbeat.start(() -> {}, 0L);
                } catch (IllegalArgumentException e) {
                    ok |= 2;
                }
                try {
                    SolverHeartbeat.start(() -> {}, -1L);
                } catch (IllegalArgumentException e) {
                    ok |= 4;
                }
                System.out.println("OK=" + ok);
            }
        }
        """), encoding="utf-8")

    out = tmp_path / "classes"
    out.mkdir()
    r = subprocess.run(
        [javac, "-d", str(out), str(HEARTBEAT_SRC), str(driver)],
        capture_output=True, text=True, timeout=60,
    )
    assert r.returncode == 0, r.stderr

    r = subprocess.run(
        [java, "-cp", str(out), "RejectProbe"],
        capture_output=True, text=True, timeout=10,
    )
    assert r.returncode == 0, r.stderr
    assert "OK=7" in r.stdout, f"not all three guards fired: {r.stdout!r}"


def test_solver_heartbeat_callback_exception_is_swallowed(tmp_path):
    """A throwing callback must not stop subsequent beats."""
    javac = _javac_or_skip()
    java = _java_or_skip(javac)

    driver = tmp_path / "ThrowProbe.java"
    driver.write_text(textwrap.dedent("""
        import java.util.concurrent.atomic.AtomicInteger;
        public class ThrowProbe {
            public static void main(String[] args) throws Exception {
                AtomicInteger n = new AtomicInteger(0);
                SolverHeartbeat hb = SolverHeartbeat.start(() -> {
                    int v = n.incrementAndGet();
                    if (v % 2 == 1) throw new RuntimeException("boom");
                }, 100L);
                Thread.sleep(700);
                hb.stop();
                System.out.println("N=" + n.get());
            }
        }
        """), encoding="utf-8")

    out = tmp_path / "classes"
    out.mkdir()
    r = subprocess.run(
        [javac, "-d", str(out), str(HEARTBEAT_SRC), str(driver)],
        capture_output=True, text=True, timeout=60,
    )
    assert r.returncode == 0, r.stderr

    r = subprocess.run(
        [java, "-cp", str(out), "ThrowProbe"],
        capture_output=True, text=True, timeout=10,
    )
    assert r.returncode == 0, r.stderr
    line = [l for l in r.stdout.splitlines() if l.startswith("N=")][-1]
    n = int(line.split("=")[1])
    # ~7 beats over 700ms; if exception killed scheduling, n would be 1.
    assert n >= 5, f"scheduling stopped after exception: n={n}"
