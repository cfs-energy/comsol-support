"""dimensional — Layer C symbolic unit analysis (pure Python).

Layers A and B catch the easy classes (unit-bracket typos, missing
descriptions, parameter unit errors). They cannot reach expression-level
dimensional analysis on variables or physics quantities — see
`comsol_linting_research.md` for the dead ends against COMSOL's
internal analyser.

Layer C is an independent dimensional engine: it parses an expression
in COMSOL syntax, resolves each symbol's unit from a symbol table, and
propagates dimensions through the AST as 7-vectors of rational
exponents over the SI base dimensions (mass, length, time, current,
temperature, amount, luminosity). The engine is:

1. **Topic-agnostic.** SI base dimensions and compound units; nothing
   is electromagnetism-specific.
2. **Independent of COMSOL.** The unit table and algebra are this
   module's own — Layer C remains a second opinion on COMSOL's
   analyser, not a wrapper around it.
3. **Dependency-free.** Stdlib only. (This module once
   shelled out to Wolfram/Mathematica for the terminal unit algebra;
   an expired license silently disabled Layer C fleet-wide, and the
   algebra involved — abelian-group arithmetic over Q^7 plus a unit
   lookup table — never needed a computer-algebra system. The
   tokenizer, parser, W1 detection, scope resolution, and multi-pass
   loop were always Python; the port replaced only the evaluation
   oracle. See docs/linting.md.)

## What Layer C detects

- **Dimensional inconsistency in arithmetic** — `+`/`-` (or
  `max`/`min`/`atan2`) over resolved-but-different dimensions sets
  `inconsistent_arithmetic` and leaves the expression unresolved.
- **Symbolic / non-integer exponent on a unit-bearing base** (W1
  class) — detected structurally on the AST before evaluation.
- **Expected-vs-deduced mismatch** — when the caller supplies an
  expected unit (from a physics-slot annotation), compatibility is
  dimension equality (CompatibleUnitQ semantics — W2/W3 class when
  physics-slot expected units are supplied).
- **Unresolved references** — any identifier not in the symbol table
  is recorded as an unresolved symbol and treated as dimensionless so
  the rest of the expression still analyses.
- **Unknown unit names** — a unit name absent from the table leaves
  the expression unresolved and is surfaced explicitly (the Wolfram
  path silenced these via `Off[Quantity::unkunit]`).

Not in Layer C's scope: physics-interface-derived expected units (the
exact `Ga`/`da` slot-unit contracts for General Form PDE etc.). Callers
can pass explicit expected units alongside each expression; layer C
compares them. Deriving expected units from persisted physics props is
left to future integration code, not this module.

## COMSOL syntax supported

- Numeric literals: ints, decimals, exponentials (`1e-7`, `1.5E+3`).
- Unit-annotated literals: `NUMBER[unit_expr]`, e.g. `4*pi*1e-7[H/m]`.
- Identifiers: `[A-Za-z_][A-Za-z0-9_]*`, case-sensitive. Dotted
  qualifiers like `comp1.rho_val` and `mod1.u` are read as single
  names and looked up verbatim in the symbol table (no scope
  resolution — the caller owns that).
- Operators: `+`, `-`, `*`, `/`, `^`, unary `-`, parentheses.
- Functions: `max`, `min`, `abs`, `exp`, `log`, `log10`, `sqrt`,
  `sin`, `cos`, `tan`, `d` (2-arg derivative), `if`, the COMSOL
  dimensionless built-ins (`flc2hs`, `tri_wave`, …), plus a permissive
  passthrough for any other call (the return unit defaults to the
  first argument's unit, which is correct for most COMSOL math
  functions on scalars).

Unit-expression grammar inside `[…]`: identifiers, digits, `^`, `*`,
`/`, parentheses. Unit names resolve against an explicit COMSOL unit
table with SI-prefix decomposition (`mm`, `uH`, `kA`, `GHz`, `mbar`);
`deduced_unit` strings are rendered in canonical SI base-unit form
(`m/s`, `kg*m^2/(A^2*s^3)`) and re-parse through the same table.
"""

from __future__ import annotations

import json
import logging
import math
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from fractions import Fraction
from typing import Any

logger = logging.getLogger("comsol_support.dimensional")


# ---- Exceptions ----

class DimensionalError(Exception):
    """Layer C failure (parse error, translation bug, etc.)."""


# ---- Data ----

@dataclass
class DimensionalFinding:
    """One analysis result per input expression."""
    id: str
    expression: str
    deduced_unit: str = ""        # canonical base form, e.g. "m/s";
                                  # "" = dimensionless; "UNRESOLVED"
    dimensions: str = ""          # dimension signature JSON or ""
    resolved: bool = False
    inconsistent_arithmetic: bool = False   # +/- of incompatibles
    non_integer_power: bool = False         # W1-class flag
    expected_unit: str = ""                 # as supplied by caller
    expected_compatible: bool | None = None # None = not checked
    unresolved_symbols: list[str] = field(default_factory=list)
    # Unit NAMES the engine's table doesn't know — these
    # leave the expression unresolved and deserve their own diagnostic.
    unknown_units: list[str] = field(default_factory=list)
    translation_error: str = ""             # parser/translator failure


# ---- Tokenizer ----

_TOKEN_SPEC = [
    ("NUMBER",  r"\d+\.\d*(?:[eE][+-]?\d+)?"
                r"|\.\d+(?:[eE][+-]?\d+)?"
                r"|\d+(?:[eE][+-]?\d+)?"),
    ("IDENT",   r"[A-Za-z_][A-Za-z_0-9]*(?:\.[A-Za-z_][A-Za-z_0-9]*)*"),
    ("LBRACK",  r"\["),
    ("RBRACK",  r"\]"),
    ("LPAREN",  r"\("),
    ("RPAREN",  r"\)"),
    ("COMMA",   r","),
    ("OP",      r"\*\*|[+\-*/^]"),
    ("SPACE",   r"\s+"),
]
_TOKEN_RE = re.compile("|".join(f"(?P<{n}>{p})" for n, p in _TOKEN_SPEC))


@dataclass
class Token:
    kind: str
    value: str
    pos: int


def tokenize(src: str) -> list[Token]:
    """Break a COMSOL expression string into tokens."""
    out: list[Token] = []
    pos = 0
    n = len(src)
    while pos < n:
        m = _TOKEN_RE.match(src, pos)
        if m is None:
            raise DimensionalError(
                f"unexpected character at pos {pos} in {src!r}"
            )
        kind = m.lastgroup
        if kind != "SPACE":
            out.append(Token(kind, m.group(), pos))
        pos = m.end()
    return out


# ---- AST ----

@dataclass
class NumLit:
    value: str          # raw text, preserved
    unit: str | None    # COMSOL unit expression inside [...] or None


@dataclass
class Ident:
    name: str           # full dotted name as seen


@dataclass
class BinOp:
    op: str             # + - * / ^
    left: Any
    right: Any


@dataclass
class UnaryMinus:
    operand: Any


@dataclass
class Call:
    name: str
    args: list[Any]


