# linting — units and descriptions

Flag expression typos (bad unit brackets) and missing / placeholder
descriptions in COMSOL Java builders and the `.mph` they produce, and
detect dimensional inconsistencies via independent symbolic unit
analysis.

Three layers, all **warn-only** (never block a build):

- **Layer A — static source scan.** Pure Python regex over `.java`
  source. Catches the unit-bracket typos worker agents often introduce
  (`[m]/[s]`, `/[s]`, `[m+s]`, Unicode units) and missing / placeholder
  descriptions on `.param().set(...)` and `.variable(…).set(...)` calls.
  Milliseconds, no COMSOL dependency.
- **Layer B — runtime model check.** Loads the `.mph` via COMSOL's Java
  API, enumerates params and component variables, probes
  `ParamBase.evaluateUnit(name)` per param, and records descriptions.
  Requires COMSOL.
- **Layer C — symbolic dimensional analysis.** Independent pure-Python
  engine (stdlib only; previously a Wolfram bridge) that
  parses every expression in the model and propagates dimensions as
  7-vectors of rational exponents over the SI base dimensions.
  Detects non-integer exponents on unit-bearing bases (COMSOL-style
  W1 warnings), inconsistent arithmetic, expected-vs-deduced
  mismatches on physics slots the caller annotates, and unknown unit
  names. Topic-agnostic — no electromagnetism-specific knowledge
  baked in. No external dependencies.

Both layers emit JSON sidecars next to the `.mph`:

```
<output>.mph                       # the COMSOL model
<output>.mph.mphgen.json           # mphgen provenance
<output>.mph.units.json            # unit lint findings
<output>.mph.descriptions.json     # description lint findings
```

## CLI

```bash
# Source scan only
comsol-support check Builder.java

# Runtime .mph scan
comsol-support check path/to/out.mph

# With mphgen — all three layers run automatically after build
comsol-support mphgen --builder Builder.java --output out.mph
# skip flags:
#   --no-lint         skip all layers
#   --skip-layer-a    skip source scan
#   --skip-layer-b    skip runtime check (e.g. no COMSOL available)
#   --skip-layer-c    skip dimensional analysis (pure Python; skip for speed)
#   --allow-partial   lint a *.partial.mph anyway (default: skip)
```

### Partial-state .mph handling

`comsol-support check <path>.mph` classifies the input as one of:

| State | Trigger | Behavior |
|---|---|---|
| `partial`  | filename ends `.partial.mph` | Skipped by default with a clear message — `--allow-partial` overrides. Model graph is mid-edit and findings are noisy. |
| `pre_solve`| ends `_unsolved.mph` or `.pre_solve.mph`, **or** co-located `.mphgen.json` has `solved: false` | Linted normally. Sidecars annotated with `"model_state": "pre_solve"` so downstream consumers know no solve-dependent results are present. |
| `complete` | anything else | Linted normally, no annotation. |

When a non-default state is detected, the human-readable stdout summary
prefixes the filename with the tag (`model.mph [pre_solve]: …`), and
the JSON-format output adds a top-level `"model_state"` field.

Why this matters: a `*.partial.mph` written by `ModelExporter`'s
save-on-exit path is a diagnostic artifact, not a built model. Linting
it produces dozens of noise findings that trained earlier reviewers to
ignore lint output entirely. Skipping by default keeps the lint signal
trustworthy; `--allow-partial` is the escape hatch for the deliberate
case.

## Unit patterns (Layer A)

| # | Matches | Fix |
|---|---|---|
| A1 | `[m]/[s]`, `[V]*[A]` — bracket-op-bracket | merge: `[m/s]`, `[V*A]` |
| A2 | leading `*[...]` or `/[...]` with no quantity | prefix: `1*[m]`, `1/[s]` |
| A3 | bare bracket (no preceding quantity) | prefix: `1[m]` instead of `[m]` |
| A4 | `+` or `-` inside a bracket group | split into separate bracket groups |
| A5 | non-ASCII inside a bracket (`Ω`, `μ`, `°`) | use ASCII: `[ohm]`, `[um]`, `[degC]` |

