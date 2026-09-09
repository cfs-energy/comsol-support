"""Tests for comsol_support.linting — Layer A static scan."""

import os
import json
from pathlib import Path

from comsol_support.linting import (
    _scan_unit_literal,
    scan_java_source,
    _SUFFIX_ONLY,
)


FIXTURE_DIR = Path(__file__).parent / "fixtures" / "linting"


# ---- Unit pattern matrix ------------------------------------------------

def _ids(literal):
    return [p for p, _ in _scan_unit_literal(literal)]


def test_a1_bracket_op_bracket():
    assert _ids("1[m]/[s]") == ["A1"]
    assert _ids("[V]*[A]") == ["A1"]
    assert _ids("x*1[m/s]") == []


def test_a2_operator_then_bracket_no_quantity():
    assert _ids("foo * [m]") == ["A2"]
    assert _ids("/[s]") == ["A2"]
    # `1/[s]` — a digit immediately precedes the operator so the per-plan
    # A2 character class does not fire. Borderline case documented here.
    assert _ids("1/[s]") == []


def test_a3_bare_bracket_without_quantity():
    # A3 only emits when no higher-priority pattern fired.
    assert _ids("(a, [m])") == ["A3"]
    assert _ids("[degC]") == ["A3"]
    assert _ids("1[ohm*m]") == []


def test_a4_plus_minus_inside_brackets():
    assert _ids("[m+s]") == ["A4"]
    assert _ids("[kg-m]") == ["A4"]
    # `[m/s]` is a valid unit — `/` is a unit operator, not a sign.
    # _scan_unit_literal sees a bare bracket so A3 fires at the pattern
    # level; the suffix-only guard in scan_java_source filters it.
    assert _ids("[m/s]") == ["A3"]


def test_a5_unicode_in_brackets():
    assert _ids("[Ω]") == ["A5"]
    assert _ids("[μm]") == ["A5"]
    # Bare ASCII brackets also fire A3 at the pattern level. The
    # suffix-only guard in scan_java_source discards these when they
    # stand alone inside a Java literal (the concat-suffix case).
    assert _ids("[ohm]") == ["A3"]
    assert _ids("[degC]") == ["A3"]


def test_valid_unit_expressions_pass():
    for good in (
        "1[ohm*m]",
        "1[m/s]",
        "4*pi*1e-7[H/m]",
        "a0_val/b0*max(abs(x_val)/b0,1e-6)^(p_val-1)+1e-14[ohm*m]",
        "I_peak*tri_wave(t[1/s]/T_per[1/s])",
        "rho_val*Jz_val",
    ):
        assert _ids(good) == [], f"false positive on {good!r}"


# ---- Suffix-only guard (concat-safe) ------------------------------------

def test_suffix_only_literals_skipped():
    # These are the Java-side suffix of `WIDTH + "[m]"` style code.
    # They must not trigger Layer A, because the true COMSOL expression
    # is the concatenation.
    for lit in ("[m]", " [m] ", "[H/m]", "[ohm*m]", "[s]"):
        assert _SUFFIX_ONLY.match(lit), f"{lit!r} should match suffix-only"


# ---- End-to-end scan_java_source ----------------------------------------

def test_scan_clean_builder_has_no_findings(tmp_path):
    src = tmp_path / "Clean.java"
    src.write_text(
        'import com.comsol.model.*;\n'
        'public class Clean {\n'
        '  public static Model buildModel(java.util.Map<String,String> a) {\n'
        '    Model m = null;\n'
        '    m.param().set("w", "1[m]", "width");\n'
        '    m.param().set("v", "1[m/s]", "speed");\n'
        '    m.component("c").variable("v1").set("ez", "1[V/m]", "E-field");\n'
        '    return m;\n'
        '  }\n'
        '}\n'
    )
    r = scan_java_source(src)
    assert r.unit_warnings == []
    assert r.descr_warnings == []


def test_scan_flags_unit_typos(tmp_path):
    src = tmp_path / "Typos.java"
    src.write_text(
        'public class Typos {\n'
        '  static void x(Object m) {\n'
        '    // A1: bracket-op-bracket\n'
        '    m.call("1[m]/[s]", "A");\n'
        '    // A4: +/- inside\n'
        '    m.call("1[m+s]", "B");\n'
        '    // A5: unicode\n'
        '    m.call("1[Ω]", "C");\n'
        '  }\n'
        '}\n',
        encoding="utf-8",
    )
    r = scan_java_source(src)
    patterns = sorted({w.pattern for w in r.unit_warnings})
    assert "A1" in patterns
    assert "A4" in patterns
    assert "A5" in patterns