class _Parser:
    def __init__(self, tokens: list[Token]):
        self.toks = tokens
        self.i = 0

    def _peek(self, k: int = 0) -> Token | None:
        j = self.i + k
        return self.toks[j] if j < len(self.toks) else None

    def _eat(self, kind: str, value: str | None = None) -> Token:
        t = self._peek()
        if t is None or t.kind != kind or (value is not None
                                           and t.value != value):
            want = kind + (f"={value!r}" if value is not None else "")
            raise DimensionalError(
                f"expected {want} got {t!r} at pos "
                f"{t.pos if t else 'EOF'}"
            )
        self.i += 1
        return t

    # expr := term (('+'|'-') term)*
    def parse_expr(self) -> Any:
        node = self.parse_term()
        while True:
            t = self._peek()
            if t and t.kind == "OP" and t.value in ("+", "-"):
                self.i += 1
                right = self.parse_term()
                node = BinOp(t.value, node, right)
            else:
                return node

    # term := factor (('*'|'/') factor)*
    def parse_term(self) -> Any:
        node = self.parse_factor()
        while True:
            t = self._peek()
            if t and t.kind == "OP" and t.value in ("*", "/"):
                self.i += 1
                right = self.parse_factor()
                node = BinOp(t.value, node, right)
            else:
                return node

    # factor := unary_power
    def parse_factor(self) -> Any:
        return self.parse_power()

    # power := unary ('^' unary)*  (right-associative in COMSOL too)
    def parse_power(self) -> Any:
        node = self.parse_unary()
        t = self._peek()
        if t and t.kind == "OP" and t.value == "^":
            self.i += 1
            right = self.parse_power()
            return BinOp("^", node, right)
        return node

    # unary := '-' unary | atom
    def parse_unary(self) -> Any:
        t = self._peek()
        if t and t.kind == "OP" and t.value == "-":
            self.i += 1
            return UnaryMinus(self.parse_unary())
        if t and t.kind == "OP" and t.value == "+":
            self.i += 1
            return self.parse_unary()
        return self.parse_atom()

    # atom := NUMBER ('[' unit ']')? | IDENT ('(' args ')')? | '(' expr ')'
    def parse_atom(self) -> Any:
        t = self._peek()
        if t is None:
            raise DimensionalError("unexpected end of expression")
        if t.kind == "NUMBER":
            self.i += 1
            unit = self._parse_opt_unit()
            return NumLit(t.value, unit)
        if t.kind == "LPAREN":
            self.i += 1
            node = self.parse_expr()
            self._eat("RPAREN")
            # COMSOL allows `(expr)[unit]`? NOT in standard syntax — the
            # linter already flags that via Layer A A3. We don't attach
            # units to expression groups.
            return node
        if t.kind == "IDENT":
            self.i += 1
            nxt = self._peek()
            if nxt and nxt.kind == "LPAREN":
                self.i += 1
                args = []
                if self._peek() and self._peek().kind != "RPAREN":
                    args.append(self.parse_expr())
                    while self._peek() and self._peek().kind == "COMMA":
                        self.i += 1
                        args.append(self.parse_expr())
                self._eat("RPAREN")
                return Call(t.value, args)
            # `IDENT[unit]` — COMSOL unit-conversion syntax. Evaluates
            # identifier's value in the given units; the result has
            # exactly that unit. We model it as a Call to a synthetic
            # `__unit_cast__` that takes (identifier, unit).
            ident = Ident(t.value)
            if nxt and nxt.kind == "LBRACK":
                unit = self._parse_opt_unit()
                if unit is not None:
                    return Call("__unit_cast__",
                                [ident, NumLit("1", unit)])
            return ident
        raise DimensionalError(
            f"unexpected token {t!r} at pos {t.pos}"
        )

    def _parse_opt_unit(self) -> str | None:
        t = self._peek()
        if t is None or t.kind != "LBRACK":
            return None
        self.i += 1
        # Collect raw text until matching RBRACK, preserving operators
        # and identifiers. Unit expressions never contain nested [].
        buf: list[str] = []
        depth = 0
        while True:
            tk = self._peek()
            if tk is None:
                raise DimensionalError("unterminated unit bracket")
            if tk.kind == "LBRACK":
                depth += 1
            if tk.kind == "RBRACK":
                if depth == 0:
                    self.i += 1
                    return "".join(buf)
                depth -= 1
            buf.append(tk.value)
            self.i += 1


def ast_has_unit_bearing_leaf(node: Any,
                              symbol_table: dict[str, str]) -> bool:
    """True if the sub-AST contains any unit-bearing quantity.

    Used for W1 detection — we flag `base^exponent` when the exponent
    is not a literal integer AND the base statically contains something
    with units. COMSOL's analyser is conservative in the same way: it
    doesn't attempt to prove that a compound base reduces to
    dimensionless before applying the integer-exponent rule.
    """
    if isinstance(node, NumLit):
        return node.unit is not None
    if isinstance(node, Ident):
        u = symbol_table.get(node.name)
        return u not in (None, "")
    if isinstance(node, UnaryMinus):
        return ast_has_unit_bearing_leaf(node.operand, symbol_table)
    if isinstance(node, Call):
        return any(ast_has_unit_bearing_leaf(a, symbol_table)
                   for a in node.args)
    if isinstance(node, BinOp):
        return (ast_has_unit_bearing_leaf(node.left, symbol_table)
                or ast_has_unit_bearing_leaf(node.right, symbol_table))
    return False


def ast_is_literal_integer(node: Any) -> bool:
    """True if the AST is a literal integer (possibly negated)."""
    if isinstance(node, NumLit):
        if node.unit is not None:
            return False
        # A literal integer has no fractional part and no exponent.
        v = node.value
        return bool(re.fullmatch(r"-?\d+", v))
    if isinstance(node, UnaryMinus):
        return ast_is_literal_integer(node.operand)
    return False


def find_non_integer_unit_powers(
    node: Any, symbol_table: dict[str, str],
) -> list[tuple[Any, Any]]:
    """Collect (base, exponent) pairs for every `^` in the AST whose
    exponent is not a literal integer and whose base statically
    contains a unit-bearing leaf — the W1-class pattern.
    """
    hits: list[tuple[Any, Any]] = []

    def _walk(n: Any) -> None:
        if isinstance(n, BinOp) and n.op == "^":
            if (not ast_is_literal_integer(n.right)
                    and ast_has_unit_bearing_leaf(n.left, symbol_table)):
                hits.append((n.left, n.right))
            _walk(n.left)
            _walk(n.right)
        elif isinstance(n, BinOp):
            _walk(n.left)
            _walk(n.right)
        elif isinstance(n, UnaryMinus):
            _walk(n.operand)
        elif isinstance(n, Call):
            for a in n.args:
                _walk(a)

    _walk(node)
    return hits


def parse_comsol(src: str) -> Any:
    """Parse a COMSOL expression string to an AST."""
    toks = tokenize(src)
    if not toks:
        raise DimensionalError("empty expression")
    p = _Parser(toks)
    node = p.parse_expr()
    if p._peek() is not None:
        raise DimensionalError(
            f"trailing tokens after parse at pos {p._peek().pos}: "
            f"{src[p._peek().pos:]!r}"
        )
    return node


# ---- Dimension algebra (pure Python, stdlib only) ----
#
# A dimension is a 7-vector of Fraction exponents over the SI base
# dimensions, in this fixed order:
#
#     (mass, length, time, current, temperature, amount, luminosity)
#
# Unit algebra is then elementary: multiply = add vectors, divide =
# subtract, power = scale. Fractions keep sqrt exact (m^2 → m).

_DIM_NAMES = ("mass", "length", "time", "current",
              "temperature", "amount", "luminosity")
_BASE_SYMBOLS = ("kg", "m", "s", "A", "K", "mol", "cd")

DimVector = tuple  # 7-tuple of Fraction

_D0: DimVector = tuple(Fraction(0) for _ in range(7))  # dimensionless


