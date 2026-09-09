"""Tests for comsol_support.dimensional — Layer C dimensional analyzer.

Layer C is pure Python (the Wolfram bridge was
retired), so EVERY test here runs unconditionally — including the
end-to-end analyze/analyze_model tests that used to be gated on a local
wolframscript install.
"""

from __future__ import annotations

import pytest

from comsol_support.dimensional import (
    AnalysisRequest,
    BinOp,
    Call,
    DimensionalError,
    Ident,
    NumLit,
    SymbolResolver,
    UnaryMinus,
    _canonicalise_comsol_unit,
    analyze,
    analyze_model,
    ast_has_unit_bearing_leaf,
    ast_is_literal_integer,
    build_symbol_table,
    find_non_integer_unit_powers,
    findings_to_sidecar,
    normalise_unit_string,
    parse_comsol,
    tokenize,
    unit_expr_dim,
    unit_name_dim,
)


def _deduce(expr: str, table: dict[str, str] | None = None,
            expected: str = ""):
    """One-expression convenience: returns the single finding."""
    out = analyze([AnalysisRequest("t", expr, expected)], table or {})
    assert len(out) == 1
    return out[0]


# ---- Tokenizer ----------------------------------------------------------

def test_tokenize_simple_expression():
    toks = tokenize("1[m/s] * 2")
    kinds = [t.kind for t in toks]
    assert "NUMBER" in kinds
    assert "LBRACK" in kinds
    assert "OP" in kinds


def test_tokenize_dotted_identifier():
    toks = tokenize("comp1.rho_val")
    assert len(toks) == 1
    assert toks[0].kind == "IDENT"
    assert toks[0].value == "comp1.rho_val"


def test_tokenize_rejects_garbage():
    with pytest.raises(DimensionalError):
        tokenize("1 @ 2")


# ---- Parser -------------------------------------------------------------

def test_parse_number_with_unit():
    ast = parse_comsol("1[m/s]")
    assert isinstance(ast, NumLit)
    assert ast.value == "1"
    assert ast.unit == "m/s"


def test_parse_bare_number():
    ast = parse_comsol("2.5e-3")
    assert isinstance(ast, NumLit)
    assert ast.unit is None


def test_parse_identifier():
    ast = parse_comsol("rho_val")
    assert isinstance(ast, Ident)
    assert ast.name == "rho_val"


def test_parse_binary_precedence():
    # 1 + 2 * 3  →  1 + (2 * 3)
    ast = parse_comsol("1 + 2 * 3")
    assert isinstance(ast, BinOp)
    assert ast.op == "+"
    assert isinstance(ast.right, BinOp) and ast.right.op == "*"


def test_parse_unary_minus():
    ast = parse_comsol("-rho_val")
    assert isinstance(ast, UnaryMinus)
    assert isinstance(ast.operand, Ident)


def test_parse_call_and_nested():
    ast = parse_comsol("max(abs(x)/y, 1e-6)")
    assert isinstance(ast, Call)
    assert ast.name == "max"
    assert len(ast.args) == 2


def test_parse_unit_conversion_syntax():
    # t[1/s] is a COMSOL unit cast on an identifier.
    ast = parse_comsol("t[1/s]")
    assert isinstance(ast, Call)
    assert ast.name == "__unit_cast__"
    assert len(ast.args) == 2


def test_parse_trailing_token_rejected():
    with pytest.raises(DimensionalError):
        parse_comsol("1 + 2 )")


# ---- AST analysis helpers -----------------------------------------------

def test_literal_integer_recognition():
    assert ast_is_literal_integer(parse_comsol("30"))
    assert ast_is_literal_integer(parse_comsol("-3"))
    assert not ast_is_literal_integer(parse_comsol("3.0"))
    assert not ast_is_literal_integer(parse_comsol("n_val"))
    assert not ast_is_literal_integer(parse_comsol("1[m]"))


def test_unit_bearing_leaf_detection():
    st = {"rho": "ohm*m", "n": ""}
    assert ast_has_unit_bearing_leaf(parse_comsol("rho"), st)
    assert ast_has_unit_bearing_leaf(parse_comsol("1[m]"), st)
    assert not ast_has_unit_bearing_leaf(parse_comsol("n"), st)
    assert not ast_has_unit_bearing_leaf(parse_comsol("2 * n"), st)