def test_scan_detects_missing_descriptions(tmp_path):
    src = tmp_path / "NoDescr.java"
    src.write_text(
        'public class NoDescr {\n'
        '  static void x(Object m) {\n'
        '    m.param().set("alpha", "1[m]");\n'            # missing
        '    m.param().set("beta", "2[s]", "beta descr");\n'  # ok (inline)
        '    m.component("c").variable("v").set("gamma", "rho*J");\n'  # missing
        '  }\n'
        '}\n'
    )
    r = scan_java_source(src)
    by_name = {(d.kind, d.name): d for d in r.descr_warnings}
    assert ("param", "alpha") in by_name
    assert by_name[("param", "alpha")].reason == "missing"
    assert ("variable", "gamma") in by_name
    assert ("param", "beta") not in by_name


def test_scan_finds_descr_via_separate_call(tmp_path):
    src = tmp_path / "SeparateDescr.java"
    src.write_text(
        'public class SeparateDescr {\n'
        '  static void x(Object m) {\n'
        '    m.param().set("alpha", "1[m]");\n'
        '    m.param().descr("alpha", "width of the element");\n'
        '  }\n'
        '}\n'
    )
    r = scan_java_source(src)
    # The separate descr("alpha", ...) should rescue the missing-descr flag.
    assert all(d.name != "alpha" for d in r.descr_warnings)


def test_placeholder_descriptions_are_flagged(tmp_path):
    src = tmp_path / "Placeholder.java"
    src.write_text(
        'public class Placeholder {\n'
        '  static void x(Object m) {\n'
        '    m.param().set("a", "1[m]", "TODO");\n'
        '    m.param().set("b", "2[s]", "xx");\n'         # <3 chars
        '    m.param().set("c", "3[A]", "current draw");\n'
        '  }\n'
        '}\n'
    )
    r = scan_java_source(src)
    by_name = {d.name: d for d in r.descr_warnings}
    assert by_name["a"].reason == "placeholder"
    assert by_name["b"].reason == "placeholder"
    assert "c" not in by_name


# ---- Sidecar serialisation ----------------------------------------------

def test_sidecar_shapes_match_plan(tmp_path):
    src = tmp_path / "Small.java"
    src.write_text(
        'public class Small {\n'
        '  static void x(Object m) {\n'
        '    m.param().set("a", "1[m]/[s]");\n'
        '  }\n'
        '}\n'
    )
    r = scan_java_source(src)
    units = r.to_units_sidecar()
    descrs = r.to_descriptions_sidecar()

    # Units sidecar — layer_a.warnings with the documented keys.
    assert "layer_a" in units
    assert "warnings" in units["layer_a"]
    assert all({"file", "line", "pattern", "expression",
                "suggested_fix"} <= w.keys()
               for w in units["layer_a"]["warnings"])

    # Descriptions sidecar — layer_a.missing_in_source +
    # placeholder_in_source (we keep them separate per the plan schema).
    assert "layer_a" in descrs
    assert "missing_in_source" in descrs["layer_a"]
    assert "placeholder_in_source" in descrs["layer_a"]

    # JSON round-trip cleanly.
    json.dumps(units)
    json.dumps(descrs)


# ---- Real reference builder — no false positives ------------------------

def test_reference_builder_no_unit_warnings():
    """A real reference builder should produce zero unit warnings.

    It may have many missing descriptions (an older builder predates
    .descr()), but all brackets are valid concat suffixes or full-unit
    expressions that Layer A must not flag.
    """
    # External asset, not a repo fixture: enabled by COMSOL_REFERENCE_JAVA
    # (see gaps.md, "Some tests need a local reference model").
    ref_java = os.environ.get("COMSOL_REFERENCE_JAVA", "")
    p = Path(ref_java) if ref_java else None
    try:
        available = p is not None and p.exists()
    except OSError:
        # The asset may live under a home directory this user cannot
        # traverse; exists() then raises instead of returning False.
        available = False
    if not available:
        return  # external fixture not available — skip silently
    r = scan_java_source(p)
    # A1..A5 (typo-class) must not false-fire on this real-world
    # builder. A6 may legitimately fire — an older builder predates the
    # unit-slot-defaults contract, so omitted slots there are real
    # findings, not false positives.
    typos = [w for w in r.unit_warnings
             if w.pattern in ("A1", "A2", "A3", "A4", "A5")]
    assert typos == [], (
        f"unexpected typo-class unit warnings: {typos[:5]}"
    )
    # Descriptions are missing for most entries; just sanity-check we
    # recorded at least one of each kind.
    kinds = {d.kind for d in r.descr_warnings}
    assert "param" in kinds
    assert "variable" in kinds