def _dim(mass=0, length=0, time=0, current=0,
         temperature=0, amount=0, luminosity=0) -> DimVector:
    return (Fraction(mass), Fraction(length), Fraction(time),
            Fraction(current), Fraction(temperature), Fraction(amount),
            Fraction(luminosity))


def _dim_mul(a: DimVector, b: DimVector) -> DimVector:
    return tuple(x + y for x, y in zip(a, b))


def _dim_div(a: DimVector, b: DimVector) -> DimVector:
    return tuple(x - y for x, y in zip(a, b))


def _dim_pow(a: DimVector, e: Fraction) -> DimVector:
    return tuple(x * e for x in a)


# COMSOL unit name → (dimension, SI-base scale factor). Case-sensitive,
# matching COMSOL's own unit-bracket vocabulary. Scales matter for one
# thing only: telling `ohm*m` apart from `ohm*cm` in deduced-unit
# strings (the cross-scope conflict detector compares them — the
# Wolfram path preserved that distinction via QuantityUnit).
# COMPATIBILITY stays dimension-only, exactly like CompatibleUnitQ.
_UNIT_DIMS: dict[str, tuple[DimVector, float]] = {
    # SI base (+ gram: same dimension as kg, different scale)
    "m": (_dim(length=1), 1.0), "kg": (_dim(mass=1), 1.0),
    "g": (_dim(mass=1), 1e-3),
    "s": (_dim(time=1), 1.0), "A": (_dim(current=1), 1.0),
    "K": (_dim(temperature=1), 1.0),
    "mol": (_dim(amount=1), 1.0), "cd": (_dim(luminosity=1), 1.0),
    # Angles / solid angles — dimensionless
    "rad": (_D0, 1.0), "sr": (_D0, 1.0),
    "deg": (_D0, math.pi / 180.0),
    # Temperature scales (offsets are irrelevant to dimension)
    "degC": (_dim(temperature=1), 1.0),
    "degF": (_dim(temperature=1), 5.0 / 9.0),
    "degR": (_dim(temperature=1), 5.0 / 9.0),
    # Derived SI
    "Hz": (_dim(time=-1), 1.0), "Bq": (_dim(time=-1), 1.0),
    "N": (_dim(mass=1, length=1, time=-2), 1.0),
    "Pa": (_dim(mass=1, length=-1, time=-2), 1.0),
    "J": (_dim(mass=1, length=2, time=-2), 1.0),
    "W": (_dim(mass=1, length=2, time=-3), 1.0),
    "C": (_dim(time=1, current=1), 1.0),
    "V": (_dim(mass=1, length=2, time=-3, current=-1), 1.0),
    "F": (_dim(mass=-1, length=-2, time=4, current=2), 1.0),
    "ohm": (_dim(mass=1, length=2, time=-3, current=-2), 1.0),
    "S": (_dim(mass=-1, length=-2, time=3, current=2), 1.0),
    "Wb": (_dim(mass=1, length=2, time=-2, current=-1), 1.0),
    "T": (_dim(mass=1, time=-2, current=-1), 1.0),
    "H": (_dim(mass=1, length=2, time=-2, current=-2), 1.0),
    "lm": (_dim(luminosity=1), 1.0),
    "lx": (_dim(length=-2, luminosity=1), 1.0),
    "Gy": (_dim(length=2, time=-2), 1.0),
    "Sv": (_dim(length=2, time=-2), 1.0),
    "kat": (_dim(time=-1, amount=1), 1.0),
    # Non-SI time
    "min": (_dim(time=1), 60.0), "h": (_dim(time=1), 3600.0),
    "d": (_dim(time=1), 86400.0),
    # Non-SI length
    "in": (_dim(length=1), 0.0254), "ft": (_dim(length=1), 0.3048),
    "yd": (_dim(length=1), 0.9144),
    "mi": (_dim(length=1), 1609.344),
    "mil": (_dim(length=1), 2.54e-5),
    # Volume
    "L": (_dim(length=3), 1e-3), "l": (_dim(length=3), 1e-3),
    # Mass
    "lb": (_dim(mass=1), 0.45359237),
    # Energy
    "eV": (_dim(mass=1, length=2, time=-2), 1.602176634e-19),
    "Wh": (_dim(mass=1, length=2, time=-2), 3600.0),
    "cal": (_dim(mass=1, length=2, time=-2), 4.1868),
    # Pressure
    "bar": (_dim(mass=1, length=-1, time=-2), 1e5),
    "atm": (_dim(mass=1, length=-1, time=-2), 101325.0),
    "Torr": (_dim(mass=1, length=-1, time=-2), 133.322),
    "mmHg": (_dim(mass=1, length=-1, time=-2), 133.322),
    "psi": (_dim(mass=1, length=-1, time=-2), 6894.757),
}

# Names that never take an SI prefix (prefixing them is either
# meaningless — `kmin` — or already spoken for — `kg` prefixes on `g`).
_NO_PREFIX = frozenset({
    "kg", "min", "h", "d", "in", "ft", "yd", "mi", "mil", "lb",
    "atm", "Torr", "mmHg", "psi", "cal",
    "deg", "degC", "degF", "degR", "rad", "sr",
})

# SI prefixes with multipliers, longest first so `da` (deka) is tried
# before `d` (deci).
_SI_PREFIX_SCALE = {
    "da": 1e1, "Y": 1e24, "Z": 1e21, "E": 1e18, "P": 1e15, "T": 1e12,
    "G": 1e9, "M": 1e6, "k": 1e3, "h": 1e2, "d": 1e-1, "c": 1e-2,
    "m": 1e-3, "u": 1e-6, "n": 1e-9, "p": 1e-12, "f": 1e-15,
    "a": 1e-18, "z": 1e-21, "y": 1e-24,
}
_SI_PREFIXES = tuple(sorted(_SI_PREFIX_SCALE, key=len, reverse=True))


def unit_name_dim_scale(name: str) -> tuple[DimVector, float] | None:
    """(dimension, SI scale) of a single COMSOL unit name, or None.

    Exact table match wins (so `min` is minutes, `T` is tesla, `Pa` is
    pascal); otherwise try SI-prefix decomposition (`mm`, `uH`, `kA`,
    `GHz`, `mbar`, `meV`, …) against prefixable table entries.
    """
    hit = _UNIT_DIMS.get(name)
    if hit is not None:
        return hit
    for p in _SI_PREFIXES:
        if name.startswith(p) and len(name) > len(p):
            rest = name[len(p):]
            entry = _UNIT_DIMS.get(rest)
            if entry is not None and rest not in _NO_PREFIX:
                return entry[0], entry[1] * _SI_PREFIX_SCALE[p]
    return None


def unit_name_dim(name: str) -> DimVector | None:
    """Dimension of a single COMSOL unit name, or None when unknown."""
    hit = unit_name_dim_scale(name)
    return None if hit is None else hit[0]