def test_w1_pattern_detection():
    st = {"rho": "ohm*m", "n": "", "x": "A/m^2", "y": "A/m^2"}
    # A power-law resistivity expression of the kind real models use.
    ast = parse_comsol(
        "a0/y*max(abs(x)/y, 1e-6)^(n-1) + 1e-14[ohm*m]"
    )
    hits = find_non_integer_unit_powers(ast, st)
    assert len(hits) >= 1, "power-law expression should flag W1"


def test_w1_does_not_fire_on_literal_integer_exponent():
    st = {"x": "m"}
    ast = parse_comsol("(1[m]^2) + x^3")
    assert find_non_integer_unit_powers(ast, st) == []


def test_w1_fires_on_non_integer_literal():
    st = {}
    ast = parse_comsol("1[m]^0.5")
    hits = find_non_integer_unit_powers(ast, st)
    assert len(hits) == 1


# ---- Evaluator (ports the retired translator tests' intents) ------------

def test_evaluate_numeric_with_reciprocal_unit():
    # `1[1/m]` must reduce to the m^-1 dimension.
    f = _deduce("1[1/m]")
    assert f.resolved
    assert f.deduced_unit == "m^-1"


def test_evaluate_ident_with_unit():
    f = _deduce("rho_val", {"rho_val": "ohm*m"})
    assert f.resolved
    assert unit_expr_dim(f.deduced_unit)[0] == unit_expr_dim("ohm*m")[0]


def test_evaluate_dimensionless_ident():
    f = _deduce("N", {"N": ""})
    assert f.resolved
    assert f.deduced_unit == ""


def test_evaluate_unknown_ident_flagged_dimensionless():
    f = _deduce("foo")
    assert f.resolved                      # analysis still completes
    assert f.deduced_unit == ""            # treated as dimensionless
    assert f.unresolved_symbols == ["foo"]


def test_evaluate_constants_dimensionless():
    f = _deduce("pi * 2")
    assert f.resolved and f.deduced_unit == ""


def test_evaluate_spatial_coords_are_meters():
    f = _deduce("x + y + z")
    assert f.resolved
    assert f.deduced_unit == "m"


def test_user_symbol_table_overrides_builtin_constants():
    """Domain-agnostic: the caller MUST be able to override a built-in
    constant like `x` or `pi` with a model-specific unit. Built-ins
    are fallbacks only."""
    f = _deduce("x", {"x": "K"})
    assert f.deduced_unit == "K"
    f2 = _deduce("pi", {"pi": ""})
    assert f2.resolved and f2.deduced_unit == ""


def test_evaluate_derivative_encodes_ratio():
    # d(u, x) with u=A/m, x=m → unit = A/m^2
    f = _deduce("d(u, x)", {"u": "A/m"})
    assert f.resolved
    assert f.deduced_unit == "A/m^2"


def test_evaluate_dimensionless_function():
    f = _deduce("tri_wave(t)")
    assert f.resolved and f.deduced_unit == ""


def test_evaluate_unit_conversion_yields_unit_of_cast():
    # t[1/s] carries unit 1/s regardless of t's own unit.
    f = _deduce("t[1/s]")
    assert f.resolved
    assert f.deduced_unit == "s^-1"


# ---- Unit table + engine semantics (pure-Python port) -------------------

def test_unit_table_prefix_decomposition():
    assert unit_name_dim("mm") == unit_name_dim("m")
    assert unit_name_dim("uH") == unit_name_dim("H")
    assert unit_name_dim("kA") == unit_name_dim("A")
    assert unit_name_dim("GHz") == unit_name_dim("Hz")
    assert unit_name_dim("mbar") == unit_name_dim("bar")
    assert unit_name_dim("meV") == unit_name_dim("eV")


def test_unit_table_exact_match_beats_prefix():
    # `min` is minutes (time), not milli-inch; `T` tesla, not tera-…;
    # `Pa` pascal, not peta-annum.
    assert unit_name_dim("min") == unit_name_dim("s")
    assert unit_name_dim("T") == unit_expr_dim("Wb/m^2")[0]
    assert unit_name_dim("Pa") == unit_name_dim("bar")