# ---- A6: missing unit slots ---------------------------------------------

def _write_java(tmp_path, body):
    p = tmp_path / "T.java"
    p.write_text(
        "import com.comsol.model.*;\n"
        "import com.comsol.model.util.*;\n"
        "public class T {\n"
        "  public static void main(String[] args) throws Exception {\n"
        "    Model m = ModelUtil.create(\"m\");\n"
        f"{body}\n"
        "  }\n"
        "}\n"
    )
    return p


def test_a6_fires_on_gfpde_without_slots(tmp_path):
    body = (
        '    m.component("comp1").physics().create("g1", '
        '"GeneralFormPDE", "geom1");\n'
        # no prop("Units").set(...) follows
    )
    p = _write_java(tmp_path, body)
    r = scan_java_source(p)
    a6 = [w for w in r.unit_warnings if w.pattern == "A6"]
    assert len(a6) == 1
    assert "g1" in a6[0].expression
    assert "GeneralFormPDE" in a6[0].expression


def test_a6_silent_when_slot_set(tmp_path):
    body = (
        '    m.component("comp1").physics().create("g1", '
        '"GeneralFormPDE", "geom1");\n'
        '    m.component("comp1").physics("g1").prop("Units")'
        '.set("DependentVariableQuantity", "magneticfield");\n'
    )
    p = _write_java(tmp_path, body)
    r = scan_java_source(p)
    a6 = [w for w in r.unit_warnings if w.pattern == "A6"]
    assert a6 == []


def test_a6_silent_with_skip_comment(tmp_path):
    body = (
        '    // LINT-A6: skip — this stage of the build is dimensionless\n'
        '    m.component("comp1").physics().create("g1", '
        '"GeneralFormPDE", "geom1");\n'
    )
    p = _write_java(tmp_path, body)
    r = scan_java_source(p)
    a6 = [w for w in r.unit_warnings if w.pattern == "A6"]
    assert a6 == []


def test_a6_fires_on_global_equations_without_slots(tmp_path):
    body = (
        '    m.component("comp1").physics().create("ge", '
        '"GlobalEquations", "geom1");\n'
    )
    p = _write_java(tmp_path, body)
    r = scan_java_source(p)
    a6 = [w for w in r.unit_warnings if w.pattern == "A6"]
    assert len(a6) == 1
    assert "GlobalEquations" in a6[0].expression


def test_a6_silent_when_custom_unit_set(tmp_path):
    body = (
        '    m.component("comp1").physics().create("ge", '
        '"GlobalEquations", "geom1");\n'
        '    m.component("comp1").physics("ge").feature("ge1")'
        '.set("CustomDependentVariableUnit", "V");\n'
    )
    p = _write_java(tmp_path, body)
    r = scan_java_source(p)
    a6 = [w for w in r.unit_warnings if w.pattern == "A6"]
    assert a6 == []


def test_a6_silent_for_native_modules(tmp_path):
    """A6 must NOT fire on native physics interfaces (mfh, ht, ec, …) —
    those set their own units internally, no user slot-set is required."""
    body = (
        '    m.component("comp1").physics().create("mfh", "mfh", "geom1");\n'
        '    m.component("comp1").physics().create("ht", "HeatTransfer", "geom1");\n'
    )
    p = _write_java(tmp_path, body)
    r = scan_java_source(p)
    a6 = [w for w in r.unit_warnings if w.pattern == "A6"]
    assert a6 == []


def test_a6_tag_aware_one_set_does_not_discharge_others(tmp_path):
    """When two physics interfaces are created back-to-back and only
    ONE of them has its slots set, A6 must still fire on the other.
    The earlier window-only check would have falsely silenced both."""
    body = (
        '    m.component("comp1").physics().create("g1", '
        '"GeneralFormPDE", "geom1");\n'
        '    m.component("comp1").physics().create("g2", '
        '"GeneralFormPDE", "geom1");\n'
        '    m.component("comp1").physics("g1").prop("Units")'
        '.set("DependentVariableQuantity", "magneticfield");\n'
        # g2 has NO slot set
    )
    p = _write_java(tmp_path, body)
    r = scan_java_source(p)
    a6 = sorted(w.expression for w in r.unit_warnings if w.pattern == "A6")
    assert len(a6) == 1
    assert "g2" in a6[0]


