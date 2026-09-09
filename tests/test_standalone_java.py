"""Compile + behaviour tests for the COMSOL-free Java sources.

Four sources under ``comsol_support/java/`` import nothing from
``com.comsol.*``: ``TagRegistry``, ``SelectionAlgebra``,
``SolverHeartbeat`` and ``SolverTelemetry``. They therefore compile with
a bare JDK and no classpath, which is what hosted CI (no COMSOL) has —
so this file is the only place those four are *compiled* on every push;
``tests/test_lint_selftest.py::test_all_java_sources_compile`` compiles
everything against the COMSOL classpath but skips without an install.

``test_solver_heartbeat.py`` already drives ``SolverHeartbeat`` through
a Java probe. The two probes here do the same for ``SelectionAlgebra``
and ``TagRegistry``: the previously deleted "full pipeline" test was the
only code that called them by name, and it called a private method, so
until now their behaviour had no test at all.
"""

from __future__ import annotations

import subprocess
import textwrap
from pathlib import Path

from tests.test_solver_heartbeat import _java_or_skip, _javac_or_skip

JAVA_DIR = Path(__file__).resolve().parent.parent / "comsol_support" / "java"

#: Every source the README ("Testing") says hosted CI compiles. If a
#: ``com.comsol`` import ever creeps into one of these, this test is what
#: turns red on the COMSOL-free runners.
STANDALONE_SOURCES = (
    "TagRegistry.java",
    "SelectionAlgebra.java",
    "SolverHeartbeat.java",
    "SolverTelemetry.java",
)