def test_unit_table_rejects_unknown_and_kg_prefixing():
    assert unit_name_dim("blorp") is None
    assert unit_name_dim("Mkg") is None    # prefixes go on g, not kg


def test_inconsistent_addition_flags_and_unresolves():
    f = _deduce("1[m] + 1[s]")
    assert not f.resolved
    assert f.inconsistent_arithmetic
    assert f.deduced_unit == "UNRESOLVED"


def test_sqrt_halves_dimension_exponents():
    f = _deduce("sqrt(1[m^2])", expected="m")
    assert f.resolved
    assert f.deduced_unit == "m"
    assert f.expected_compatible is True


def test_transcendental_requires_dimensionless_argument():
    assert not _deduce("exp(1[m])").resolved
    f = _deduce("exp(-t/tau)", {"tau": "s"})
    assert f.resolved and f.deduced_unit == ""


def test_unknown_unit_name_surfaces_explicitly():
    f = _deduce("1[blorp]")
    assert not f.resolved
    assert f.unknown_units == ["blorp"]
    sc = findings_to_sidecar([f])
    kinds = [w["kind"] for w in sc["warnings"]]
    assert "unknown_unit" in kinds
    assert "unresolved_dimensions" not in kinds


# ---- Unit string normalisation ------------------------------------------

def test_unit_string_unicode_normalised():
    assert normalise_unit_string("Ω*m") == "ohm*m"
    assert normalise_unit_string("μm") == "um"
    assert normalise_unit_string("°C") == "degC"


def test_unit_string_noop_on_ascii():
    assert normalise_unit_string("ohm*m") == "ohm*m"


# ---- Symbol table builder -----------------------------------------------

def test_build_symbol_table_params_win_over_variables():
    doc = {
        "params":    [{"name": "rho", "unit": "ohm*m"}],
        "variables": [{"name": "rho", "scope": "c/v"}],
    }
    table = build_symbol_table(doc)
    assert table["rho"] == "ohm*m"


def test_build_symbol_table_normalises_unicode():
    doc = {"params": [{"name": "rho", "unit": "Ω*m"}], "variables": []}
    table = build_symbol_table(doc)
    assert table["rho"] == "ohm*m"


# ---- Unit canonicaliser (AST-based, replaces the old regex) -------------

def test_canonicalise_single_identifier_unchanged():
    assert _canonicalise_comsol_unit("m") == "m"
    assert _canonicalise_comsol_unit("ohm") == "ohm"


def test_canonicalise_num_denom_form_preserved():
    assert _canonicalise_comsol_unit("m/s") == "m/s"
    assert _canonicalise_comsol_unit("A/m^2") == "A/m^2"
    assert _canonicalise_comsol_unit("H/m") == "H/m"
    assert _canonicalise_comsol_unit("kg*m/s^2") == "kg*m/s^2"


def test_canonicalise_rewrites_leading_one_over():
    assert _canonicalise_comsol_unit("1/m") == "m^-1"
    assert _canonicalise_comsol_unit("1/s^2") == "s^-2"
    assert _canonicalise_comsol_unit("1/(m*s)") == "m^-1*s^-1"


def test_canonicalise_respects_left_to_right_precedence():
    """`1/m*s` is `(1/m)*s` = `s/m` per standard math precedence.
    The old regex-based code produced `(m*s)^-1` — that was wrong."""
    assert _canonicalise_comsol_unit("1/m*s") == "s/m"


def test_canonicalise_complex_compound():
    # Volts = kg*m^2*s^-3*A^-1 — canonical num/denom form.
    assert _canonicalise_comsol_unit("kg*m^2/(A^2*s^3)") == \
        "kg*m^2/(A^2*s^3)"


def test_canonicalise_passthrough_on_parse_failure():
    # Something outside the unit-expression grammar passes through
    # unchanged (unit brackets inside a unit string, function calls).
    assert _canonicalise_comsol_unit("1[m]") == "1[m]"


def test_canonicalise_empty_and_whitespace():
    assert _canonicalise_comsol_unit("") == ""
    assert _canonicalise_comsol_unit("   ") == ""


