"""Lint self-test — would-have-caught regression detector.

Three smoke tests intended to fail loudly on any regression in the
linting stack. Designed to run on every commit, NOT gated on the COMSOL
runtime (which is what made the historical SlotHarvester regression go
undetected for many cycles).

A — Compile every .java in comsol_support/java/ against the full COMSOL
    classpath. The single thing that would have caught the historical
    SlotHarvester compile regression. Skipped only if COMSOL is absent.

B — Layer A regex catalog regression-pinning. Run scan_java_source over
    a clean fixture (expects 0 findings) and a deliberately-broken
    fixture (pins exact counts per A-pattern + missing/placeholder
    descriptions). Pure Python, no COMSOL.

C — dimensional.py parser/translator regression-pinning. Exercise the
    tokenize → parse → AST-walk → evaluate pipeline on a handful of
    representative expressions. Pure Python, no Wolfram.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path

import pytest

from comsol_support import COMSOL_PATH
from comsol_support.dimensional import (
    AnalysisRequest,
    analyze,
    ast_has_unit_bearing_leaf,
    find_non_integer_unit_powers,
    parse_comsol,
    _canonicalise_comsol_unit,
)
from comsol_support.java_facade import JavaFacade
from comsol_support.linting import scan_java_source


JAVA_DIR = Path(__file__).resolve().parent.parent / "comsol_support" / "java"
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "lint_selftest"


# ----------------------------------------------------------------------
# A. Compile smoke test
# ----------------------------------------------------------------------


def _comsol_runtime_available() -> bool:
    return Path(COMSOL_PATH).is_dir()


@pytest.mark.skipif(
    not _comsol_runtime_available(),
    reason=f"COMSOL not installed at {COMSOL_PATH}",
)
def test_standalone_slf4j_binding_precedes_osgi_binding():
    """The flat-classpath SLF4J binding must come before the OSGi one.

    Regression: COMSOL's plugins/ dir ships `org.osgi.slf4j.osgi-*.jar`,
    an OSGi-specific SLF4J binding whose `StaticLoggerBinder.<clinit>`
    fails in a flat-classpath standalone Java app. Heat Transfer module
    initialization (which loads ASHRAE properties via SLF4J → SQLite
    JDBC) blows up with `Failed_to_initialize_physics_interface` as a
    consequence. Resolution: `JavaFacade.get_full_classpath` prepends
    `bin/tomcat/lib/org.slf4j.slf4j-jdk14-*.jar` (a plain JDK14
    binding) so the JVM's classloader finds a working
    StaticLoggerBinder first.

    This test pins that ordering. If a future refactor accidentally
    drops the prepend, the test fails immediately rather than after
    spending 5+ minutes loading a Heat Transfer model to discover the
    regression.
    """
    with tempfile.TemporaryDirectory() as wd:
        facade = JavaFacade(comsol_path=COMSOL_PATH, workspace_dir=wd)
        # os.pathsep, not ":" — the Windows separator is ";" and drive
        # letters contain ":".
        cp = facade.get_full_classpath().split(os.pathsep)
        # The broken OSGi binding still ships in plugins/ (it reaches
        # the classpath via the plugins/* wildcard entry).
        osgi_jars = list(
            (Path(COMSOL_PATH) / "plugins").glob("org.osgi.slf4j.osgi-*.jar")
        )
        assert osgi_jars, (
            "expected OSGi slf4j binding to be present in plugins/ "
            "(it's the broken one we're working around)"
        )
        plugins_idx = next(
            (i for i, p in enumerate(cp)
             if p.endswith(os.sep + "*") and "plugins" in p),
            None,
        )
        assert plugins_idx is not None, (
            "expected the plugins/* wildcard entry on the classpath"
        )
        jdk14_idx = next(
            (i for i, p in enumerate(cp)
             if "slf4j-jdk14" in p or "slf4j-simple" in p),
            None,
        )
        # The standalone binding must be in the classpath AND before
        # the plugins/* wildcard (which expands to the OSGi binding).
        assert jdk14_idx is not None, (
            "expected a standalone slf4j binding "
            "(bin/tomcat/lib/org.slf4j.slf4j-jdk14-*.jar) — "
            "this is the fix for the OSGi-binding failure"
        )
        assert jdk14_idx < plugins_idx, (
            f"standalone slf4j binding (index {jdk14_idx}) must come "
            f"before the plugins/* wildcard (index {plugins_idx}) on "
            f"the classpath. Reorder JavaFacade.get_full_classpath."
        )


@pytest.mark.skipif(
    not _comsol_runtime_available(),
    reason=f"COMSOL not installed at {COMSOL_PATH}",
)
def test_run_layer_b_compile_path_succeeds():
    """`run_layer_b` compiles ModelChecker without invoking COMSOL.

    Regression: ModelChecker.java references SlotHarvester.HarvestResult
    unconditionally (the symbol must resolve at compile time even
    though the call site is inside an `if (slotsOut != null)` block).
    A previous version compiled ModelChecker alone and failed with
    "cannot find symbol SlotHarvester" on any real invocation, before
    `test_all_java_sources_compile` caught it (because that test
    compiles all sources together).

    This test exercises the *same compile path* run_layer_b uses, so a
    regression to per-file compilation is caught immediately.
    """
    from comsol_support.linting import CHECKER_SOURCE, CHECKER_CLASS

    with tempfile.TemporaryDirectory() as wd:
        facade = JavaFacade(comsol_path=COMSOL_PATH, workspace_dir=wd)
        javac = facade.find_java_executable("javac")
        checker_source = facade.java_source_dir / CHECKER_SOURCE
        harvester_source = facade.java_source_dir / "SlotHarvester.java"
        compiled_dir = facade.compiled_dir
        compiled_dir.mkdir(parents=True, exist_ok=True)

        cp = facade.get_full_classpath()
        result = subprocess.run(
            [javac, "-cp", cp, "-d", str(compiled_dir),
             str(checker_source), str(harvester_source)],
            capture_output=True, text=True, timeout=120,
        )
        if result.returncode != 0:
            pytest.fail(
                "run_layer_b compile path failed. ModelChecker.java + "
                "SlotHarvester.java must compile together via the same "
                "javac invocation that linting.run_layer_b uses.\n"
                f"--- stderr ---\n{result.stderr[-2000:]}"
            )
        assert (compiled_dir / f"{CHECKER_CLASS}.class").exists()
        assert (compiled_dir / "SlotHarvester.class").exists()


@pytest.mark.skipif(
    not _comsol_runtime_available(),
    reason=f"COMSOL not installed at {COMSOL_PATH}",
)
def test_all_java_sources_compile():
    """Every .java in comsol_support/java/ compiles against the COMSOL classpath.

    This is the test that would have caught the historical SlotHarvester
    compile regression on commit. If this fails, Layer B and Layer C are
    both broken — they live or die together because Layer B runs
    SlotHarvester inline.
    """
    sources = sorted(JAVA_DIR.glob("*.java")) + sorted(
        (JAVA_DIR / "probes").glob("*.java"))
    assert sources, f"no .java sources found in {JAVA_DIR}"

    with tempfile.TemporaryDirectory() as wd:
        facade = JavaFacade(comsol_path=COMSOL_PATH, workspace_dir=wd)
        javac = facade.find_java_executable("javac")
        cp = facade.get_full_classpath()
        classes_dir = Path(wd) / "classes"
        classes_dir.mkdir()

        result = subprocess.run(
            [javac, "-d", str(classes_dir), "-cp", cp,
             *[str(s) for s in sources]],
            capture_output=True, text=True, timeout=180,
        )
        if result.returncode != 0:
            pytest.fail(
                "javac failed on one or more sources in comsol_support/java/. "
                "This is the canonical Layer B/C regression signal.\n"
                f"--- stdout ---\n{result.stdout[-2000:]}\n"
                f"--- stderr ---\n{result.stderr[-2000:]}"
            )

        # Sanity: a representative class file was produced.
        for required in ("ModelChecker.class", "SlotHarvester.class",
                         "ModelExporter.class", "SolverTelemetry.class"):
            assert (classes_dir / required).exists(), \
                f"{required} missing after javac"


# ----------------------------------------------------------------------
# B. Layer A regression pinning
# ----------------------------------------------------------------------


def test_layer_a_clean_fixture_silent():
    """Clean fixture must produce zero findings.

    Any new finding here means the linter started reporting on
    legitimate code — likely a false-positive regression.
    """
    report = scan_java_source(FIXTURES / "clean.java")
    assert report.unit_warnings == [], (
        f"unexpected unit findings on clean fixture: "
        f"{[(w.pattern, w.expression) for w in report.unit_warnings]}"
    )
    assert report.descr_warnings == [], (
        f"unexpected descr findings on clean fixture: "
        f"{[(w.kind, w.name, w.reason) for w in report.descr_warnings]}"
    )


def test_layer_a_broken_fixture_pinned_counts():
    """Broken fixture must produce exactly the documented findings.

    A1 (bracket-op-bracket), A4 (addition in brackets), A5 (non-ASCII
    in brackets), one missing-description, one placeholder-description.
    Any drift means either a regex regressed (false negative) or a new
    pattern fires unexpectedly (false positive).
    """
    report = scan_java_source(FIXTURES / "broken.java")

    patterns = sorted(w.pattern for w in report.unit_warnings)
    assert patterns == ["A1", "A4", "A5"], (
        f"expected exactly [A1, A4, A5]; got {patterns}"
    )

    reasons = sorted(w.reason for w in report.descr_warnings)
    assert reasons == ["missing", "placeholder"], (
        f"expected exactly [missing, placeholder]; got {reasons}"
    )

    # Sanity: the placeholder finding names the right param.
    placeholders = [w for w in report.descr_warnings
                    if w.reason == "placeholder"]
    assert len(placeholders) == 1
    assert placeholders[0].name == "placeholder"


# ----------------------------------------------------------------------
# C. dimensional.py parser/translator smoke (no Wolfram)
# ----------------------------------------------------------------------


@pytest.mark.parametrize("expr", [
    "1[m]",
    "rho*cp*T0",
    "10[m]/5[s]",
    "exp(-t/tau)",
    "sqrt(x^2 + y^2 + z^2)",
    "max(0, T-T_ref)",
    "comp1.var1[V] + 1e-6[V]",
    "A*sin(2*pi*f*t)",
])
def test_dimensional_parse_representative_expressions(expr):
    """Tokenize+parse must succeed on representative COMSOL expressions.

    If this fails, the COMSOL grammar coverage regressed.
    """
    node = parse_comsol(expr)
    assert node is not None


def test_dimensional_unit_canonicalisation():
    """`1/m*s` precedence + `1/X` rewriting are the documented Mathematica
    correctness fixes — pin them so Mathematica compatibility doesn't regress.
    """
    # COMSOL precedence: `1/m*s` ≡ `s/m`, not `1/(m*s)`.
    assert _canonicalise_comsol_unit("1/m*s") == "s/m"
    # `1/m` must rewrite, not pass through (Mathematica parses bare 1 as
    # a unit name).
    canonical = _canonicalise_comsol_unit("1/m")
    assert canonical == "m^-1" or canonical == "1/m^1" or "m^-1" in canonical, \
        f"got {canonical!r}"
    # Compound with cancellation reduces to dimensionless or near it.
    assert _canonicalise_comsol_unit("m/m") in ("", "1")


def test_dimensional_w1_detection():
    """Non-integer exponent on a unit-bearing base must be flagged (W1)."""
    symbol_table = {"rho_val": ""}  # dimensionless symbol; still W1-suspect
    node = parse_comsol("(x[m])^(rho_val)")
    powers = find_non_integer_unit_powers(node, symbol_table)
    assert len(powers) == 1, f"expected 1 W1 finding, got {len(powers)}"

    # Integer literal exponent must NOT trip W1.
    node_ok = parse_comsol("(x[m])^3")
    assert find_non_integer_unit_powers(node_ok, symbol_table) == []


def test_dimensional_evaluate_smoke():
    """The pure-Python engine (replaces the Mathematica
    translation) must fully resolve a representative set."""
    symbol_table = {"rho": "kg/m^3", "cp": "J/(kg*K)", "T0": "K"}
    cases = [
        "1[m]",
        "rho*cp*T0",       # volumetric heat capacity → J/(m^3) = Pa
        "exp(-t/1[s])",
        "sqrt(x^2 + y^2 + z^2)",
    ]
    out = analyze([AnalysisRequest(str(i), e, "")
                   for i, e in enumerate(cases)], symbol_table)
    for f in out:
        assert f.resolved, f"unresolved: {f.expression!r} ({f!r})"
        assert not f.unknown_units


def test_dimensional_unit_bearing_leaf_detection():
    """AST helper used by W1 detector must classify leaves correctly."""
    symbol_table: dict[str, str] = {}
    assert ast_has_unit_bearing_leaf(parse_comsol("1[m]"), symbol_table)
    assert ast_has_unit_bearing_leaf(parse_comsol("1[m] * t"), symbol_table)
    assert not ast_has_unit_bearing_leaf(parse_comsol("3.14"), symbol_table)
    assert not ast_has_unit_bearing_leaf(parse_comsol("foo * 2"), symbol_table)