Layer A skips Java literals that are a single bracket group (`"[m]"`)
because those are almost always the suffix half of a concatenation
(`width + "[m]"`) whose COMSOL expression is fine.

## Description heuristics (Layer A + B)

- **Missing**: the descriptor argument is absent on `.set()` and no
  `.descr("name", ...)` call covers the name.
- **Placeholder**: descriptor matches one of `TODO | TBD | XXX |
  PENDING | FIXME | ... | ?+ | N/A` (case-insensitive), or is under
  three characters.

Argument parsing splits the `.set(...)` call on
top-level commas — string-, char- and paren-aware — and then
classifies each argument, so a non-literal middle argument
(`String.valueOf(N_STEPS)`, `"1[m]" + suffix`) no longer shifts the
description slot (the old all-string-literals scan fired false
missing/placeholder findings on such calls; LINT_BACKLOG entry 1).
Two cases are deliberately silent: a *dynamic name* (`"p" + i`) can't
be tracked at all, and a *non-literal description* (`DESCR_CONST`)
counts as present but unverifiable.

## Sidecar schema

`<mph>.units.json`:

```json
{
  "layer_a": {"warnings": [{"file":"…","line":42,"pattern":"A1",
                             "expression":"[m]/[s]",
                             "suggested_fix":"[m/s]"}]},
  "layer_b": {"errors": [{"node":"param","name":"rho_val",
                            "expression":"…","error":"…"}]},
  "generated_at": "…",
  "model_sha256": "…"
}
```

`<mph>.descriptions.json`:

```json
{
  "layer_a": {"missing_in_source": [...],
              "placeholder_in_source": [...]},
  "layer_b": {"missing_runtime": [...],
              "placeholder": [...]},
  "generated_at": "…",
  "model_sha256": "…"
}
```

Both layers are always present in the file (with empty arrays if not
run) so consumers can read a stable schema.

## Layer C — symbolic dimensional analysis

Layer C is an **independent** engine that does not rely on any COMSOL
internal channel. Six parallel research probes (summary in
`docs/comsol_linting_research.md`) confirmed that COMSOL's
GUI yellow unit warnings are architecturally unreachable from a
headless Java session — so Layer C reimplements dimensional analysis
from scratch with its own unit-algebra engine (pure Python; the
original Wolfram back end was retired after an expired
license silently disabled Layer C fleet-wide — the algebra is
elementary and never needed a CAS). Benefits: topic-agnostic,
version-stable, auditable, dependency-free.

### Pipeline

1. **Collect symbols.** `ModelChecker.java` emits
   `<mph>.symbols.json` listing every param and variable with its
   expression and (for params) COMSOL-deduced unit from
   `ParamBase.evaluateUnit`.
2. **Parse expressions.** A small recursive-descent parser turns each
   COMSOL expression into an AST (handles `NUMBER[unit]`,
   `IDENT[unit]` unit-conversion, dotted names, `^`/`*`/`/`/`+`/`-`,
   function calls, derivatives).
3. **Flag W1 at parse time.** Any `^(exp)` whose exponent is not a
   literal integer AND whose base statically contains a unit-bearing
   leaf is flagged. Matches COMSOL's conservative behavior.
4. **Evaluate dimensions.** The AST is reduced in-process: unit names
   resolve against an explicit COMSOL unit table (SI base + derived +
   common non-SI, with SI-prefix decomposition and per-unit scale
   factors), and dimensions propagate as rational-exponent 7-vectors.
   Magnitudes are ignored except as exponents. Results include the
   canonical deduced unit (SI base form, e.g. `kg*m^2/(A^2*s^3)`),
   a dimension signature, a `resolved` flag, and dimension-equality
   compatibility against any caller-supplied expected unit
   (`CompatibleUnitQ` semantics).
6. **Multi-pass.** Deduced variable units are fed back into the
   symbol table so later expressions see the right dimensions (e.g.
   `Ez_val = rho_val * Jz_val` resolves once `rho_val` and `Jz_val`
   are known).