def test_canonicalise_self_cancelling_units_are_dimensionless():
    """When exponents cancel to zero, emit the empty string so
    consumers can degrade to a bare dimensionless scalar. The earlier
    implementation produced the broken string `'/'` here."""
    assert _canonicalise_comsol_unit("m/m") == ""
    assert _canonicalise_comsol_unit("1/m*m") == ""
    assert _canonicalise_comsol_unit("ohm*m/(m*ohm)") == ""
    assert _canonicalise_comsol_unit("1") == ""
    # Partial cancellation still emits something sensible.
    assert _canonicalise_comsol_unit("ohm*m/m") == "ohm"


def test_evaluator_treats_dimensionless_cancellation_as_scalar():
    """Self-cancelling units (`m/m`, `ohm*m/(m*ohm)`) must reduce to a
    clean dimensionless result (ported from the retired translator's
    bare-`1` behavior)."""
    f1 = _deduce("1[m/m]")
    assert f1.resolved and f1.deduced_unit == ""
    f2 = _deduce("x", {"x": "ohm*m/(m*ohm)"})
    assert f2.resolved and f2.deduced_unit == ""


# ---- Scope-aware SymbolResolver ----------------------------------------

def test_resolver_same_name_different_scopes_no_conflict():
    """Two `rho_val` definitions with the same deduced unit are fine —
    no scope conflict should fire."""
    doc = {
        "params": [],
        "variables": [
            {"name": "rho_val", "scope": "comp1/var_air",
             "unit": "ohm*m"},
            {"name": "rho_val", "scope": "comp1/var_b",
             "unit": "ohm*m"},
        ],
    }
    r = SymbolResolver(doc)
    r.update_variable("comp1/var_air", "rho_val", "ohm*m")
    r.update_variable("comp1/var_b", "rho_val", "ohm*m")
    assert r.conflicts() == []


def test_resolver_same_name_different_unit_flags_conflict():
    doc = {
        "params": [],
        "variables": [
            {"name": "rho_val", "scope": "comp1/var_cu"},
            {"name": "rho_val", "scope": "comp1/var_air"},
        ],
    }
    r = SymbolResolver(doc)
    r.update_variable("comp1/var_cu",  "rho_val", "ohm*m")
    r.update_variable("comp1/var_air", "rho_val", "ohm*cm")
    conflicts = r.conflicts()
    assert len(conflicts) == 1
    assert conflicts[0]["name"] == "rho_val"
    assert set(conflicts[0]["distinct_units"]) == {"ohm*m", "ohm*cm"}


def test_resolver_param_wins_over_variable_shadow():
    """A param and a variable sharing a name — param wins globally."""
    doc = {
        "params":    [{"name": "T", "unit": "K"}],
        "variables": [{"name": "T", "scope": "comp1/v1"}],
    }
    r = SymbolResolver(doc)
    view = r.view_for_scope("comp1/v1")
    assert view["T"] == "K"


def test_resolver_current_scope_overrides_other_scope():
    doc = {
        "params":    [],
        "variables": [
            {"name": "rho", "scope": "comp1/a"},
            {"name": "rho", "scope": "comp1/b"},
        ],
    }
    r = SymbolResolver(doc)
    r.update_variable("comp1/a", "rho", "ohm*m")
    r.update_variable("comp1/b", "rho", "ohm*km")
    view_a = r.view_for_scope("comp1/a")
    view_b = r.view_for_scope("comp1/b")
    assert view_a["rho"] == "ohm*m"
    assert view_b["rho"] == "ohm*km"


def test_resolver_provides_qualified_names():
    doc = {
        "params":    [],
        "variables": [{"name": "rho", "scope": "comp1/var_air"}],
    }
    r = SymbolResolver(doc)
    r.update_variable("comp1/var_air", "rho", "ohm*m")
    view = r.view_for_scope("comp1/other")
    # Short form `var_air.rho` and full-path `comp1/var_air.rho` both work.
    assert view["var_air.rho"] == "ohm*m"
    assert view["comp1/var_air.rho"] == "ohm*m"


# ---- analyze_model end-to-end with scope conflict -----------------------

def test_analyze_model_emits_scope_conflict_warning():
    symbols = {
        "params": [],
        "variables": [
            {"name": "rho_val", "scope": "comp1/var_a",
             "expression": "1[ohm*m]"},
            {"name": "rho_val", "scope": "comp1/var_b",
             "expression": "1[ohm*cm]"},   # different unit!
        ],
    }
    findings = analyze_model(symbols)
    sc = findings_to_sidecar(findings)
    conflict_warnings = [w for w in sc["warnings"]
                         if w["kind"] == "scope_conflict"]
    assert len(conflict_warnings) == 1
    assert conflict_warnings[0]["name"] == "rho_val"