def _reduce_unit_ast_scaled(node: object) -> tuple[dict, float]:
    """Like `_reduce_unit_ast` but numeric literals contribute a SCALE
    factor instead of being rejected — needed so deduced-unit strings
    like `0.01*kg*m^2/(A^2*s^3)` (an `ohm*cm` product) re-parse through
    the multi-pass feedback loop.
    """
    if isinstance(node, NumLit):
        if node.unit is not None:
            raise _UnitReduceError("nested unit annotation in unit string")
        try:
            return {}, float(node.value)
        except ValueError:
            raise _UnitReduceError(f"bad numeric literal {node.value!r}")
    if isinstance(node, Ident):
        return {node.name: 1}, 1.0
    if isinstance(node, UnaryMinus):
        raise _UnitReduceError("unary minus not valid in unit algebra")
    if isinstance(node, Call):
        raise _UnitReduceError(f"function {node.name!r} not valid in unit")
    if isinstance(node, BinOp):
        if node.op == "*":
            lo, ls = _reduce_unit_ast_scaled(node.left)
            ro, rs = _reduce_unit_ast_scaled(node.right)
            out = dict(lo)
            for k, v in ro.items():
                out[k] = out.get(k, 0) + v
            return out, ls * rs
        if node.op == "/":
            lo, ls = _reduce_unit_ast_scaled(node.left)
            ro, rs = _reduce_unit_ast_scaled(node.right)
            out = dict(lo)
            for k, v in ro.items():
                out[k] = out.get(k, 0) - v
            if rs == 0:
                raise _UnitReduceError("zero scale divisor")
            return out, ls / rs
        if node.op == "^":
            base, bs = _reduce_unit_ast_scaled(node.left)
            exp = _extract_numeric_exponent(node.right)
            return {k: v * exp for k, v in base.items()}, bs ** exp
        raise _UnitReduceError(f"operator {node.op!r}")
    raise _UnitReduceError(f"unknown node {type(node).__name__}")


def unit_expr_dim_scale(
    unit: str,
) -> tuple[DimVector | None, float, list[str]]:
    """(dimension, SI scale, unknown_names) of a COMSOL unit expression
    (`A/m^2`, `ohm*cm`, `0.01*kg*m^2/(A^2*s^3)`, …). `dim` is None when
    the expression cannot be reduced (parse failure or any unknown unit
    name); the unknown names are listed so callers can surface them.
    """
    stripped = (unit or "").strip()
    if not stripped:
        return _D0, 1.0, []
    try:
        ast = parse_comsol(stripped)
        exponents, scale = _reduce_unit_ast_scaled(ast)
    except (DimensionalError, _UnitReduceError):
        return None, 1.0, [stripped]
    dim = _D0
    unknown: list[str] = []
    for name, e in exponents.items():
        hit = unit_name_dim_scale(name)
        if hit is None:
            unknown.append(name)
            continue
        d, s = hit
        frac = Fraction(e) if not isinstance(e, float) \
            else Fraction(e).limit_denominator(1_000_000)
        dim = _dim_mul(dim, _dim_pow(d, frac))
        try:
            scale *= s ** float(frac)
        except (OverflowError, ZeroDivisionError):
            scale = 1.0
    if unknown:
        return None, 1.0, unknown
    return dim, scale, []


def unit_expr_dim(unit: str) -> tuple[DimVector | None, list[str]]:
    """Dimension of a COMSOL unit expression — see `unit_expr_dim_scale`."""
    dim, _scale, unknown = unit_expr_dim_scale(unit)
    return dim, unknown


def _fmt_exp(e: Fraction) -> str:
    """Render a Fraction exponent for a COMSOL-style unit string."""
    if e.denominator == 1:
        return str(e.numerator)
    return repr(float(e))


def dim_to_comsol_unit(dim: DimVector) -> str:
    """Render a dimension as a canonical COMSOL-style unit string in SI
    base units — `m/s`, `kg*m^2/(A^2*s^3)`, `m^0.5`, `""` for
    dimensionless. The output re-parses through `unit_expr_dim`, which
    is what lets multi-pass feedback re-ingest deduced units.
    """
    pos: dict[str, Fraction] = {}
    neg: dict[str, Fraction] = {}
    for sym, e in zip(_BASE_SYMBOLS, dim):
        if e > 0:
            pos[sym] = e
        elif e < 0:
            neg[sym] = -e
    if not pos and not neg:
        return ""

    def _terms(d: dict[str, Fraction]) -> str:
        return "*".join(
            n if d[n] == 1 else f"{n}^{_fmt_exp(d[n])}"
            for n in sorted(d)
        )

    if pos and not neg:
        return _terms(pos)
    if neg and not pos:
        return "*".join(
            f"{n}^-{_fmt_exp(neg[n])}" for n in sorted(neg)
        )
    den = _terms(neg)
    if len(neg) > 1:
        return f"{_terms(pos)}/({den})"
    return f"{_terms(pos)}/{den}"


def dim_signature(dim: DimVector) -> str:
    """Canonical dimension signature string, e.g.
    `{"current": -2, "length": 2, "mass": 1, "time": -3}` for ohm.
    Replaces the old Mathematica `UnitDimensions` InputForm string.
    """
    sig = {}
    for name, e in zip(_DIM_NAMES, dim):
        if e != 0:
            sig[name] = (e.numerator if e.denominator == 1
                         else float(e))
    return json.dumps(sig, sort_keys=True)


# COMSOL-built-in functions that return a dimensionless scalar regardless
# of their arguments. For dimensional analysis we treat them as producing
# `1`. COMSOL's own analyser does the same.
_DIMLESS_FUNCS = frozenset({
    "tri_wave", "square_wave", "sawtooth_wave",
    "step", "heaviside", "flc1hs", "flc2hs",
    "flsmhs", "flsmsign", "rand", "random",
    "noise", "pulse", "ramp",
})

# Built-in constants: name → (dimension, statically-known numeric value
# or None). Case-sensitive. The caller's symbol table always overrides
# these (domain-agnostic promise — a model may redefine `x` or `pi`).
_CONST_DIMS: dict[str, tuple[DimVector, float | None]] = {
    "pi":    (_D0, math.pi),
    "e":     (_D0, math.e),
    "eps0":  (_dim(mass=-1, length=-3, time=4, current=2), None),  # F/m
    "mu0":   (_dim(mass=1, length=1, time=-2, current=-2), None),  # H/m
    "Z0":    (_dim(mass=1, length=2, time=-3, current=-2), None),  # ohm
    "inf":   (_D0, None),
    "Inf":   (_D0, None),
    "nan":   (_D0, None),
    "true":  (_D0, None),
    "false": (_D0, None),
    # Spatial coordinates — always `m` in COMSOL regardless of naming
    # convention. Derivatives like `d(u, x)` therefore carry `unit(u)/m`.
    "x": (_dim(length=1), None),
    "y": (_dim(length=1), None),
    "z": (_dim(length=1), None),
    "r": (_dim(length=1), None),
    # Time — always `s`.
    "t": (_dim(time=1), None),
}


