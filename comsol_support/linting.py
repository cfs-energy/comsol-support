"""linting — Layer A static scan of COMSOL Java builders.

Cheap deterministic checks for the two error classes worker agents
commonly introduce into `.java` builders:

- Unit-bracket typos: `[m]/[s]`, `/[s]`, `[m+s]`, unicode in brackets.
- Missing or placeholder descriptions on params and variables.

Pure Python. No COMSOL dependency. Emits a LintReport that can be
serialised to the linting sidecar JSONs defined in the plan.

Layer B (runtime unit analysis via the Java API) lives in
`comsol_support/java/ModelChecker.java` and is narrower in scope than
originally planned — see `comsol_linting_research.md`.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import subprocess
import sys
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger("comsol_support.linting")

CHECKER_SOURCE = "ModelChecker.java"
CHECKER_CLASS = "ModelChecker"

# Forward-imported lazily in cmd_check, re-exported here as a hint for
# (Layer C is pure Python — no Wolfram import needed.)


# ---- Data ----

@dataclass
class UnitFinding:
    file: str
    line: int
    pattern: str  # A1..A5
    expression: str
    suggested_fix: str


@dataclass
class DescrFinding:
    file: str
    line: int
    kind: str  # "param" | "variable"
    name: str
    reason: str  # "missing" | "placeholder"
    descr: str = ""


@dataclass
class LintReport:
    source_path: str
    unit_warnings: list[UnitFinding] = field(default_factory=list)
    descr_warnings: list[DescrFinding] = field(default_factory=list)

    def to_units_sidecar(self) -> dict:
        return {
            "layer_a": {
                "warnings": [asdict(w) for w in self.unit_warnings],
            },
            "source_path": self.source_path,
            "generated_at": _utc_now_iso(),
        }

    def to_descriptions_sidecar(self) -> dict:
        missing = [asdict(w) for w in self.descr_warnings
                   if w.reason == "missing"]
        placeholder = [asdict(w) for w in self.descr_warnings
                       if w.reason == "placeholder"]
        return {
            "layer_a": {
                "missing_in_source": missing,
                "placeholder_in_source": placeholder,
            },
            "source_path": self.source_path,
            "generated_at": _utc_now_iso(),
        }


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---- Unit patterns ----

# We scan only inside Java string literals — that's where COMSOL
# expressions live. The literal-finder is intentionally simple: it
# matches `"..."` without handling escaped quotes, which is adequate
# for COMSOL expressions (they do not embed `"`).
_JAVA_STRING_LITERAL = re.compile(r'"([^"\\]*)"')

# A1: bracket-operator-bracket, e.g. `[m]/[s]`, `[V]*[A]`.
_P_A1 = re.compile(r"\][*/]\s*\[")

# A2: operator immediately before `[` with no preceding quantity,
# e.g. `foo * [m]`, `/[s]`. The char class excludes digits, `)`, `]`,
# and letters/underscore (identifiers) — those would be valid
# numerator quantities.
_P_A2 = re.compile(r"(?:^|[+\-*/(=,\s])[*/]\s*\[")

# A3: bracket without a preceding numeric/identifier quantity, e.g.
# `[m]` alone at expression start or after `(`. More permissive than
# A2 — it catches the bracket even when there is no operator.
_P_A3 = re.compile(r"(?:^|[=,(\s])\[[A-Za-z]")

# A4: `+` or `-` inside a bracket group, e.g. `[m+s]`. Almost always
# a typo (unit brackets should contain one unit expression).
_P_A4 = re.compile(r"\[[^\]\s]*[+\-][^\]]*\]")

# A5: non-ASCII character inside a bracket group (Ω, μ, °, etc.).
_P_A5 = re.compile(r"\[[^\]]*[^\x00-\x7F][^\]]*\]")

# A6: physics interface created without an accompanying unit-slot set
# call. Fires when one of the user-PDE / DAE classes is created via
# `physics().create("<tag>", "<class>", ...)` and no
# `physics("<tag>").prop("Units").set("DependentVariableQuantity", ...)`
# (or "CustomDependentVariableUnit", "SourceTermQuantity",
# "CustomSourceTermUnit") appears within ``_A6_WINDOW_LINES`` source
# lines after.
#
# WHY: leaving these slots unset makes COMSOL fall back to dimensionless
# defaults (1/m^2 source, no DoF unit), which silently mis-types the
# residual. The 3D_H-phi_pde reference and other corpus models pin
# these slots explicitly; we mirror that contract here.
_P_A6_CREATE = re.compile(
    r'\.physics\(\)\.create\(\s*"([A-Za-z][\w]*)"\s*,\s*'
    r'"(GeneralFormPDE|WeakFormPDE|CoefficientFormPDE|GlobalEquations)"'
)
# How many subsequent lines from the create() call we tolerate before
# the unit slot must appear. 80 is empirically large enough for typical
# builders (variables / cpl / params interleave) without tolerating
# absent slots silently.
_A6_WINDOW_LINES = 80
# The four user-settable unit slots. Matching ANY of them on the
# correct tag clears A6.
_A6_UNIT_SLOTS = (
    "DependentVariableQuantity",
    "CustomDependentVariableUnit",
    "SourceTermQuantity",
    "CustomSourceTermUnit",
)
# Explicit user opt-out marker (mirrors how A3 supports a similar
# escape hatch via a magic comment on the literal's surrounding line).
_A6_SKIP_COMMENT = "// LINT-A6: skip"
# Pattern detecting an iterator-loop discharge:
#     for (String <var> : new String[] {"g1", "g2", ...}) { ... physics(<var>).prop("Units").set("...", ...); ... }
# When a tag is mentioned in the loop's String[] body and the loop body
# touches a unit slot via `physics(<var>).prop("Units")`, that tag is
# discharged.
_P_A6_FORLOOP = re.compile(
    r'for\s*\(\s*(?:final\s+)?String\s+(\w+)\s*:\s*new\s+String\s*\[\s*\]\s*'
    r'\{([^}]*)\}',
    re.DOTALL,
)

# Literals that are *only* a single bracket group with optional whitespace
# around it. These are the Java-side suffix half of a concatenation like
# `WIDTH + "[m]"` and are NOT standalone COMSOL expressions — skip them.
_SUFFIX_ONLY = re.compile(r"^\s*\[[^\]]+\]\s*$")

# Log-prefix literals — e.g. `"[cycle36 W1 GaSignCheck] total wall: "`.
# These are println/log tags, not COMSOL expressions: the bracket at the
# start is a string tag, the colon terminates a label, and the rest is
# free prose. Suppress only pattern A3 on these (the weaker "bracket
# without preceding quantity" rule). Keep A1/A2/A4/A5 active in case a
# bracketed unit typo is buried in a log literal — those are strong
# signals and rarely false-positive.
_LOG_PREFIX = re.compile(r"^\s*\[[^\]]+\].*:\s*")


def _scan_unit_literal(literal: str) -> list[tuple[str, str]]:
    """Return a list of (pattern_id, suggested_fix) for one literal.

    Multiple patterns may fire on the same literal.
    """
    hits: list[tuple[str, str]] = []
    if _P_A1.search(literal):
        fix = _suggest_merge_brackets(literal)
        hits.append(("A1", fix))
    if _P_A2.search(literal) and not _P_A1.search(literal):
        hits.append(("A2", "prefix the bracketed unit with a quantity, "
                           "e.g. `1[m]` or `x*1[m]`"))
    if _P_A4.search(literal):
        hits.append(("A4", "remove `+`/`-` inside unit brackets; split into "
                           "separate bracketed groups if needed"))
    # A3 is coarser than A1/A2/A4 — only emit it if none of them fired,
    # so we don't double-report the same typo. Also skip log-prefix
    # literals: `"[tag] text: "` is a
    # println/log tag, not a COMSOL expression.
    if (_P_A3.search(literal)
            and not (_P_A1.search(literal) or _P_A2.search(literal)
                     or _P_A4.search(literal))
            and not _LOG_PREFIX.match(literal)):
        hits.append(("A3", "every unit bracket needs a preceding quantity, "
                           "e.g. `1[m]` instead of `[m]`"))
    if _P_A5.search(literal):
        hits.append(("A5", "use ASCII unit names: `[ohm]` not `[Ω]`, "
                           "`[um]` not `[μm]`, `[degC]` not `[°C]`"))
    return hits


def _suggest_merge_brackets(literal: str) -> str:
    """Rewrite `[a]<op>[b]` → `[a<op>b]` in the hint text."""
    return re.sub(r"\[([^\]]+)\]([*/])\[([^\]]+)\]",
                  r"[\1\2\3]", literal)


# ---- Description patterns ----

# Matches `.param().set("name", ...)` and `.variable("tag").set("name", ...)`.
# The .set(...) inside .variable() is an ExpressionEntity.set; the outer
# `.variable("tag")` locates the collection. We accept multi-line calls by
# walking the text and looking for the first `(` after `.set` up to its
# matching `)`.
_CALL_PARAM_SET = re.compile(
    r"\.param\(\s*\)\s*\.set\s*\(", re.MULTILINE,
)
_CALL_VARIABLE_SET = re.compile(
    r"\.variable\(\s*\"([^\"]+)\"\s*\)\s*(?:\s*\.\s*[A-Za-z_]\w*\([^)]*\))*"
    r"\s*\.set\s*\(",
    re.MULTILINE | re.DOTALL,
)

# `.descr("name", "...")` — any subject, param or variable.
_CALL_DESCR = re.compile(
    r"\.descr\s*\(\s*\"([^\"]+)\"\s*,\s*\"([^\"]*)\"\s*\)",
    re.MULTILINE,
)

# 3-arg form of set: set("name", "expr", "descr") — provides descr inline.
# Arguments are split on top-level commas (string- and paren-aware), then
# each is classified as a pure string literal or a Java expression. This
# keeps a non-literal middle argument (String.valueOf(N), "a"+suffix, …)
# from shifting the description slot — see LINT_BACKLOG entry 1.
_PURE_STR_LITERAL = re.compile(r'^"((?:[^"\\]|\\.)*)"$')

_PLACEHOLDER = re.compile(r"^\s*(todo|tbd|xxx|pending|fixme|\.\.\.|\?+|n/a)"
                          r"\s*$", re.IGNORECASE)


def _find_matching_paren(text: str, open_idx: int) -> int:
    """Return index of `)` matching the `(` at open_idx, skipping strings.

    Returns -1 if unmatched.
    """
    depth = 0
    i = open_idx
    n = len(text)
    while i < n:
        c = text[i]
        if c == '"':
            # Skip string literal
            i += 1
            while i < n and text[i] != '"':
                if text[i] == "\\":
                    i += 2
                    continue
                i += 1
            i += 1
            continue
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return -1


def _split_top_level_args(inside: str) -> list[str]:
    """Split a call's argument text on top-level commas.

    String- and char-literal aware; commas nested in (), [], {} or inside
    quotes do not split. Returns the raw argument expressions, stripped.
    """
    args: list[str] = []
    buf: list[str] = []
    depth = 0
    i = 0
    n = len(inside)
    while i < n:
        c = inside[i]
        if c in ('"', "'"):
            quote = c
            buf.append(c)
            i += 1
            while i < n:
                buf.append(inside[i])
                if inside[i] == "\\" and i + 1 < n:
                    buf.append(inside[i + 1])
                    i += 2
                    continue
                if inside[i] == quote:
                    i += 1
                    break
                i += 1
            continue
        if c in "([{":
            depth += 1
        elif c in ")]}":
            depth -= 1
        elif c == "," and depth == 0:
            args.append("".join(buf).strip())
            buf = []
            i += 1
            continue
        buf.append(c)
        i += 1
    tail = "".join(buf).strip()
    if tail or args:
        args.append(tail)
    return args


def _literal_content(raw_arg: str) -> str | None:
    """Return the content of a pure string-literal argument, else None.

    `"abc"` → `abc`; `String.valueOf(N)`, `"a"+x`, `DESCR_CONST` → None.
    """
    m = _PURE_STR_LITERAL.match(raw_arg.strip())
    return m.group(1) if m else None


def _extract_set_call(text: str, set_open_paren_idx: int) -> tuple[int, list[str]] | None:
    """Given the index of the `(` after `.set`, return (line, raw_args).

    raw_args is the list of top-level argument expressions in order
    (string literals still quoted) — classify with `_literal_content`.
    """
    close = _find_matching_paren(text, set_open_paren_idx)
    if close < 0:
        return None
    inside = text[set_open_paren_idx + 1:close]
    args = _split_top_level_args(inside)
    line = text.count("\n", 0, set_open_paren_idx) + 1
    return line, args


def _scan_descriptions(path: Path, text: str) -> list[DescrFinding]:
    """Find missing / placeholder descriptions for params and variables."""
    findings: list[DescrFinding] = []

    # Collect all .descr("name", "...") so we know which names have
    # a description via a separate call.
    descr_map: dict[str, str] = {}
    for m in _CALL_DESCR.finditer(text):
        name, descr = m.group(1), m.group(2)
        # First occurrence wins — later overrides, but for "is it defined
        # at all" the first is enough.
        descr_map.setdefault(name, descr)

    def _record(kind: str, name: str, descr: str | None, line: int) -> None:
        if descr is None or descr == "":
            findings.append(DescrFinding(
                file=str(path), line=line, kind=kind,
                name=name, reason="missing", descr=""))
        elif _PLACEHOLDER.match(descr) or len(descr.strip()) < 3:
            findings.append(DescrFinding(
                file=str(path), line=line, kind=kind,
                name=name, reason="placeholder", descr=descr))

    def _scan_setter(kind: str, call_re: re.Pattern) -> None:
        for m in call_re.finditer(text):
            open_idx = m.end() - 1  # position of `(`
            info = _extract_set_call(text, open_idx)
            if info is None:
                continue
            line, raw_args = info
            if not raw_args:
                continue
            name = _literal_content(raw_args[0])
            if name is None:
                # Dynamic name expression — nothing statically checkable.
                continue
            if len(raw_args) >= 3:
                descr = _literal_content(raw_args[2])
                if descr is not None:
                    _record(kind, name, descr, line)
                # else: description supplied as a non-literal Java
                # expression — presence is satisfied, content unknowable.
            else:
                _record(kind, name, descr_map.get(name), line)

    _scan_setter("param", _CALL_PARAM_SET)
    _scan_setter("variable", _CALL_VARIABLE_SET)

    return findings


# ---- Top-level ----

def scan_java_source(path: Path) -> LintReport:
    """Scan a single .java file for unit typos and description issues."""
    path = Path(path)
    text = path.read_text(encoding="utf-8", errors="replace")
    report = LintReport(source_path=str(path))

    # Unit scan — walk all string literals, tag with line numbers.
    for m in _JAVA_STRING_LITERAL.finditer(text):
        literal = m.group(1)
        if "[" not in literal:
            continue
        # Skip pure bracket-suffix literals like `"[m]"` or `" [m] "`:
        # these are almost always concatenated with a numeric quantity
        # (`WIDTH + "[m]"`) and the resulting COMSOL expression is valid.
        # Without this guard A3 floods with false positives.
        if _SUFFIX_ONLY.match(literal):
            continue
        hits = _scan_unit_literal(literal)
        if not hits:
            continue
        line = text.count("\n", 0, m.start()) + 1
        for pattern_id, fix in hits:
            report.unit_warnings.append(UnitFinding(
                file=str(path), line=line, pattern=pattern_id,
                expression=literal, suggested_fix=fix,
            ))

    # A6 scan — physics interfaces created without unit slots
    report.unit_warnings.extend(_scan_missing_unit_slots(path, text))

    # Description scan
    report.descr_warnings.extend(_scan_descriptions(path, text))

    return report


def _is_a6_discharged(window: str, tag: str) -> bool:
    """Return True if A6 should be silent for `tag` based on `window`.

    Three discharge paths (any one suffices):
    1. Explicit `// LINT-A6: skip` directive in the window.
    2. Tag-qualified slot set: a call like
       ``physics("<tag>").prop("Units").set("<UnitSlot>"`` or
       ``physics("<tag>").feature(...).set("<UnitSlot>"``.
    3. Loop-pattern discharge: a `for (String var : new String[]{...,
       "<tag>", ...})` whose body calls
       ``physics(var).prop("Units").set("<UnitSlot>"``. This catches the
       common idiom of setting unit slots for several physics
       interfaces with a single loop.
    """
    # 1. Skip directive
    if _A6_SKIP_COMMENT in window:
        return True
    # 2. Tag-qualified slot set (covers both prop("Units") and feature() variants)
    tag_anchor = f'physics("{tag}")'
    for slot in _A6_UNIT_SLOTS:
        if f'set("{slot}"' not in window or tag_anchor not in window:
            continue
        # Verify the slot-set occurs reasonably close to the tag anchor.
        # We allow up to 400 chars of intervening text (multi-line
        # builder calls split a single statement across several lines,
        # plus the .prop("Units") and the closing parens).
        slot_call = f'set("{slot}"'
        anchor_pos = 0
        while True:
            ap = window.find(tag_anchor, anchor_pos)
            if ap < 0:
                break
            sp = window.find(slot_call, ap)
            if 0 <= sp - ap <= 400:
                return True
            anchor_pos = ap + len(tag_anchor)
    # 3. For-loop discharge: an iterator over a String[] containing the tag
    for m in _P_A6_FORLOOP.finditer(window):
        var = m.group(1)
        body = m.group(2)
        # Tag must be one of the strings in the array literal.
        if f'"{tag}"' not in body:
            continue
        # Anywhere in the window AFTER the loop start, there must be a
        # `physics(var).prop("Units").set("<slot>"` or
        # `physics(var).feature(...).set("<slot>"` call.
        loop_tail = window[m.end():]
        var_anchor = f'physics({var})'
        if var_anchor not in loop_tail:
            # Loop body itself may carry the call (single-line loops).
            loop_tail = body
            if var_anchor not in loop_tail:
                continue
        for slot in _A6_UNIT_SLOTS:
            if f'set("{slot}"' in loop_tail:
                return True
    return False


def _scan_missing_unit_slots(path: Path, text: str) -> list[UnitFinding]:
    """Find ``physics().create(...)`` of user-PDE/DAE classes that have
    no accompanying ``prop("Units").set(...)`` within the next
    ``_A6_WINDOW_LINES`` lines of source. Emits A6 findings."""
    findings: list[UnitFinding] = []
    if "physics().create(" not in text:
        return findings
    lines = text.splitlines()
    n = len(lines)
    for m in _P_A6_CREATE.finditer(text):
        tag = m.group(1)
        cls = m.group(2)
        line_no = text.count("\n", 0, m.start()) + 1
        # Discharge window: a few lines before (where lint-suppress
        # comments commonly sit) plus _A6_WINDOW_LINES after the
        # create() call.
        start_line = max(1, line_no - 2)
        end_line = min(n, line_no + _A6_WINDOW_LINES)
        window = "\n".join(lines[start_line - 1:end_line])
        discharged = _is_a6_discharged(window, tag)
        if discharged:
            continue
        findings.append(UnitFinding(
            file=str(path),
            line=line_no,
            pattern="A6",
            expression=f'physics().create("{tag}", "{cls}", ...)',
            suggested_fix=(
                f'set unit slots on physics "{tag}" within '
                f'{_A6_WINDOW_LINES} lines: '
                'prop("Units").set("DependentVariableQuantity", ...) and '
                'set("CustomSourceTermUnit", ...) (or use the named '
                'quantity that maps to your DoF). Suppress with the '
                '`// LINT-A6: skip` comment if the omission is '
                'intentional.'
            ),
        ))
    return findings


# ---- Layer B bridge ---------------------------------------------------

class LayerBError(Exception):
    """Layer B (Java ModelChecker) failed."""


def run_layer_b(
    mph_path: Path,
    *,
    comsol_path: str | None = None,
    workspace_dir: Path | None = None,
    timeout_s: int = 300,
    emit_slots: bool = False,
    slots_version: str | None = None,
) -> tuple[dict, dict, dict] | tuple[dict, dict, dict, list[dict]]:
    """Compile (if needed) and run ModelChecker on an .mph file.

    By default returns `(units_layer_b, descriptions_layer_b, symbols)`.
    When `emit_slots=True`, ModelChecker additionally runs the
    SlotHarvester walk inside the SAME JVM session and returns a
    4-tuple `(units_layer_b, descriptions_layer_b, symbols, slot_records)`.
    This avoids the cost of a second COMSOL model-load for
    catalog-driven linting.

    `slots_version` tags emitted slot records with a COMSOL version
    string. If None, the Java side records "unknown".

    Raises LayerBError on failure.

    Imports from `comsol_support` are done lazily so that Layer A remains
    usable without a COMSOL install.
    """
    mph_path = Path(mph_path).resolve()
    if not mph_path.is_file():
        raise LayerBError(f"mph not found: {mph_path}")

    from comsol_support import COMSOL_PATH
    from comsol_support.java_facade import JavaFacade

    cp_path = comsol_path or COMSOL_PATH
    if workspace_dir is None:
        workspace_dir = mph_path.parent
    workspace_dir = Path(workspace_dir).resolve()
    workspace_dir.mkdir(parents=True, exist_ok=True)

    facade = JavaFacade(
        comsol_path=cp_path,
        workspace_dir=workspace_dir,
        java_source_dir=Path(__file__).parent / "java",
    )

    # Compile ModelChecker + SlotHarvester together if either .class is
    # absent or stale. SlotHarvester is referenced unconditionally from
    # ModelChecker.java (inside the `slotsOut != null` block, but the
    # symbol must resolve at compile time regardless of runtime
    # branching), so compiling ModelChecker alone fails with
    # "cannot find symbol SlotHarvester". Always compile both — the
    # cost is a single javac invocation per workspace.
    checker_source = facade.java_source_dir / CHECKER_SOURCE
    harvester_source = facade.java_source_dir / "SlotHarvester.java"
    checker_class_file = facade.compiled_dir / f"{CHECKER_CLASS}.class"
    harvester_class_file = facade.compiled_dir / "SlotHarvester.class"
    facade.compiled_dir.mkdir(parents=True, exist_ok=True)
    needs_compile = (
        not checker_class_file.exists()
        or not harvester_class_file.exists()
        or checker_source.stat().st_mtime > checker_class_file.stat().st_mtime
        or harvester_source.stat().st_mtime
            > harvester_class_file.stat().st_mtime
    )
    if needs_compile:
        logger.info("Compiling %s + SlotHarvester.java", CHECKER_SOURCE)
        javac = facade.find_java_executable("javac")
        cp = facade.get_full_classpath()
        r = subprocess.run(
            [javac, "-cp", cp, "-d", str(facade.compiled_dir),
             str(checker_source), str(harvester_source)],
            capture_output=True, text=True, timeout=120,
            encoding="utf-8", errors="replace",
        )
        if r.returncode != 0:
            raise LayerBError(
                f"Failed to compile {CHECKER_SOURCE} + SlotHarvester.java:\n"
                f"{r.stderr}"
            )

    # Run ModelChecker on a temp output set, then read back.
    with tempfile.TemporaryDirectory(prefix="linting_b_") as td:
        units_tmp = Path(td) / "units.json"
        descr_tmp = Path(td) / "descriptions.json"
        symbols_tmp = Path(td) / "symbols.json"
        slots_tmp = Path(td) / "slots.jsonl" if emit_slots else None

        java_exe = facade.find_java_executable("java")
        cp = facade.get_full_classpath()
        argv = [
            java_exe, "-Djava.awt.headless=true",
            "-cp", cp, CHECKER_CLASS,
            "--mph", str(mph_path),
            "--units-out", str(units_tmp),
            "--descr-out", str(descr_tmp),
            "--symbols-out", str(symbols_tmp),
        ]
        if emit_slots:
            argv += ["--slots-out", str(slots_tmp)]
            if slots_version:
                argv += ["--slots-version", slots_version]
        try:
            proc = subprocess.run(
                argv, capture_output=True, text=True,
                encoding="utf-8", errors="replace",
                timeout=timeout_s, env=facade.get_comsol_env(),
            )
        except subprocess.TimeoutExpired as e:
            raise LayerBError(
                f"ModelChecker timed out after {timeout_s}s"
            ) from e

        # Parse the last JSON-looking stdout line — same envelope pattern
        # as ModelExporter.
        envelope = None
        for line in reversed(
            [l for l in proc.stdout.splitlines() if l.strip()]
        ):
            try:
                envelope = json.loads(line)
                break
            except json.JSONDecodeError:
                continue
        if envelope is None:
            raise LayerBError(
                f"ModelChecker produced no JSON envelope. "
                f"exit={proc.returncode}; stderr={proc.stderr[:500]}"
            )
        if not envelope.get("success"):
            raise LayerBError(
                f"ModelChecker failed: {envelope.get('error', 'unknown')}"
            )

        units = json.loads(units_tmp.read_text(encoding="utf-8"))
        descrs = json.loads(descr_tmp.read_text(encoding="utf-8"))
        symbols = (json.loads(symbols_tmp.read_text(encoding="utf-8"))
                   if symbols_tmp.exists() else
                   {"params": [], "variables": []})

        if emit_slots:
            slot_records: list[dict] = []
            if slots_tmp is not None and slots_tmp.exists():
                from comsol_support.slot_harvest import read_dump
                slot_records = [
                    r for r in read_dump(slots_tmp)
                    if r.get("kind") == "slot"
                ]
            return units, descrs, symbols, slot_records
        return units, descrs, symbols


# ---- Sidecar writers --------------------------------------------------

def write_sidecars(
    output_mph: Path,
    *,
    layer_a_report: LintReport | None = None,
    layer_b_units: dict | None = None,
    layer_b_descriptions: dict | None = None,
    layer_c_units: dict | None = None,
) -> tuple[Path, Path]:
    """Write <mph>.units.json and <mph>.descriptions.json merging layers.

    Missing layers are written as empty sections rather than omitted, so
    the schema is stable regardless of which layers ran.
    """
    output_mph = Path(output_mph)

    units = {
        "layer_a": ((layer_a_report.to_units_sidecar().get("layer_a")
                     if layer_a_report else None)
                    or {"warnings": []}),
        "layer_b": ((layer_b_units or {}).get("layer_b")
                    or {"errors": []}),
        "layer_c": (layer_c_units
                    or {"warnings": [], "findings": []}),
        "generated_at": _utc_now_iso(),
    }
    # Propagate meta if Layer B provided it.
    for key in ("model_sha256", "model_path"):
        if layer_b_units and key in layer_b_units:
            units[key] = layer_b_units[key]

    descrs = {
        "layer_a": ((layer_a_report.to_descriptions_sidecar().get("layer_a")
                     if layer_a_report else None)
                    or {"missing_in_source": [], "placeholder_in_source": []}),
        "layer_b": ((layer_b_descriptions or {}).get("layer_b")
                    or {"missing_runtime": [], "placeholder": []}),
        "generated_at": _utc_now_iso(),
    }
    for key in ("model_sha256", "model_path"):
        if layer_b_descriptions and key in layer_b_descriptions:
            descrs[key] = layer_b_descriptions[key]

    units_path = output_mph.with_suffix(output_mph.suffix + ".units.json")
    descr_path = output_mph.with_suffix(
        output_mph.suffix + ".descriptions.json"
    )
    _atomic_write_json(units_path, units)
    _atomic_write_json(descr_path, descrs)
    return units_path, descr_path


def summarise_sidecars(
    layer_a_report: LintReport | None,
    layer_b_units: dict | None,
    layer_b_descriptions: dict | None,
    layer_c_units: dict | None = None,
) -> dict:
    """Compact counts for stdout summary line after a mphgen build."""
    a_unit = len(layer_a_report.unit_warnings) if layer_a_report else 0
    a_miss = sum(1 for d in (layer_a_report.descr_warnings
                             if layer_a_report else [])
                 if d.reason == "missing")
    a_plc = sum(1 for d in (layer_a_report.descr_warnings
                            if layer_a_report else [])
                if d.reason == "placeholder")
    b_err = len(((layer_b_units or {}).get("layer_b") or {})
                .get("errors", []))
    b_miss = len(((layer_b_descriptions or {}).get("layer_b") or {})
                 .get("missing_runtime", []))
    b_plc = len(((layer_b_descriptions or {}).get("layer_b") or {})
                .get("placeholder", []))
    c_warn = len((layer_c_units or {}).get("warnings", []))
    return {
        "units_warnings": a_unit,
        "units_errors": b_err,
        "descr_missing": a_miss + b_miss,
        "descr_placeholder": a_plc + b_plc,
        "dimensional_warnings": c_warn,
    }


# ---- CLI ----

def classify_mph_state(path: Path) -> str:
    """Classify an .mph as one of:

    - ``"partial"``  — file is a mid-edit partial save (suffix
                       ``*.partial.mph``). Linting will produce noisy
                       results because the model graph is incomplete.
    - ``"pre_solve"`` — file is an unsolved model (suffix
                       ``*_unsolved.mph`` or ``*.pre_solve.mph``, or the
                       co-located ``*.mphgen.json`` records ``solved=false``).
                       Layer B + C run normally; no solve-dependent
                       results to extract.
    - ``"complete"`` — anything else. Treated as a fully-formed model.

    Pure path/sidecar inspection; never reads the .mph zip itself. Falls
    back to ``"complete"`` on any inspection error so we never block on
    classification.
    """
    name = path.name
    if name.endswith(".partial.mph"):
        return "partial"
    if name.endswith(".pre_solve.mph") or name.endswith("_unsolved.mph"):
        return "pre_solve"
    sidecar = path.with_suffix(path.suffix + ".mphgen.json")
    if sidecar.exists():
        try:
            data = json.loads(sidecar.read_text(encoding="utf-8"))
            if data.get("solved") is False:
                return "pre_solve"
        except (OSError, json.JSONDecodeError):
            pass
    return "complete"


def cmd_check(args: argparse.Namespace) -> int:
    """CLI handler for `comsol-support check <path>`.

    Layer A only at the source scan level. Layer B runtime check against
    an .mph is handled by ModelChecker.java via a separate harness
    (not yet wired into this CLI subcommand).
    """
    p = Path(args.path)
    if not p.exists():
        print(f"error: path not found: {p}", file=sys.stderr)
        return 2

    if p.suffix == ".java":
        report = scan_java_source(p)
        units = report.to_units_sidecar()
        descrs = report.to_descriptions_sidecar()

        units_out = Path(args.output) if args.output \
            else p.with_suffix(".java.units.json")
        descrs_out = Path(args.output_descr) if args.output_descr \
            else p.with_suffix(".java.descriptions.json")

        _atomic_write_json(units_out, units)
        _atomic_write_json(descrs_out, descrs)

        n_units = len(report.unit_warnings)
        n_missing = sum(1 for d in report.descr_warnings
                        if d.reason == "missing")
        n_placeholder = sum(1 for d in report.descr_warnings
                            if d.reason == "placeholder")

        if args.format == "json":
            print(json.dumps({
                "units": units,
                "descriptions": descrs,
            }, indent=2))
        else:
            print(f"{p.name}: units {n_units} warnings; "
                  f"descriptions {n_missing} missing, "
                  f"{n_placeholder} placeholder")
            print(f"  {units_out}")
            print(f"  {descrs_out}")

        if args.exit_code_on_warnings and (
            n_units or n_missing or n_placeholder
        ):
            return 1
        return 0

    if p.suffix == ".mph":
        # Partial-state handling — distinguish "partial save from a
        # failed run, expect noise" from "fully-formed model with a
        # real defect". Without this gate, cycles ran lint on
        # *.partial.mph files mid-edit and got noisy findings that
        # trained reviewers to ignore lint output.
        model_state = classify_mph_state(p)
        if model_state == "partial":
            allow_partial = getattr(args, "allow_partial", False)
            if not allow_partial:
                print(
                    f"{p.name}: partial-state .mph (mid-edit) — linting "
                    f"deferred. Re-run on the completed .mph, or pass "
                    f"--allow-partial to lint anyway."
                )
                return 0
            logger.info("Linting partial-state .mph at user request")

        # If --db is set, ask ModelChecker to emit slots in the same
        # JVM session (fix #2 — avoids a second ~30s COMSOL load).
        want_slots = bool(getattr(args, "db", None)) and not args.skip_layer_c
        slots_version = getattr(args, "catalog_version", None)
        try:
            result = run_layer_b(
                p,
                comsol_path=args.comsol_path,
                workspace_dir=Path(args.workspace) if args.workspace else None,
                timeout_s=args.timeout,
                emit_slots=want_slots,
                slots_version=slots_version,
            )
        except LayerBError as e:
            print(f"layer-b error: {e}", file=sys.stderr)
            return 5
        if want_slots and len(result) == 4:
            units_b, descr_b, symbols_b, pre_walked_slots = result
        else:
            units_b, descr_b, symbols_b = result
            pre_walked_slots = []

        layer_c_units: dict | None = None
        if not args.skip_layer_c:
            try:
                from comsol_support.dimensional import (
                    analyze_model, findings_to_sidecar,
                )

                slot_records: list[dict] = []
                expected_overrides: dict[str, str] = {}
                annotations: list[dict] = []

                # Auto-populate expected_overrides from catalog, if a db
                # was supplied and has content. Non-fatal: any failure
                # falls through to the descriptive (non-catalog) path.
                db_path = getattr(args, "db", None)
                if db_path:
                    try:
                        from comsol_support.db import (
                            count_slot_catalog, init_db,
                        )
                        from comsol_support.slot_catalog import (
                            build_expected_overrides_from_slots,
                        )
                        conn = init_db(Path(db_path))
                        counts = count_slot_catalog(conn)
                        if counts["total"] > 0:
                            # Use slots already walked inside ModelChecker's
                            # JVM session (fix #2). No second COMSOL load.
                            slot_records = pre_walked_slots
                            expected_overrides, annotations = (
                                build_expected_overrides_from_slots(
                                    slot_records, conn,
                                    comsol_version=getattr(
                                        args, "catalog_version", "6.4"
                                    ),
                                    min_confidence=getattr(
                                        args, "catalog_min_confidence", "high"
                                    ),
                                )
                            )
                            logger.info(
                                "Catalog auto-populated %d overrides from "
                                "%d slot records (catalog total %d entries)",
                                len(expected_overrides), len(slot_records),
                                counts["total"],
                            )
                        conn.close()
                    except Exception as e:
                        logger.warning(
                            "Catalog auto-population skipped: %s", e
                        )

                findings = analyze_model(
                    symbols_b,
                    expected_overrides=(expected_overrides or None),
                    slot_records=slot_records or None,
                )
                layer_c_units = findings_to_sidecar(findings)
                if annotations:
                    layer_c_units.setdefault(
                        "catalog_annotations", annotations
                    )
            except Exception as e:
                logger.warning("Layer C dimensional analysis failed: %s", e)

        units_path, descr_path = write_sidecars(
            p, layer_b_units=units_b, layer_b_descriptions=descr_b,
            layer_c_units=layer_c_units,
        )
        # Annotate the freshly-written sidecars with the classified model
        # state so downstream consumers (auditor, MCP) can suppress
        # solve-dependent expectations for pre_solve targets and flag
        # partial-state findings separately.
        if model_state != "complete":
            for sc in (units_path, descr_path):
                try:
                    data = json.loads(sc.read_text(encoding="utf-8"))
                    data["model_state"] = model_state
                    _atomic_write_json(sc, data)
                except (OSError, json.JSONDecodeError) as e:
                    logger.warning(
                        "could not annotate %s with model_state: %s", sc, e
                    )

        summary = summarise_sidecars(
            None, units_b, descr_b, layer_c_units,
        )
        if args.format == "json":
            print(json.dumps({"summary": summary,
                              "model_state": model_state,
                              "units_sidecar": str(units_path),
                              "descriptions_sidecar": str(descr_path)},
                             indent=2))
        else:
            state_tag = f" [{model_state}]" if model_state != "complete" else ""
            print(f"{p.name}{state_tag}: units {summary['units_errors']} errors; "
                  f"descriptions {summary['descr_missing']} missing, "
                  f"{summary['descr_placeholder']} placeholder; "
                  f"dimensional {summary['dimensional_warnings']} warnings")
            print(f"  {units_path}")
            print(f"  {descr_path}")

        if args.exit_code_on_warnings and any(
            v for v in summary.values() if isinstance(v, int)
        ):
            return 1
        return 0

    print(f"error: unsupported extension {p.suffix!r} — expected .java or "
          f".mph", file=sys.stderr)
    return 2


def _atomic_write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def add_check_subparser(subs: argparse._SubParsersAction) -> None:
    p = subs.add_parser(
        "check",
        help="Lint a COMSOL Java builder (.java) or an .mph "
             "(runtime check — not yet wired).",
    )
    p.add_argument("path", help="Path to a .java or .mph file")
    p.add_argument("--output", "-o",
                   help="Override the units sidecar path")
    p.add_argument("--output-descr",
                   help="Override the descriptions sidecar path")
    p.add_argument("--format", choices=("text", "json"), default="text")
    p.add_argument("--exit-code-on-warnings", action="store_true",
                   help="Exit nonzero if any finding was recorded")
    p.add_argument("--comsol-path",
                   help="COMSOL installation root (Layer B only; default: "
                        "module constant)")
    p.add_argument("--workspace",
                   help="Build workspace for ModelChecker .class (Layer B; "
                        "default: next to the input .mph)")
    p.add_argument("--timeout", type=int, default=300,
                   help="Layer B/C timeout in seconds (default: 300)")
    p.add_argument("--skip-layer-c", action="store_true",
                   help="Skip Layer C dimensional analysis (pure-Python "
                        "unit algebra)")
    p.add_argument("--wolframscript",
                   help="DEPRECATED no-op (Layer C no longer uses "
                        "Wolfram); accepted so existing scripts keep "
                        "working")
    p.add_argument("--db",
                   help="Path to comsol-support SQLite DB. When set and the "
                        "slot_expected_units catalog has content, Layer C "
                        "auto-populates expected_overrides from it.")
    p.add_argument("--catalog-version", default="6.4",
                   help="COMSOL version tag to query against the catalog "
                        "(default: 6.4). Version-fallback annotations are "
                        "recorded alongside findings.")
    p.add_argument("--catalog-min-confidence",
                   choices=("high", "medium"), default="high",
                   help="Minimum catalog confidence for auto-population "
                        "(default: high). Medium entries are treated as "
                        "soft suggestions.")
    p.add_argument("--allow-partial", action="store_true",
                   help="Lint a partial-save .mph (suffix *.partial.mph) "
                        "anyway. By default these are skipped because the "
                        "model graph is mid-edit and findings are noisy.")