# ---- Findings sidecar ---------------------------------------------------

def test_findings_sidecar_classifies_warnings():
    from comsol_support.dimensional import DimensionalFinding
    findings = [
        DimensionalFinding(id="a", expression="x",
                           resolved=True, deduced_unit='"m"'),
        DimensionalFinding(id="b", expression="1[m]^0.5",
                           resolved=True, non_integer_power=True),
        DimensionalFinding(id="c", expression="1[m]+1[s]",
                           resolved=False),
        DimensionalFinding(id="d", expression="x",
                           resolved=True, deduced_unit='"m"',
                           expected_unit="s",
                           expected_compatible=False),
        DimensionalFinding(id="e", expression="garbage(",
                           translation_error="bad"),
    ]
    sc = findings_to_sidecar(findings)
    kinds = [w["kind"] for w in sc["warnings"]]
    assert "non_integer_power" in kinds
    assert "unresolved_dimensions" in kinds
    assert "expected_mismatch" in kinds
    assert "translation_error" in kinds
    # 'a' is clean — no warning emitted for it.
    assert all(w["id"] != "a" for w in sc["warnings"])


# ---- End-to-end (pure Python — formerly Wolfram-gated) ------------------


def test_analyze_simple_quantity_math():
    reqs = [AnalysisRequest("t", "1[m/s] * 2[s]", "")]
    out = analyze(reqs, {})
    assert len(out) == 1
    assert out[0].resolved
    assert out[0].deduced_unit == "m"


def test_analyze_detects_expected_mismatch():
    reqs = [AnalysisRequest("t", "1[ohm*m] * 1[A/m^2]", "1/m")]
    out = analyze(reqs, {})
    assert out[0].resolved
    assert out[0].expected_compatible is False
    # Positive control: the correct expectation (V/m) is compatible.
    ok = analyze([AnalysisRequest("t", "1[ohm*m] * 1[A/m^2]", "V/m")], {})
    assert ok[0].expected_compatible is True


def test_analyze_detects_w1_class():
    reqs = [AnalysisRequest("t",
            "1[m]^(n-1)", "")]
    out = analyze(reqs, {"n": ""})
    assert out[0].non_integer_power


def test_analyze_model_on_synthetic_fragment():
    # Minimal stand-in for a real symbols.json — just enough to
    # verify the multi-pass resolves Ez_val via deduced rho_val.
    symbols = {
        "params": [
            {"name": "rho_air_val", "scope": "param",
             "expression": "1[ohm*m]", "unit": "ohm*m"},
        ],
        "variables": [
            {"name": "rho_val", "scope": "comp1/var_air",
             "expression": "rho_air_val"},
            {"name": "Jz_val", "scope": "comp1/var1",
             "expression": "d(u, x) - d(u, y)"},
            {"name": "Ez_val", "scope": "comp1/var_post",
             "expression": "rho_val * Jz_val"},
        ],
    }
    findings = analyze_model(symbols)
    by_id = {f.id: f for f in findings}
    assert by_id["variable:comp1/var_post:Ez_val"].resolved
    # rho_val is ohm*m, Jz_val is 1/m → Ez_val is ohm
    # (Matches COMSOL's own W2 "Deduced unit is [Ohm]".)
    ez = by_id["variable:comp1/var_post:Ez_val"].deduced_unit
    assert unit_expr_dim(ez)[0] == unit_expr_dim("ohm")[0]


def test_analyze_model_expected_overrides_flag_mismatch():
    symbols = {
        "params": [{"name": "mu0_val", "scope": "param",
                    "expression": "4*pi*1e-7[H/m]", "unit": "H/m"}],
        "variables": [],
    }
    overrides = {"param:param:mu0_val": "s/m^2"}
    findings = analyze_model(symbols, overrides)
    assert findings[0].expected_compatible is False
    # Positive control: the declared unit itself is compatible.
    ok = analyze_model(symbols, {"param:param:mu0_val": "H/m"})
    assert ok[0].expected_compatible is True