def _canonicalise_comsol_unit(unit: str) -> str:
    """Canonicalise a COMSOL unit expression into a Mathematica-friendly form.

    Reuses the COMSOL expression parser on the unit string itself
    (identifiers = unit names), reduces to a `{name: signed_exponent}`
    dictionary using unit algebra, and re-emits as a product of
    power terms (`m^-1*s`, `kg*m^2*s^-3*A^-1`, …). Mathematica 14
    parses that form reliably.

    Why this is needed:
    - Mathematica rejects plain `"1/m"` (parses the `1` as a unit name).
    - Greedy regex rewrites produce wrong results for `1/m*s` (which
      COMSOL reads as `s/m`, not `(m*s)^-1`).
    - A parser-based reduction respects operator precedence and handles
      arbitrary nesting safely.

    Falls back to the input on parse failure; Wolfram will then try
    its own heuristics. This keeps the path robust even when the
    input is outside the supported grammar.
    """
    stripped = (unit or "").strip()
    if not stripped:
        return ""
    try:
        ast = parse_comsol(stripped)
        exponents = _reduce_unit_ast(ast)
    except (DimensionalError, _UnitReduceError):
        return stripped
    # Drop zero exponents — `m/m`, `(ohm*m)/(ohm*m)`, etc. cancel to
    # dimensionless. Emit "" so the translator can degrade to a bare
    # scalar rather than `Quantity[_, ""]` or `Quantity[_, "/"]`.
    exponents = {n: e for n, e in exponents.items() if e != 0}
    if not exponents:
        return ""
    # Separate positive and negative exponents. Mathematica reliably
    # parses `num/denom` forms and compound `X^-N` forms — but some
    # unit names (notably `H` for Henries) ambiguate when written as
    # `H*m^-1`; the `/` separator disambiguates to Henries. Emitting
    # `num/denom` preserves that context.
    pos = {n: e for n, e in exponents.items() if e > 0}
    neg = {n: -e for n, e in exponents.items() if e < 0}
    num = _format_unit_terms(pos) if pos else ""
    den = _format_unit_terms(neg) if neg else ""
    if num and not den:
        return num
    if den and not num:
        # All-negative case — emit as compound X^-e product.
        # Mathematica parses `m^-1` and `m^-1*s^-1` correctly.
        return "*".join(
            f"{n}^-{e}" if e != 1 else f"{n}^-1"
            for n, e in sorted(neg.items())
        )
    # Both present: `num/denom`, parenthesise denom when compound.
    if len(neg) > 1:
        return f"{num}/({den})"
    return f"{num}/{den}"


def _format_unit_terms(terms: dict) -> str:
    """Render `{name: positive_exponent}` as a product string."""
    parts = []
    for name in sorted(terms):
        e = terms[name]
        if e == 1:
            parts.append(name)
        else:
            parts.append(f"{name}^{e}")
    return "*".join(parts)


class _UnitReduceError(Exception):
    """Raised when a unit AST contains something that isn't legal unit
    algebra (e.g. a function call, non-numeric exponent)."""


def _reduce_unit_ast(node: object) -> dict[str, int | float]:
    """Walk a COMSOL expression AST as if it were a unit expression,
    returning `{unit_name: signed_exponent}`.

    Supports: identifiers, numeric `1`, `*`, `/`, `^<integer>`, and
    parenthesised sub-expressions. Anything else raises.
    """
    if isinstance(node, NumLit):
        if node.unit is not None:
            raise _UnitReduceError("nested unit annotation in unit string")
        if node.value == "1":
            return {}
        raise _UnitReduceError(f"non-1 numeric literal: {node.value!r}")
    if isinstance(node, Ident):
        return {node.name: 1}
    if isinstance(node, UnaryMinus):
        raise _UnitReduceError("unary minus not valid in unit algebra")
    if isinstance(node, Call):
        raise _UnitReduceError(f"function {node.name!r} not valid in unit")
    if isinstance(node, BinOp):
        if node.op == "*":
            out = dict(_reduce_unit_ast(node.left))
            for k, v in _reduce_unit_ast(node.right).items():
                out[k] = out.get(k, 0) + v
            return out
        if node.op == "/":
            out = dict(_reduce_unit_ast(node.left))
            for k, v in _reduce_unit_ast(node.right).items():
                out[k] = out.get(k, 0) - v
            return out
        if node.op == "^":
            base = _reduce_unit_ast(node.left)
            exp = _extract_numeric_exponent(node.right)
            return {k: v * exp for k, v in base.items()}
        raise _UnitReduceError(f"operator {node.op!r}")
    raise _UnitReduceError(f"unknown node {type(node).__name__}")


def _extract_numeric_exponent(node: object) -> int | float:
    if isinstance(node, NumLit) and node.unit is None:
        v = node.value
        if re.fullmatch(r"-?\d+", v):
            return int(v)
        return float(v)
    if isinstance(node, UnaryMinus):
        return -_extract_numeric_exponent(node.operand)
    raise _UnitReduceError("non-numeric exponent in unit expression")


# ---- AST evaluation (replaces the Mathematica translation + bridge) ----

@dataclass
class _EvalState:
    """Mutable per-expression evaluation state."""
    unresolved: list[str] = field(default_factory=list)
    unknown_units: list[str] = field(default_factory=list)
    inconsistent: bool = False


@dataclass
class _Val:
    """A dimensional value: dimension (None = could not be reduced), an
    optional statically-known dimensionless numeric magnitude (used
    only for exponents; mirrors the old translator's substitution of
    `1` for symbols, so `m^(n-1)` still reduces while W1 flags it), and
    the SI scale factor of the carried unit (distinguishes `ohm*cm`
    from `ohm*m` in deduced strings; never used for compatibility)."""
    dim: DimVector | None
    num: float | None = None
    scale: float = 1.0


# Transcendental functions: dimensionless result, and the argument must
# itself be dimensionless (a dimensional argument leaves the expression
# unresolved — matching the old Wolfram behavior where Exp[Quantity]
# never reduced).
_TRANSCENDENTAL = frozenset({
    "exp", "log", "log10", "sin", "cos", "tan",
    "asin", "acos", "atan", "sinh", "cosh", "tanh",
})


def _evaluate(node: Any, table: dict[str, str], st: _EvalState) -> _Val:
    """Reduce an AST to a dimension under `table` (name → unit text).

    Semantics mirror the retired Wolfram driver:
    - unknown identifiers → dimensionless `1`, recorded in
      `st.unresolved`;
    - `+`/`-` of resolved-but-different dimensions → inconsistent;
    - a `^` exponent uses the statically-known numeric value when the
      exponent is dimensionless (symbols contribute `1`);
    - unknown unit NAMES (in brackets or the symbol table) leave the
      expression unresolved and are recorded in `st.unknown_units` —
      an explicit diagnostic the Wolfram path lacked.
    """
    if isinstance(node, NumLit):
        if node.unit is None:
            try:
                return _Val(_D0, float(node.value))
            except ValueError:
                return _Val(_D0, None)
        dim, scale, unknown = unit_expr_dim_scale(node.unit)
        st.unknown_units.extend(unknown)
        # Magnitude is irrelevant once a unit is attached (and must not
        # leak into exponent arithmetic).
        return _Val(dim, None, scale)

    if isinstance(node, Ident):
        # Caller-supplied table wins over built-in constants.
        unit = table.get(node.name)
        if unit is None and node.name in _CONST_DIMS:
            dim, num = _CONST_DIMS[node.name]
            return _Val(dim, num)
        if unit is None:
            # Unresolved reference — dimensionless 1 so the rest of the
            # expression still reduces; flagged for the caller.
            st.unresolved.append(node.name)
            return _Val(_D0, 1.0)
        if unit == "":
            # Declared dimensionless.
            return _Val(_D0, 1.0)
        dim, scale, unknown = unit_expr_dim_scale(unit)
        st.unknown_units.extend(unknown)
        return _Val(dim, None, scale)

    if isinstance(node, UnaryMinus):
        v = _evaluate(node.operand, table, st)
        return _Val(v.dim, None if v.num is None else -v.num)

    if isinstance(node, Call):
        return _evaluate_call(node, table, st)

    if isinstance(node, BinOp):
        a = _evaluate(node.left, table, st)
        b = _evaluate(node.right, table, st)
        if node.op in ("+", "-"):
            if a.dim is None or b.dim is None:
                return _Val(None)
            if a.dim != b.dim:
                st.inconsistent = True
                return _Val(None)
            num = None
            if a.num is not None and b.num is not None:
                num = a.num + b.num if node.op == "+" else a.num - b.num
            # +/- of same-dimension values: keep the left term's scale
            # (any choice is a rendering convention, not a check).
            return _Val(a.dim, num, a.scale)
        if node.op == "*":
            if a.dim is None or b.dim is None:
                return _Val(None)
            num = (a.num * b.num
                   if a.num is not None and b.num is not None else None)
            return _Val(_dim_mul(a.dim, b.dim), num, a.scale * b.scale)
        if node.op == "/":
            if a.dim is None or b.dim is None:
                return _Val(None)
            num = None
            if a.num is not None and b.num is not None and b.num != 0:
                num = a.num / b.num
            scale = a.scale / b.scale if b.scale else 1.0
            return _Val(_dim_div(a.dim, b.dim), num, scale)
        if node.op == "^":
            # Exponent must be a statically-known dimensionless number
            # for a dimensional base. (Symbols contributed `1`, so the
            # common `m^(n-1)` still reduces — W1 flags it separately.)
            if a.dim is not None and a.dim == _D0:
                num = None
                if (a.num is not None and b.num is not None
                        and b.dim == _D0):
                    try:
                        num = float(a.num ** b.num)
                    except (OverflowError, ValueError, ZeroDivisionError):
                        num = None
                return _Val(_D0, num)
            if a.dim is None:
                return _Val(None)
            if b.dim != _D0 or b.num is None:
                return _Val(None)
            frac = Fraction(b.num).limit_denominator(1_000_000)
            try:
                scale = a.scale ** float(frac)
            except (OverflowError, ZeroDivisionError, ValueError):
                scale = 1.0
            return _Val(_dim_pow(a.dim, frac), None, scale)
        raise DimensionalError(f"unknown operator {node.op!r}")

    raise DimensionalError(f"cannot evaluate AST node {node!r}")