def test_a6_loop_discharges_all_listed_tags(tmp_path):
    """The common builder pattern uses a for-loop to set slots on a
    String[]-listed batch of physics tags. A6 must silently discharge
    every tag mentioned in the array."""
    body = (
        '    m.component("comp1").physics().create("g1", '
        '"GeneralFormPDE", "geom1");\n'
        '    m.component("comp1").physics().create("g2", '
        '"GeneralFormPDE", "geom1");\n'
        '    m.component("comp1").physics().create("g3", '
        '"GeneralFormPDE", "geom1");\n'
        '    for (String ph : new String[]{"g1", "g2", "g3"}) {\n'
        '      model.component("comp1").physics(ph).prop("Units")'
        '.set("DependentVariableQuantity", "magneticfield");\n'
        '    }\n'
    )
    p = _write_java(tmp_path, body)
    r = scan_java_source(p)
    a6 = [w for w in r.unit_warnings if w.pattern == "A6"]
    assert a6 == []


# ---- Description scan: argument splitting (LINT_BACKLOG entry 1) ----------


def test_nonliteral_middle_arg_keeps_description(tmp_path):
    """`String.valueOf(N)` as the 2nd arg must not shift the description
    slot — the trailing literal is still the description (backlog #1)."""
    from comsol_support.linting import scan_java_source
    src = tmp_path / "B.java"
    src.write_text(
        'public class B {\n'
        '  static void f(Model m) {\n'
        '    m.param().set("n_steps", String.valueOf(N_STEPS), "Number of steps");\n'
        '  }\n'
        '}\n'
    )
    r = scan_java_source(src)
    assert all(d.name != "n_steps" for d in r.descr_warnings)


def test_concat_middle_arg_not_mistaken_for_description(tmp_path):
    """String concatenation in the expression slot must not have one of
    its pieces mistaken for the description (old positional parsing
    flagged `"e1"` / `"e2"` fragments as placeholder descriptions)."""
    from comsol_support.linting import scan_java_source
    src = tmp_path / "C.java"
    src.write_text(
        'public class C {\n'
        '  static void f(Model m) {\n'
        '    m.param().set("w", "1[m]"+suffix, "element width");\n'
        '  }\n'
        '}\n'
    )
    r = scan_java_source(src)
    assert all(d.name != "w" for d in r.descr_warnings)


def test_nonliteral_description_expression_is_silent(tmp_path):
    """A description supplied as a Java expression (constant, call) is
    present but statically unknowable — no finding either way."""
    from comsol_support.linting import scan_java_source
    src = tmp_path / "D.java"
    src.write_text(
        'public class D {\n'
        '  static void f(Model m) {\n'
        '    m.param().set("h", "2[m]", DESCR_CONSTANT);\n'
        '  }\n'
        '}\n'
    )
    r = scan_java_source(src)
    assert all(d.name != "h" for d in r.descr_warnings)


def test_dynamic_param_name_is_skipped(tmp_path):
    """A computed parameter name cannot be tracked — no finding."""
    from comsol_support.linting import scan_java_source
    src = tmp_path / "E.java"
    src.write_text(
        'public class E {\n'
        '  static void f(Model m, int i) {\n'
        '    m.param().set("p" + i, "1[A]");\n'
        '  }\n'
        '}\n'
    )
    r = scan_java_source(src)
    assert all(not d.name.startswith("p") for d in r.descr_warnings)


def test_comma_inside_string_does_not_split_args(tmp_path):
    """Commas inside string literals are not argument separators."""
    from comsol_support.linting import scan_java_source
    src = tmp_path / "F.java"
    src.write_text(
        'public class F {\n'
        '  static void f(Model m) {\n'
        '    m.param().set("q", "min(a, b)[m]", "smaller of a, b");\n'
        '  }\n'
        '}\n'
    )
    r = scan_java_source(src)
    assert all(d.name != "q" for d in r.descr_warnings)


def test_two_arg_form_still_flags_missing(tmp_path):
    """The 2-arg form without a separate .descr() still fires missing."""
    from comsol_support.linting import scan_java_source
    src = tmp_path / "G.java"
    src.write_text(
        'public class G {\n'
        '  static void f(Model m) {\n'
        '    m.param().set("nodesc", String.valueOf(N));\n'
        '  }\n'
        '}\n'
    )
    r = scan_java_source(src)
    assert any(d.name == "nodesc" and d.reason == "missing"
               for d in r.descr_warnings)