def _compile(javac: str, out: Path, *sources: Path) -> None:
    out.mkdir(exist_ok=True)
    r = subprocess.run(
        [javac, "-d", str(out), *map(str, sources)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=120,
    )
    assert r.returncode == 0, f"javac failed:\n{r.stderr}"


def _run_probe(java: str, classes: Path, main_class: str) -> str:
    r = subprocess.run(
        [java, "-cp", str(classes), main_class],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=30,
    )
    assert r.returncode == 0, f"probe exited {r.returncode}:\n{r.stdout}\n{r.stderr}"
    failures = [l for l in r.stdout.splitlines() if l.startswith("FAIL ")]
    assert not failures, "probe checks failed:\n" + "\n".join(failures)
    assert "RESULT=PASS" in r.stdout, f"probe did not reach the end:\n{r.stdout}"
    return r.stdout


def test_standalone_sources_compile_without_classpath(tmp_path):
    """All four COMSOL-free sources compile in one javac call, no -cp."""
    javac = _javac_or_skip()
    for name in STANDALONE_SOURCES:
        src = JAVA_DIR / name
        assert src.is_file(), src
        assert "import com.comsol" not in src.read_text(encoding="utf-8"), (
            f"{name} imports com.comsol — it is no longer standalone; "
            "update STANDALONE_SOURCES and README 'Testing'")
    out = tmp_path / "classes"
    _compile(javac, out, *(JAVA_DIR / n for n in STANDALONE_SOURCES))
    for name in STANDALONE_SOURCES:
        assert (out / name.replace(".java", ".class")).is_file(), (
            f"{name} compiled but produced no top-level class file")


def test_selection_algebra_behaviour(tmp_path):
    """Primitives, boolean ops, operand resolution, guards, JSON round-trip."""
    javac = _javac_or_skip()
    java = _java_or_skip(javac)

    driver = tmp_path / "SelectionAlgebraProbe.java"
    driver.write_text(textwrap.dedent("""
        import java.util.LinkedHashMap;
        import java.util.List;
        import java.util.Map;
        public class SelectionAlgebraProbe {
            static void check(boolean ok, String what) {
                System.out.println((ok ? "OK   " : "FAIL ") + what);
            }
            static boolean throwsIAE(Runnable r) {
                try { r.run(); return false; }
                catch (IllegalArgumentException e) { return true; }
            }
            public static void main(String[] args) {
                SelectionAlgebra sa = new SelectionAlgebra();
                Map<String, String> top = new LinkedHashMap<>();
                top.put("zmin", "0.9");
                String boxTag = sa.createBox("top", top);
                String expTag = sa.createExplicit("lid", 2, new int[] {3, 4, 5});
                String uniTag = sa.createUnion("top_or_lid", "top", "lid");

                check(boxTag.equals("boxsel1"), "box tag = boxsel1, got " + boxTag);
                check(expTag.equals("expsel1"), "explicit tag = expsel1, got " + expTag);
                check(uniTag.equals("uni1"), "union tag = uni1, got " + uniTag);
                check(sa.size() == 3, "size 3, got " + sa.size());
                check(sa.getTag("top").equals(boxTag), "getTag(top)");
                check(sa.get("lid").params.get("entities").equals("3,4,5"),
                      "explicit entities serialised as 3,4,5");
                check(sa.isPrimitive("top") && sa.isComposite("top_or_lid"),
                      "primitive / composite classification");
                List<String> ops = sa.resolveOperands("top_or_lid");
                check(ops.size() == 2 && ops.contains(boxTag) && ops.contains(expTag),
                      "resolveOperands flattens to primitive tags: " + ops);
                check(sa.exists("top") && !sa.exists("nope") && !sa.exists(null),
                      "exists() is a safe check");

                check(throwsIAE(() -> sa.createBox("top", top)),
                      "duplicate name rejected");
                check(throwsIAE(() -> sa.createUnion("bad", "top", "missing")),
                      "unknown operand rejected");
                check(throwsIAE(() -> sa.createUnion("bad", "top")),
                      "union with one operand rejected");
                check(throwsIAE(() -> sa.getTag("missing")),
                      "getTag on unknown name throws");
                check(sa.size() == 3, "failed creates left no residue");
                check(sa.createBox("bottom", top).equals("boxsel2"),
                      "a rejected create does not burn a tag number");

                String json = sa.serialize();
                SelectionAlgebra back = SelectionAlgebra.deserialize(json);
                check(back.size() == 4, "round-trip size");
                check(back.getTag("top_or_lid").equals(uniTag), "round-trip union tag");
                check(back.resolveOperands("top_or_lid").equals(ops),
                      "round-trip operands");
                check(back.serialize().equals(json), "serialize is stable across round-trip");
                String next = back.createBox("base", top);
                check(next.equals("boxsel3"), "counters survive round-trip, got " + next);
                System.out.println("RESULT=PASS");
            }
        }
        """), encoding="utf-8")

    out = tmp_path / "classes"
    _compile(javac, out, JAVA_DIR / "SelectionAlgebra.java", driver)
    _run_probe(java, out, "SelectionAlgebraProbe")


def test_tag_registry_behaviour(tmp_path):
    """Per-prefix counters, lookups, guards, JSON round-trip."""
    javac = _javac_or_skip()
    java = _java_or_skip(javac)

    driver = tmp_path / "TagRegistryProbe.java"
    driver.write_text(textwrap.dedent("""
        public class TagRegistryProbe {
            static void check(boolean ok, String what) {
                System.out.println((ok ? "OK   " : "FAIL ") + what);
            }
            static boolean throwsIAE(Runnable r) {
                try { r.run(); return false; }
                catch (IllegalArgumentException e) { return true; }
            }
            public static void main(String[] args) {
                TagRegistry reg = new TagRegistry();
                String g = reg.create("geom", "geom", "Geometry", "", "geometry");
                String a = reg.create("main_block", "blk", "Block", g, "geometry");
                String b = reg.create("hole", "cyl", "Cylinder", g, "geometry");
                String c = reg.create("side_block", "blk", "Block", g, "geometry");

                check(g.equals("geom1") && a.equals("blk1") && b.equals("cyl1") && c.equals("blk2"),
                      "per-prefix counters: " + g + " " + a + " " + b + " " + c);
                check(reg.size() == 4, "size 4");
                check(reg.get("hole").equals("cyl1"), "get(name) -> tag");
                check(reg.getByTag("blk2").name.equals("side_block"), "getByTag");
                check(reg.exists("hole") && reg.tagExists("blk1") && !reg.exists("nope"),
                      "exists / tagExists");
                check(reg.getByStage("geometry").size() == 4, "getByStage");
                check(reg.getByType("Block").size() == 2, "getByType");
                check(reg.getChildren("geom").size() == 3, "getChildren(parent name) by tag");
                check(reg.getChildren("hole").isEmpty(), "leaf has no children");

                check(throwsIAE(() -> reg.create("hole", "cyl", "Cylinder", "geom1", "geometry")),
                      "duplicate name rejected");
                check(throwsIAE(() -> reg.create("x", "", "Block", "geom1", "geometry")),
                      "empty prefix rejected");
                check(throwsIAE(() -> reg.create("", "blk", "Block", "geom1", "geometry")),
                      "empty name rejected");
                check(throwsIAE(() -> reg.get("missing")), "get on unknown name throws");
                check(reg.size() == 4, "failed creates left no residue");

                String json = reg.serialize();
                TagRegistry back = TagRegistry.deserialize(json);
                check(back.size() == 4, "round-trip size");
                check(back.getChildren("geom").size() == 3, "round-trip parent links");
                check(back.get("side_block").equals("blk2"), "round-trip lookup");
                check(back.serialize().equals(json), "serialize is stable across round-trip");
                String d = back.create("third_block", "blk", "Block", "geom1", "geometry");
                check(d.equals("blk3"), "counters survive round-trip, got " + d);
                System.out.println("RESULT=PASS");
            }
        }
        """), encoding="utf-8")

    out = tmp_path / "classes"
    _compile(javac, out, JAVA_DIR / "TagRegistry.java", driver)
    _run_probe(java, out, "TagRegistryProbe")