def _evaluate_call(node: Call, table: dict[str, str],
                   st: _EvalState) -> _Val:
    name = node.name

    if name == "__unit_cast__" and len(node.args) == 2:
        # `ident[unit]` — result carries exactly the cast's unit,
        # regardless of the identifier's own unit.
        return _evaluate(node.args[1], table, st)

    if name == "d" and len(node.args) == 2:
        # Derivative: unit(a) / unit(b).
        a = _evaluate(node.args[0], table, st)
        b = _evaluate(node.args[1], table, st)
        if a.dim is None or b.dim is None:
            return _Val(None)
        return _Val(_dim_div(a.dim, b.dim),
                    scale=(a.scale / b.scale if b.scale else 1.0))

    if name == "if" and len(node.args) == 3:
        # COMSOL guarantees both branches share units when valid; use
        # branch a's dimension. All three args are walked so unresolved
        # references in the condition / other branch still surface.
        _evaluate(node.args[0], table, st)
        a = _evaluate(node.args[1], table, st)
        _evaluate(node.args[2], table, st)
        return _Val(a.dim, scale=a.scale)

    if name in _DIMLESS_FUNCS:
        for a in node.args:
            _evaluate(a, table, st)
        # Dimensionless with magnitude 1 — mirrors the old translator,
        # which emitted a literal `1` for these calls.
        return _Val(_D0, 1.0)

    args = [_evaluate(a, table, st) for a in node.args]

    if name == "sqrt" and len(args) == 1:
        a = args[0]
        if a.dim is None:
            return _Val(None)
        num = None
        if a.num is not None and a.num >= 0 and a.dim == _D0:
            num = math.sqrt(a.num)
        scale = math.sqrt(a.scale) if a.scale > 0 else 1.0
        return _Val(_dim_pow(a.dim, Fraction(1, 2)), num, scale)

    if name == "abs" and len(args) == 1:
        a = args[0]
        return _Val(a.dim, None if a.num is None else abs(a.num),
                    a.scale)

    if name in ("max", "min") and args:
        dims = [a.dim for a in args]
        if any(d is None for d in dims):
            return _Val(None)
        if len({d for d in dims}) > 1:
            st.inconsistent = True
            return _Val(None)
        nums = [a.num for a in args]
        num = None
        if all(n is not None for n in nums):
            num = max(nums) if name == "max" else min(nums)
        return _Val(dims[0], num, args[0].scale)

    if name in _TRANSCENDENTAL and len(args) >= 1:
        a = args[0]
        if a.dim is None or a.dim != _D0:
            # Dimensional argument to exp/log/sin/… does not reduce.
            return _Val(None)
        return _Val(_D0)

    if name == "atan2" and len(args) == 2:
        a, b = args
        if a.dim is None or b.dim is None:
            return _Val(None)
        if a.dim != b.dim:
            st.inconsistent = True
            return _Val(None)
        return _Val(_D0)

    if name in ("floor", "ceil", "round") and len(args) == 1:
        return _Val(args[0].dim, scale=args[0].scale)

    if name == "sign" and len(args) == 1:
        return _Val(_D0)

    # Permissive passthrough for unknown functions: dimensionless when
    # every argument is dimensionless; otherwise the first argument's
    # dimension (correct for most COMSOL math functions on scalars).
    if args:
        if all(a.dim == _D0 for a in args):
            return _Val(_D0)
        return _Val(args[0].dim, scale=args[0].scale)
    return _Val(_D0)


# (The Mathematica translator `translate`, `_mma_symbol`, the
#  embedded Wolfram driver, and the `_run_wolfram` subprocess bridge
#  were removed — Layer C is now the pure-Python engine
#  above. See docs/linting.md.)


# ---- Top-level API ----

@dataclass
class AnalysisRequest:
    id: str
    expression: str           # COMSOL syntax
    expected_unit: str = ""   # COMSOL unit or ""
    # Optional per-request override of the default symbol table. Used
    # by `analyze_model` to supply a scope-specific view so same-named
    # variables in different scopes resolve to the right unit.
    symbol_table: dict[str, str] | None = None


def analyze(
    requests: list[AnalysisRequest],
    symbol_table: dict[str, str],
    *,
    wolframscript_path: str | None = None,
    timeout_s: int = 120,
) -> list[DimensionalFinding]:
    """Analyze a batch of expressions with the pure-Python engine.

    `wolframscript_path` and `timeout_s` are retained for backward
    compatibility with pre-1.0 callers/mocks and are ignored —
    the engine no longer shells out to anything.
    """
    del wolframscript_path, timeout_s  # back-compat, unused
    findings: list[DimensionalFinding] = []
    for r in requests:
        table = r.symbol_table if r.symbol_table is not None else symbol_table
        try:
            ast = parse_comsol(r.expression)
            w1_hits = find_non_integer_unit_powers(ast, table)
            st = _EvalState()
            val = _evaluate(ast, table, st)
        except DimensionalError as e:
            findings.append(DimensionalFinding(
                id=r.id, expression=r.expression,
                translation_error=str(e),
                expected_unit=r.expected_unit,
            ))
            continue

        f = DimensionalFinding(
            id=r.id, expression=r.expression,
            expected_unit=r.expected_unit,
        )
        f.resolved = val.dim is not None
        if val.dim is None:
            f.deduced_unit = "UNRESOLVED"
        else:
            base = dim_to_comsol_unit(val.dim)
            # A non-unit scale (ohm*cm vs ohm*m) is carried as a numeric
            # factor so deduced strings stay distinguishable — the
            # cross-scope conflict detector compares them. The factor
            # re-parses through unit_expr_dim_scale.
            if base and not math.isclose(val.scale, 1.0,
                                         rel_tol=1e-9, abs_tol=0.0):
                f.deduced_unit = f"{val.scale:.9g}*{base}"
            else:
                f.deduced_unit = base
        f.dimensions = (dim_signature(val.dim)
                        if val.dim is not None else "unresolved")
        f.inconsistent_arithmetic = st.inconsistent
        f.non_integer_power = bool(w1_hits)
        f.unresolved_symbols = sorted(set(st.unresolved))
        f.unknown_units = sorted(set(st.unknown_units))

        # Expected-unit comparison — skipped when the caller-supplied
        # string is empty/whitespace or cancels to dimensionless
        # (e.g. `m/m`), matching the historical contract. Compatibility
        # is dimension equality; an UNKNOWN expected-unit name reports
        # False (the Wolfram path's `Check[..., False]` did the same)
        # and surfaces the name in `unknown_units`.
        if r.expected_unit and f.resolved:
            canonical_exp = _canonicalise_comsol_unit(r.expected_unit)
            if canonical_exp:
                exp_dim, exp_unknown = unit_expr_dim(canonical_exp)
                if exp_dim is None:
                    f.expected_compatible = False
                    f.unknown_units = sorted(
                        set(f.unknown_units) | set(exp_unknown))
                else:
                    f.expected_compatible = (val.dim == exp_dim)

        findings.append(f)
    return findings