7. **Emit sidecar.** `layer_c.warnings[]` lists each finding by kind:
   `non_integer_power`, `inconsistent_arithmetic`,
   `unresolved_dimensions`, `expected_mismatch`, `translation_error`.

### Scope-aware resolution

Complex models — agentic or human — often define the same variable
name in multiple scopes (e.g. `rho_val` in both `var_air` and
`var_b`). The same name can even carry different units across
scopes (think `rho` as copper resistivity vs. air resistivity with
different prefixes). Layer C handles this with a `SymbolResolver`:

- Params live at scope `"param"` and are globally visible.
- Variables live at their source scope (e.g. `"comp1/var_b"`).
- For an expression written in scope `S`, the symbol view is:
  1. **Params** — always win.
  2. **Current-scope variables** — override other scopes.
  3. **Other-scope variables** — fall-through (first-seen wins).
  4. **Qualified references** — `var_b.rho_val` and
     `comp1/var_b.rho_val` resolve to the specific scope.
- A `scope_conflict` warning is emitted when the same variable name
  has **different non-empty deduced units** across scopes — a
  genuine correctness issue that would produce wrong physics in
  whichever scope's version the model's runtime selects.

The reference model's two `rho_val` definitions (`var_air`, `var_b`) both deduce
to `ohm*m`, so no conflict fires — the shadowing is legitimate.

### Unit canonicalisation

Unit strings are parsed as COMSOL expressions (not regex-rewritten)
and re-emitted as a canonical `num/denom` form Mathematica parses
reliably. This:

- Correctly reads `1/m*s` as `s/m` (standard left-to-right math
  precedence), not the regex-trap `(m*s)^-1`.
- Preserves Mathematica's context-sensitive name resolution (e.g.
  `H/m` stays as-is because `H*m^-1` makes Mathematica ambiguate `H`
  between Henries and Hours).
- Normalises `1/X` to `X^-1` only when safe.

### Domain-agnostic design

No physics baked in. The unit table handles the SI base units, the
derived SI units, and common non-SI units (`min`, `bar`, `eV`, `in`,
…) with SI-prefix decomposition (`mm`, `uH`, `kA`, `GHz`); COMSOL-style
short names (`ohm`, `H/m`, `A/m^2`) resolve directly. Unknown unit
names surface as explicit `unknown_unit` warnings. Spatial
coordinates `x`/`y`/`z`/`r` and time `t` carry their conventional
units (`m` and `s`) so derivatives `d(u, x)` produce the right
dimension without any physics-specific code.

### Supplying expected units for physics slots

COMSOL's GUI computes "expected" units for physics slots (e.g. Ga in
a General Form PDE) from the physics-interface's dimensional form
contract. That contract is physics-specific and not fully expressed
in `dmodel.xml`; deriving it in general would reimplement COMSOL.

The practical workaround: **let the caller annotate slot
expectations**. `analyze_model` takes an optional
`expected_overrides` dict mapping `<kind>:<scope>:<name>` to an
expected unit string:

```python
from comsol_support.dimensional import analyze_model
overrides = {
    "variable:comp1/var_post:Ez_val": "1/m",   # Ga slot expects [1/m]
    "param:param:mu0_val":            "s/m^2", # da slot expects [s/m^2]
}
findings = analyze_model(symbols_json, overrides)
```

When present, Layer C compares the deduced unit against the expected
unit (dimension equality — `CompatibleUnitQ` semantics) and emits
`expected_mismatch` warnings on discrepancy. This is how the reference model's
W2/W3 reach the sidecar.

### What Layer C catches on the reference model

- **W1** (non-integer exponent on `rho_val` in `var_b`) — caught
  intrinsically, no annotation needed.
- **W2** (`Ez_val` deduces to `[Ohm]`, expected `[1/m]`) — caught
  with the `variable:comp1/var_post:Ez_val → 1/m` override.
- **W3** (`mu0_val` deduces to `[H/m]`, expected `[s/m^2]`) — caught
  with the `param:param:mu0_val → s/m^2` override.

### Requirements