# ---- Symbol table builders ----

# COMSOL sometimes returns Unicode unit names from `evaluateUnit` (e.g.
# `Ω*m` rather than `ohm*m`). Mathematica accepts both but we normalise
# to ASCII for stability.
_UNICODE_UNIT_FIXES = {
    "Ω": "ohm",   # Ω
    "μ": "u",     # μ (micro) — COMSOL prefers `um`, `us`
    "°": "deg",   # °
}


def normalise_unit_string(u: str) -> str:
    if not u:
        return u
    for src, dst in _UNICODE_UNIT_FIXES.items():
        u = u.replace(src, dst)
    return u


def build_symbol_table(symbols_json: dict) -> dict[str, str]:
    """Flat `name → unit` view. Kept for backwards compatibility with
    callers that don't need scope-aware resolution.

    Prefer `SymbolResolver` for model-level analysis — it preserves
    per-scope information and surfaces cross-scope unit conflicts.
    """
    return SymbolResolver(symbols_json).flat_view()


class SymbolResolver:
    """Scope-aware name → unit resolver for COMSOL models.

    Real-world COMSOL models may define the same variable name in
    multiple scopes (e.g. `rho_val` in both `var_air` and `var_b`
    variable collections). The units may differ in principle, and an
    expression written in one scope refers to the definition active
    in that scope. Agentic builders can produce such sharding; human
    models do it frequently for physics-by-region.

    Design:
    - Params live at pseudo-scope `"param"` (globally visible).
    - Variables live at their source scope (e.g. `"comp1/var_b"`).
    - `view_for_scope(s)` returns a `name → unit` dict where
      same-scope variables override other-scope ones, with params
      always accessible as a fallback.
    - Fully-qualified references `short.name` (e.g. `var_b.rho_val`)
      resolve to the specific scope's definition.
    - `conflicts()` flags names that have DIFFERENT non-empty units
      across scopes — a genuine correctness issue.

    The design is intentionally conservative: when a bare name is
    ambiguous across scopes and the current scope doesn't define it,
    we pick the first available and record it as ambiguous in the
    returned view's unresolved_overloads.
    """

    def __init__(self, symbols_json: dict):
        # (scope, name) → unit (may be "" meaning "unit not yet known")
        self._entries: dict[tuple[str, str], str] = {}
        for s in symbols_json.get("params", []):
            self._entries[("param", s["name"])] = normalise_unit_string(
                s.get("unit", "") or "")
        for s in symbols_json.get("variables", []):
            key = (s["scope"], s["name"])
            # Don't overwrite — Layer C's multi-pass fills these in later.
            self._entries.setdefault(
                key, normalise_unit_string(s.get("unit", "") or ""))

    def update_variable(self, scope: str, name: str, unit: str) -> None:
        """Multi-pass feedback: record a deduced variable unit."""
        self._entries[(scope, name)] = unit

    def scopes(self) -> list[str]:
        return sorted({scope for (scope, _name) in self._entries})

    def variable_scopes(self) -> list[str]:
        return [s for s in self.scopes() if s != "param"]

    def flat_view(self) -> dict[str, str]:
        """Legacy flat view: name → unit, with params winning over
        variables and later-added variables winning over earlier."""
        view: dict[str, str] = {}
        for (scope, name), unit in self._entries.items():
            if scope != "param":
                view.setdefault(name, unit)
        for (scope, name), unit in self._entries.items():
            if scope == "param":
                view[name] = unit
        return view

    def view_for_scope(self, current_scope: str) -> dict[str, str]:
        """Symbol view an expression written in `current_scope` sees.

        Priority (highest first):
          1. Params (`param` scope) — globally visible, always win.
          2. Current-scope variables — local definitions.
          3. Other-scope variables — fall-through for convenience,
             first-seen-wins.
          4. Qualified names `<short_scope>.<name>` and
             `<full_scope>.<name>` — direct pointers into a specific
             scope's definition.
        """
        view: dict[str, str] = {}

        # (3) Other-scope fallbacks — registered first so later layers
        # overwrite them.
        for (scope, name), unit in self._entries.items():
            if scope == "param" or scope == current_scope:
                continue
            if name not in view:
                view[name] = unit

        # (2) Current scope.
        for (scope, name), unit in self._entries.items():
            if scope == current_scope:
                view[name] = unit

        # (1) Params win outright.
        for (scope, name), unit in self._entries.items():
            if scope == "param":
                view[name] = unit

        # (4) Qualified references. Preserved even when unit is empty
        # so the translator knows the name exists.
        for (scope, name), unit in self._entries.items():
            if scope == "param":
                continue
            short = scope.rsplit("/", 1)[-1]  # comp1/var_b → var_b
            view.setdefault(f"{short}.{name}", unit)
            view.setdefault(f"{scope}.{name}", unit)

        return view

    def conflicts(self) -> list[dict]:
        """Variables with the same name but different non-empty units
        across scopes. One entry per conflicting name."""
        by_name: dict[str, dict[str, str]] = {}
        for (scope, name), unit in self._entries.items():
            if scope == "param":
                continue
            by_name.setdefault(name, {})[scope] = unit
        out: list[dict] = []
        for name, by_scope in by_name.items():
            distinct = {u for u in by_scope.values() if u}
            if len(distinct) > 1:
                out.append({
                    "name": name,
                    "units_by_scope": dict(by_scope),
                    "distinct_units": sorted(distinct),
                })
        return out


def _unit_string_from_finding(f: DimensionalFinding) -> str:
    """Extract a usable unit string from a resolved finding.

    The engine renders `deduced_unit` directly in
    canonical COMSOL-style SI base form (`m/s`, `kg*m^2/(A^2*s^3)`),
    which re-parses through the unit table — no Mathematica-name
    round-trip needed for the multi-pass feedback.
    """
    if not f.resolved or f.deduced_unit in ("", "UNRESOLVED"):
        return ""
    return f.deduced_unit.strip()