None beyond the Python stdlib (the historical
`wolframscript` requirement is gone; the deprecated `--wolframscript`
CLI flag is accepted as a no-op so existing scripts keep working).
No MATLAB, no licensed COMSOL helpers, no GUI session.

## Prescriptive expected units — slot catalog

Layer C as originally shipped was **descriptive**: it deduces the unit
of an expression and flags internal inconsistencies (W1-class) but has
no knowledge of what COMSOL *expects* for a given physics-slot
assignment. The `slot_expected_units` catalog adds
prescriptive knowledge by mining COMSOL's shipping application corpus.

Five-axis subvariant key (all from the COMSOL API, no hand curation):

```
(physics_type, sdim, feature_type, feature_scope, slot_property)
```

Workflow:

```
# Phase 1+2: harvest and aggregate (once per COMSOL version)
comsol-support scrape slots \
  --applications-dir "$COMSOL_PATH/applications" \
  --output slot_dump.jsonl \
  --db comsol-support.db

# Phase 3: use the catalog at lint time
comsol-support check path/to/model.mph --db comsol-support.db

# Phase 4: inspect catalog / discover missing subvariant axes
comsol-support slot-stats --db comsol-support.db --show-high-confidence
comsol-support slot-stats --db comsol-support.db --show-scattered
comsol-support slot-stats --db comsol-support.db --show-coverage
```

Confidence tiering (`comsol_support/slot_catalog.py` constants):

- **high** — `n_attempts ≥ 20` AND `modal_frac ≥ 0.9` AND
  `coverage_frac ≥ 0.5` AND modal unit is non-trivially dimensional.
- **medium** — `n_attempts ≥ 5` AND `modal_frac ≥ 0.7` AND non-trivial.
- **low** — everything else.

Dimensionless-demotion: modal units of `DimensionlessUnit`, `1`, or
empty never reach high/medium. Their prescriptive value is zero.

Scatter report (`--show-scattered`) surfaces keys where the same
five-axis tuple has a bimodal (or worse) unit distribution. These are
concrete candidates for adding a sixth subvariant axis — any new axis
must be justified by scatter data, not speculation.

(The original slot-catalog planning note lived outside the repo; the
design it describes is the one implemented in `slot_catalog.py`.)

## Historical note — GUI yellow warnings aren't accessible

The yellow-highlight warnings that appear in the COMSOL GUI are
computed client-side and are **not persisted** in the `.mph` nor
exposed by the Java API.

**Nine empirical probes** have confirmed this channel is architecturally
unreachable from any deterministic backend:

- Original round (2026-04-21): reflection into `ModelUtil`, solver-prep
  probes, `comsol batch` scraping, live Model tree walks, MATLAB LiveLink,
  probe-param `evaluateUnit` trick.
- Follow-up round (2026-04-22): `comsol mphserver` RPC (same `ModelClient`
  API wire-wrapped), `comsol batch -methodcall` Model Methods (MethodRunner
  JVM is *more* restricted than standalone — classloader excludes
  `com.comsol.core.*` / `guimph.*` / `clientapi.*`), and an exhaustive
  20-combo flag + env sweep (`com.comsol.guiappl` — which contains
  `ExpandWarningsAndErrors` — is never loaded in batch mode, and the
  SecurityManager blocks `j.u.l.LoggingPermission` for injected code).

See `comsol_linting_research.md` for the evidence. Layer C is our
answer: compute dimensions ourselves with an independent, robust engine.

## Python API

```python
from comsol_support.linting import (
    scan_java_source, run_layer_b, write_sidecars,
)

# Layer A
report = scan_java_source(Path("Builder.java"))
units_a = report.to_units_sidecar()

# Layer B
units_b, descr_b = run_layer_b(Path("out.mph"))

# Merged sidecars
write_sidecars(Path("out.mph"),
               layer_a_report=report,
               layer_b_units=units_b,
               layer_b_descriptions=descr_b)
```

## Exit codes

`comsol-support check` exits 0 by default. Pass
`--exit-code-on-warnings` in CI to exit nonzero when findings are
present.