def analyze_model(
    symbols_json: dict,
    expected_overrides: dict[str, str] | None = None,
    *,
    slot_records: list[dict] | None = None,
    wolframscript_path: str | None = None,   # back-compat, ignored
    timeout_s: int = 180,                    # back-compat, ignored
    max_passes: int = 3,
) -> list[DimensionalFinding]:
    """Run Layer C across all params, variables, and optional slots.

    Multi-pass: each pass feeds the previous pass's deduced variable
    units back into the symbol table. This lets later-defined variables
    resolve once their dependencies have been reduced. Stops when a
    pass produces no new resolutions or `max_passes` is reached.

    `expected_overrides` maps `<kind>:<scope>:<name>` to an expected
    unit string. When present, the deduced unit is compared against it
    via `CompatibleUnitQ`. The `kind` segment is one of ``param``,
    ``variable``, or ``slot``.

    `slot_records` is an optional list of per-slot dicts from the Phase 1
    harvester's per-slot output. Each dict must carry ``expression``,
    ``physics_tag``, ``feature_tag``, and ``slot_property``; other fields
    are ignored. Slot expressions are added to the analysis alongside
    params and variables. Use
    ``slot_catalog.build_expected_overrides_from_slots`` to pre-populate
    the override dict with catalog-derived expected units.
    """
    expected_overrides = expected_overrides or {}
    slot_records = slot_records or []
    resolver = SymbolResolver(symbols_json)

    def _build_requests() -> list[AnalysisRequest]:
        reqs: list[AnalysisRequest] = []
        for s in symbols_json.get("params", []):
            if not s.get("expression"):
                continue
            key = f"param:{s['scope']}:{s['name']}"
            reqs.append(AnalysisRequest(
                id=key,
                expression=s["expression"],
                expected_unit=expected_overrides.get(key, ""),
                symbol_table=resolver.view_for_scope("param"),
            ))
        for s in symbols_json.get("variables", []):
            if not s.get("expression"):
                continue
            key = f"variable:{s['scope']}:{s['name']}"
            reqs.append(AnalysisRequest(
                id=key,
                expression=s["expression"],
                expected_unit=expected_overrides.get(key, ""),
                symbol_table=resolver.view_for_scope(s["scope"]),
            ))
        # Slot requests — expected_overrides key pattern mirrors the
        # scheme used by build_expected_overrides_from_slots.
        for s in slot_records:
            expr = s.get("expression")
            if not expr:
                continue
            physics_tag = s.get("physics_tag", "?")
            feature_tag = s.get("feature_tag", "?")
            slot_property = s.get("slot_property", "")
            if not slot_property:
                continue
            key = f"slot:{physics_tag}/{feature_tag}:{slot_property}"
            # Component-scoped symbol table when the harvester emitted
            # a component_tag (multi-component models where
            # a variable name exists with different units in different
            # components). Prefix-match the resolver's known scopes so
            # `comp1` matches `comp1/var_b`, `comp1/var_post`, etc.
            comp_tag = s.get("component_tag") or ""
            table = resolver.flat_view()
            if comp_tag:
                merged: dict[str, str] = {}
                for scope in resolver.variable_scopes():
                    if scope.startswith(comp_tag + "/") or scope == comp_tag:
                        # view_for_scope returns a flat view keyed by
                        # variable name; later scopes overwrite earlier
                        # ones but within a single component that's
                        # the expected behaviour.
                        merged.update(resolver.view_for_scope(scope))
                if merged:
                    # Preserve params under the merged view.
                    merged_with_params = dict(resolver.view_for_scope("param"))
                    merged_with_params.update(merged)
                    table = merged_with_params
            reqs.append(AnalysisRequest(
                id=key,
                expression=expr,
                expected_unit=expected_overrides.get(key, ""),
                symbol_table=table,
            ))
        return reqs

    if not _build_requests():
        return []

    findings: list[DimensionalFinding] = []
    last_resolved = -1
    for _pass in range(max_passes):
        findings = analyze(
            _build_requests(),
            # An empty default table — each request carries its own
            # scope-specific view via `AnalysisRequest.symbol_table`.
            {},
        )
        new_resolved = sum(1 for f in findings if f.resolved)
        if new_resolved == last_resolved:
            break
        last_resolved = new_resolved
        # Feed resolved variable units back into the resolver.
        for f in findings:
            parts = f.id.split(":", 2)
            if len(parts) != 3 or parts[0] != "variable":
                continue
            _, scope, name = parts
            unit = _unit_string_from_finding(f)
            if unit:
                resolver.update_variable(scope, name, unit)

    # Annotate findings with the detected cross-scope conflicts so
    # `findings_to_sidecar` can surface them.
    conflicts = resolver.conflicts()
    if conflicts:
        # Attach a sentinel finding per conflict so the sidecar stage
        # can emit them without a second return channel.
        for c in conflicts:
            findings.append(DimensionalFinding(
                id=f"scope_conflict:{c['name']}",
                expression="",
                resolved=False,
                translation_error="",
                # Stash the conflict payload in deduced_unit for the
                # sidecar classifier to unpack — simplest carrier.
                deduced_unit=json.dumps({
                    "scope_conflict": True,
                    "name": c["name"],
                    "units_by_scope": c["units_by_scope"],
                    "distinct_units": c["distinct_units"],
                }),
            ))
    return findings


def findings_to_sidecar(findings: list[DimensionalFinding]) -> dict:
    """Shape findings for the layer_c section of the units sidecar."""
    warnings = []
    for f in findings:
        # Scope-conflict sentinels (emitted by analyze_model) carry the
        # payload encoded in `deduced_unit` as JSON. Detect and unpack.
        if f.id.startswith("scope_conflict:") and f.deduced_unit \
                and f.deduced_unit.startswith("{"):
            try:
                payload = json.loads(f.deduced_unit)
            except json.JSONDecodeError:
                payload = {}
            if payload.get("scope_conflict"):
                warnings.append({
                    "id": f.id,
                    "kind": "scope_conflict",
                    "name": payload.get("name", ""),
                    "units_by_scope": payload.get("units_by_scope", {}),
                    "distinct_units": payload.get("distinct_units", []),
                    "detail": (
                        f"variable {payload.get('name')!r} has "
                        f"different deduced units across scopes: "
                        f"{payload.get('distinct_units')}"
                    ),
                })
                continue
        if f.translation_error:
            warnings.append({
                "id": f.id, "kind": "translation_error",
                "expression": f.expression, "detail": f.translation_error,
            })
            continue
        if f.non_integer_power:
            warnings.append({
                "id": f.id, "kind": "non_integer_power",
                "expression": f.expression,
                "detail": "exponent on a unit-bearing base is not a "
                          "literal integer — COMSOL-style W1 class.",
            })
        if f.inconsistent_arithmetic:
            warnings.append({
                "id": f.id, "kind": "inconsistent_arithmetic",
                "expression": f.expression,
                "detail": "Plus/Subtract of dimensionally incompatible "
                          "terms.",
            })
        if f.unknown_units:
            warnings.append({
                "id": f.id, "kind": "unknown_unit",
                "expression": f.expression,
                "unknown_units": list(f.unknown_units),
                "detail": "unit name(s) not in the engine's table: "
                          f"{', '.join(f.unknown_units)} — check for a "
                          "typo, or extend _UNIT_DIMS in dimensional.py.",
            })
        if not f.resolved and not f.non_integer_power \
                and not f.inconsistent_arithmetic and not f.unknown_units:
            warnings.append({
                "id": f.id, "kind": "unresolved_dimensions",
                "expression": f.expression,
                "detail": "the dimensional engine could not reduce "
                          "this to a concrete dimension signature.",
            })
        if f.expected_compatible is False:
            warnings.append({
                "id": f.id, "kind": "expected_mismatch",
                "expression": f.expression,
                "expected": f.expected_unit,
                "deduced": f.deduced_unit,
                "detail": f"deduced unit not compatible with expected "
                          f"{f.expected_unit!r}.",
            })
    return {
        "warnings": warnings,
        "findings": [asdict(f) for f in findings],
        "generated_at": datetime.now(timezone.utc)
            .strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
