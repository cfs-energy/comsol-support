# comsol-support

> **Requires COMSOL Multiphysics 6.4.** Every table, catalog, gotcha and
> test in this repository was built and measured against 6.4
> (6.4.0.293 Linux, 6.4.0.429 Windows, 6.4.0.378 macOS). Other versions
> are unverified: the CLI prints a warning when it finds one, `doctor`
> flags it, and [COMSOL_VERSIONS.md](COMSOL_VERSIONS.md) explains what
> would break. A COMSOL install and license are required for everything
> except Layer A linting and knowledge-base search.

**Comsol-support is a deterministic automation and quality-assurance layer designed to connect LLMs (e.g., Claude, Codex, Gemini) with COMSOL Multiphysics workflows.**
For example, comsol-support can be used to automate meshing (a complex but easily verifiable problem), simplify results post-processing, and audit and debug complex models. 

**COMSOL model-code generation, execution, and linting tools** for
COMSOL Multiphysics 6.4: compile Java builders to `.mph`, load / mutate /
mesh / solve existing models, lint units and dimensions, and look up the
API from a scraped knowledge base — all deterministic, driven from a CLI
and an MCP server. The calling agent (human or LLM) plans and composes;
comsol-support does the typed Java glue, the linting, the ontology
lookups, and the telemetry plumbing.

**Design goal:** deterministic floor, agentic ceiling. The agent that
uses this repo should never have to *guess* a COMSOL API string — every
type name and property key it emits should be something it looked up
in the knowledge base or copied from a known-good fragment, and every
unit it writes should be checked before the solver sees it. That is the
goal the tooling is built toward; the section
[What is actually checked](#what-is-actually-checked) says precisely
how far it is realised today, because the gap between "looked up" and
"verified" is where silent modeling errors live.

**Status:** 793 tests collected (766 in the default sweep;
27 real-COMSOL tests opt-in via `-m real_comsol`); zero external
runtime dependencies (stdlib only). Verified against COMSOL 6.4.0.293 on
Linux, 6.4.0.429 on Windows, and 6.4.0.378 on macOS (Apple Silicon —
see [docs/macos-setup.md](docs/macos-setup.md)). Install on any of the
three: [INSTALL.md](INSTALL.md); port detail in
[docs/platform-setup.md](docs/platform-setup.md). Every layer is
deterministic — nothing in this repository calls an LLM; the modeling
practice a calling agent is expected to follow is written down in
[`docs/modeling-practice.md`](docs/modeling-practice.md).

---

## What it gives you

| You want to… | Reach for | Doc |
|---|---|---|
| Compile a `.java` builder to an unsolved `.mph` (or solve too) | `comsol-support mphgen` | [docs/mphgen.md](docs/mphgen.md) |
| Load an existing `.mph`, optionally mutate, solve, save | `comsol-support edit-mph` | [docs/mphedit.md](docs/mphedit.md) |
| Build a mesh on an existing model, with census + per-feature records | `comsol-support edit-mph --mesh <tag>` | [docs/meshing.md](docs/meshing.md) |
| Read-only introspection of an `.mph` (never saves) | `comsol-support query-mph` | [docs/query-mph.md](docs/query-mph.md) |
| Compile + run any COMSOL-dependent Java class, tee full log | `comsol-support run-harness` | [docs/run-harness.md](docs/run-harness.md) |
| Check COMSOL license-seat availability (bounded probe) | `comsol-support license-status` | [docs/run-harness.md](docs/run-harness.md) |
| Lint a `.java` (units / descriptions) or `.mph` (runtime + dimensional) | `comsol-support check` | [docs/linting.md](docs/linting.md) |
| Periodic "still alive" event during a long solve | `import SolverHeartbeat` | [docs/solver-progress.md](docs/solver-progress.md) |
| Check this machine can run comsol-support | `comsol-support doctor` | [INSTALL.md](INSTALL.md) |
| Search a recurring symptom against the gotcha catalog | `comsol-support gotcha-search` | [docs/known-gotchas.md](docs/known-gotchas.md) |
| Look up an API type / property key (Javadoc + Reference Manual) | `comsol-support search "<query>" [--stage …]` | MCP `search_api` is the same backend |
| Stage-by-stage modeling practice (mindsets, invariants, pitfalls, API quick-refs) | read | [docs/modeling-practice.md](docs/modeling-practice.md) |
| Tag / selection conventions for builders and mutators | `TagRegistry`, `SelectionAlgebra` | [docs/facade-conventions.md](docs/facade-conventions.md) |
| Ingest a telemetry JSONL into the DB | `comsol-support ingest-telemetry` | |
| Populate the knowledge base / fragments / slot catalog | `comsol-support scrape {javadoc,refmanual,corpus,slots}` | |

Full CLI reference: `comsol-support --help`.

> **Source checkout only.** Install is `git clone` + `uv sync` (or
> `pip install -e .`). A wheel built from `pyproject.toml` does not carry
> the Java sources, the probe library or `docs/known-gotchas.md`, so an
> installed wheel cannot work; `comsol-support doctor` reports it as a
> blocking problem and `scripts/wheel_smoke.sh` (run in CI) pins that
> the failure is explicit. Do not publish wheels.
>
> **Running from another directory.** `uv run …` resolves the
> `comsol_support` package relative to the *current* project, so invoking
> it from outside this repo raises `ModuleNotFoundError: comsol_support`.
> Either run from the repo root, or pass the project explicitly:
> `uv --project /path/to/comsol-support run comsol-support …`. The
> installed console script `comsol-support` does not depend on cwd. The
> COMSOL install path is auto-discovered per platform (see
> [COMSOL_VERSIONS.md](COMSOL_VERSIONS.md) for the probed roots) and can
> be overridden anywhere via the `COMSOL_PATH` environment variable.
>
> **Which database.** The knowledge base, fragments, telemetry and
> catalogs live in one SQLite file: `data/comsol.db` **inside the
> checkout**, the same file from whatever directory you run in (before
> 1.0 it was cwd-relative, so every new directory silently got an empty
> one). `COMSOL_DB` or `--db` override it. Populate it once with
> `comsol-support scrape javadoc` + `comsol-support scrape refmanual`
> (~14k rows from the on-disk 6.4 docs); `comsol-support doctor` reports
> what it contains. The MCP server still takes the path from `COMSOL_DB`.
>
> **Names.** The package, CLI and import name are `comsol-support` /
> `comsol_support` since 1.0. The pre-1.0 names still work — the
> `comsol-agent` command is an alias and `import comsol_agent` resolves
> to the same modules — so existing scripts and campaign workspaces are
> unaffected; prefer the new names.

---

## Quick start

Full setup instructions for Linux, macOS, and Windows — including the
knowledge-base build and what the expected failure counts look like —
are in [INSTALL.md](INSTALL.md). The short version:

```bash
# Install everything: deps, Java, ontology, corpus
./scripts/install.sh          # Linux / macOS
.\scripts\install.ps1          # Windows (PowerShell)

# Check the machine can run it (works before installing, any platform)
python scripts/doctor.py

# …or just the Python package (editable, dev deps)
uv sync

# Generate a pre-solve .mph from a Java builder
comsol-support mphgen --builder MyBuilder.java --output model_unsolved.mph

# Solve it via edit-mph (no mutator; pure load → solve → save)
comsol-support edit-mph \
    --input  model_unsolved.mph \
    --output model_solved.mph \
    --solve  std1

# Lint the solved .mph (Layer A + B + C)
comsol-support check model_solved.mph

# Each step writes sibling sidecars:
#   model_solved.mph.telemetry.jsonl   (live JSONL of solve events)
#   model_solved.mph.mphedit.json      (provenance + digest)
#   model_solved.mph.jvm.log           (raw JVM stream, crash-safe tee)
#   model_solved.mph.units.json        (linting findings)
#   model_solved.mph.descriptions.json (description findings)
```

### Parameterized edit across cycles

A campaign that varies one parameter across many runs of the same
reference model:

```bash
for pitch in 0.030 0.040 0.050; do
  comsol-support edit-mph \
      --input  reference.mph \
      --output sweep_p${pitch}.mph \
      --mutator SetSlotPitch.java \
      --arg    pitch=${pitch} \
      --solve  std1
done
```

The mutator is one method:

```java
public class SetSlotPitch {
    public static Model mutate(Model m, Map<String,String> args) {
        double p = Double.parseDouble(args.getOrDefault("pitch", "0.04"));
        m.param().set("slot_pitch", p + "[m]");
        return m;
    }
}
```

---

## Architecture

Three layers, all deterministic. Planning belongs to whatever agent
calls the CLI / MCP tools; nothing in this repo talks to an LLM.

```
L3  Fragment library (mined from the bundled model corpus, schema-backed)
L2  Typed Java facade (TagRegistry, SelectionAlgebra,
                       ModelExporter, ModelChecker,
                       SlotHarvester, SolverHeartbeat,
                       SolverTelemetry, probes/)
L1  com.comsol.model.* (vendor API)
```

An earlier design placed an LLM-driven build orchestrator above these
layers. It was removed before 1.0 — every consumer reached COMSOL
through the verbs above instead. The modeling practice it encoded is
now in `docs/modeling-practice.md` and its tag and selection
conventions in `docs/facade-conventions.md`. A `data/comsol.db`
created by that version upgrades in place: the retired tables are
dropped the next time it is opened.

### What is actually checked

The honest map of where deterministic verification exists today and
where the calling agent is still trusted:

| Surface | Checked how | Outcome |
|---|---|---|
| Unit literals in `.java` `.set(...)` calls | Layer A regex (six typo patterns, missing / placeholder descriptions) | findings in `*.units.json` / `*.descriptions.json`; `check` exits 0 unless `--exit-code-on-warnings` |
| Global parameters in a saved `.mph` | Layer B: `ModelChecker.java` calls `ParamBase.evaluateUnit` per parameter | same; component *variables* have no `evaluateUnit` in the API (`G-VARIABLE-EVALUATEUNIT-MISSING`), so they are covered only by Layer C |
| Expression dimensions vs expected units | Layer C: pure-Python symbolic dimensional analysis against the corpus-derived slot catalog | W1/W2/W3 findings; the catalog has confidence tiers, so a "mismatch" against a low-confidence slot is a hint, not a verdict |
| Type names / property keys the agent is about to emit | **Lookup, not enforcement.** `comsol-support search` / MCP `search_api` answer from the Javadoc + Reference Manual scrape (~14k rows); nothing rejects a Java file that uses an unknown string | the agent's discipline — `docs/modeling-practice.md` gives the ladder (fragment → adapted exemplar → free synthesis with every `.create` type and `.set` key looked up first) |
| Tags, selection names, expressions, values, method arguments | **Not checked against any table.** `TagRegistry` / `SelectionAlgebra` make tag and selection *construction* structured for code that opts into them | `docs/facade-conventions.md` |
| Solver / mesh outcomes | Telemetry stream (`halt_reason`, `error_detail`, mid-run `*_error` events, mesh census) | digest + sidecars; `check_health` budgets are opt-in |

So: linting and telemetry are enforced by code; API-name correctness
is *retrievable* by code but *applied* by the agent. Missing databases
and lookup failures fail open (empty result plus a hint naming the
`scrape` verbs), never by blocking a build.

### Three-layer linting

- **Layer A** — pure-Python regex over `.java` source. Catches the
  six common unit-typo patterns (`[m]/[s]`, missing quantity,
  addition inside brackets, non-ASCII, …) plus missing /
  placeholder `.set()` descriptions. Runs in milliseconds. No COMSOL
  required.
- **Layer B** — runtime check on the saved `.mph` via
  `ModelChecker.java`. Walks `model.param()` and
  `model.variable()`, calls `ParamBase.evaluateUnit` per param,
  produces structured sidecars.
- **Layer C** — independent symbolic dimensional analysis, pure
  Python (stdlib only; the Wolfram bridge was retired before 1.0).
  Tokenizes + parses COMSOL expressions, propagates dimensions as
  rational-exponent vectors over the SI base dimensions, and checks
  dimension-equality compatibility against catalog-derived expected
  units. Catches W1 (non-integer exponent on unit base), W2/W3
  (deduced-vs-expected mismatch), scope-conflicts, unknown units.

Slot expected-unit catalog (5-axis key: `physics_type × sdim ×
feature_type × feature_scope × slot_property × comsol_version`)
promotes Layer C from descriptive to contract-checking. Phase 1:
harvest from the .mph corpus. Phase 2: aggregate with confidence
tiering.

### One SQLite, many roles

`data/comsol.db` (or `$COMSOL_DB`) carries **7 user tables + 2 FTS5
virtuals + sync triggers**:

| Table | Role |
|---|---|
| `knowledge` (+ FTS5) | Javadoc + RefManual ontology — the `search` / `search_api` backend |
| `fragments` | Corpus-mined Java code blocks |
| `telemetry_events` (+ FTS5) | Solver events from the `TELEMETRY:` stream, keyed by a caller-chosen run id |
| `slot_expected_units` | Layer C prescriptive catalog |
| `variable_declared_units` | SymbolResolver seed |
| `native_interfaces` | Native-preference catalog |
| `domain_synonyms` | Intent-keyword → domain mapping |

Migrations are additive `ALTER TABLE ADD COLUMN` in try/except so
rolling upgrades stay backward-compatible. Files created before
2026-08 may also carry the retired orchestrator's `builds` /
`checkpoints` objects; `init_db` drops them on open (they never held
rows; nothing kept references them).

### MCP server

Ten JSON-RPC stdio tools: `search_api`, `get_fragment`,
`search_fragments`, `get_solver_telemetry`, `get_solver_results`,
`search_telemetry`, `list_physics_options`,
`list_studies_for_physics`, `list_default_plots_for_physics`,
`search_gotchas`. Launch directly or via Claude Code's `--mcp-config`
(`command: python comsol_support/mcp_server.py`, `env: {COMSOL_DB: …}`).
No auth — runs inside a Claude Code subprocess.

---

## Documentation

| Doc | Topic |
|---|---|
| [docs/modeling-practice.md](docs/modeling-practice.md) | Stage-by-stage modeling practice: working discipline, native-first ladders, mindsets / invariants / pitfalls / API quick-refs |
| [docs/facade-conventions.md](docs/facade-conventions.md) | TagRegistry + SelectionAlgebra: tag discipline, prefix table, selection discipline, API tables |
| [docs/mphgen.md](docs/mphgen.md) | Builder → .mph generator |
| [docs/mphedit.md](docs/mphedit.md) | Load → mutate → save (sister to mphgen) |
| [docs/meshing.md](docs/meshing.md) | Mesh doctrine measured on a 2,773-domain laminate; `--mesh` verb and probes |
| [docs/query-mph.md](docs/query-mph.md) | Read-only `query(Model, Map)` introspection (never saves) |
| [docs/run-harness.md](docs/run-harness.md) | Generic harness runner + `license-status` probe |
| [docs/linting.md](docs/linting.md) | Three-layer linting architecture |
| [docs/solver-progress.md](docs/solver-progress.md) | Why there's no per-step solver callback, and the polling-heartbeat workaround |
| [docs/known-gotchas.md](docs/known-gotchas.md) | COMSOL 6.4 + headless-solve pitfalls catalog (33 measured entries) |
| [INSTALL.md](INSTALL.md) | **Start here** — install on Linux / macOS / Windows |
| [docs/platform-setup.md](docs/platform-setup.md) | Windows / macOS / Linux port notes + verification record |
| [docs/macos-setup.md](docs/macos-setup.md) | macOS (Apple Silicon) verification record |
| [MANIFEST.md](MANIFEST.md) | Repository map: modules, database schema, MCP tools, data flow |
| [CHANGELOG.md](CHANGELOG.md) | Change history |
| [CONTRIBUTING.md](CONTRIBUTING.md) | Why the project exists; what contributions are wanted; what to run before a PR |
| [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md) · [SECURITY.md](SECURITY.md) | Contributor Covenant 2.1; what the tools execute on your machine and how to report a vulnerability |
| [docs/data-provenance.md](docs/data-provenance.md) | What derives from the COMSOL install, what the repo ships (scrapers, never data), the CI guard |
| [COMSOL_VERSIONS.md](COMSOL_VERSIONS.md) | 6.4 as target and ceiling; install auto-discovery; what is read from the install |
| [gaps.md](gaps.md) | Roadmap and known limitations |

For long-running campaign workspaces, copy from [`templates/`](templates/):
findings register schema, campaign-state JSON schema, finding-ID
convention, and workspace-directory layout.

---

## Repository layout

```
comsol_support/
    java/            # COMSOL-aware Java: TagRegistry, SelectionAlgebra,
                     # ModelExporter (build / edit / mesh), ModelChecker,
                     # SlotHarvester, SolverTelemetry, SolverHeartbeat,
                     # NodeTreeProbe, LicenseProbe, CorpusBatchConverter,
                     # probes/ (MeshStats, FeatureProblem, MeshSelection)
    cli.py           # 13-subcommand entry point
    doctor.py        # cross-platform install preflight (`comsol-support doctor`)
    mphgen.py        # builder → .mph
    edit_mph.py      # existing .mph → mutate / mesh → save
    query_mph.py     # read-only query contract
    run_harness.py   # generic harness runner
    linting.py       # three-layer lint orchestration
    dimensional.py   # Layer C symbolic analysis (pure Python)
    slot_catalog.py  # prescriptive slot catalog
    gotcha_search.py # known-gotchas search
    telemetry.py     # telemetry stream / digest / health / DB ingest
    jvm_slot.py      # advisory exclusive-JVM slot + leak scan
    knowledge_format.py  # one rendering for CLI search + MCP search_api
    …                # scrapers, catalogs, MCP server, etc.

scripts/             # install.sh / install.ps1 / doctor.py (pre-install preflight)
skill/               # the Claude skill (copy to ~/.claude/skills/comsol-support/)
templates/           # campaign-state primitives (findings register, etc.)
docs/                # design docs (see table above)
tests/               # 35 test files, 793 tests collected
data/                # one SQLite DB (knowledge + fragments + telemetry + catalogs)
```

---

## Testing

```bash
# Full default sweep (~70 s, 766 tests; real-COMSOL tests deselected):
uv run pytest -q

# The lint self-test — would-have-caught regressions to Layer A/B/C,
# plus compile-every-Java-source against the full COMSOL classpath:
uv run pytest tests/test_lint_selftest.py

# Real-COMSOL E2E (gated; requires a live COMSOL install + license seat):
COMSOL_E2E=1 uv run pytest -m real_comsol
```

**CI** ([`.github/workflows/ci.yml`](.github/workflows/ci.yml)) runs on
every push and pull request, on hosted **Linux, macOS and Windows**
runners (**Python 3.10–3.13** on Linux) **without COMSOL**: the pure-Python suite (the COMSOL-dependent
tests skip — 755 run, 11 skip), the four Java sources that compile
without COMSOL (`SolverHeartbeat`, `SolverTelemetry`, `TagRegistry`,
`SelectionAlgebra` — compiled with a bare JDK by
`tests/test_standalone_java.py`, which also drives `SelectionAlgebra`
and `TagRegistry` through Java probes; `SolverHeartbeat` likewise in
`tests/test_solver_heartbeat.py`), the full ruff `F` rule set, the provenance guard
(no COMSOL-derived data, copied documentation prose or site-specific
strings tracked — [docs/data-provenance.md](docs/data-provenance.md)),
`doctor`, and the installed-wheel smoke.

What hosted CI cannot check — every real-COMSOL path and the
compile-every-Java-source-against-the-COMSOL-classpath self-test in
`tests/test_lint_selftest.py` (the check that would have caught the
historical `SlotHarvester` regression) — is covered by
[`.github/workflows/real-comsol.yml`](.github/workflows/real-comsol.yml),
which runs `doctor`, `license-status`, the full sweep, the
gated `real_comsol` suite and an mphgen → mesh → probe → lint smoke on
a **self-hosted runner** labelled `comsol`. It is inert (manual trigger)
until such a runner is registered; until then, run those by hand on a
machine with COMSOL 6.4 before a release: `uv run pytest -q` there, plus
`COMSOL_E2E=1 uv run pytest -m real_comsol` with a license seat. The
reference-model tests additionally need a model that is not in this
repository: `COMSOL_REFERENCE_MPH` (a solved `.mph`, for
`test_linting_runtime.py`) and `COMSOL_REFERENCE_JAVA` (a builder
source, for the Layer A false-positive check). Both skip cleanly when
unset.

---

## Claude skill

The repo ships a Claude skill at [`skill/SKILL.md`](skill/SKILL.md).
Register it by copying it to your user skills directory —
`~/.claude/skills/comsol-support/SKILL.md` (Linux/macOS) or
`%USERPROFILE%\.claude\skills\comsol-support\SKILL.md` (Windows).
Any Claude Code session that mentions COMSOL, `.mph`, `FlException`,
`study.run`, `mphgen`, or `edit-mph` then surfaces the skill
automatically with usage hints and doc pointers.

---

## Dependencies

- **Python 3.10+** — stdlib only at runtime; `pytest` for dev.
- **COMSOL Multiphysics 6.4** — verified on `6.4.0.293` (Linux),
  `6.4.0.429` (Windows), `6.4.0.378` (macOS). JARs and the bundled JDK
  are discovered automatically; the native-library search path
  (`LD_LIBRARY_PATH` on Linux, `DYLD_LIBRARY_PATH` on macOS, `PATH` on
  Windows) is set to include `lib/{plat}` + `lib/{plat}/ext` +
  `ext/*/{plat}`. Other 6.x installs are untested —
  [docs/platform-setup.md](docs/platform-setup.md).
- **`pdftotext`** (poppler) — only for `scrape refmanual`.

No web server, no daemon, no Docker, no LLM transport. Everything is a
CLI subprocess or a single Python process.

---

## Contributing and license

Headless-COMSOL competence is scarce and mostly locked in individual
experts; this project exists to make it durable and teachable — a
curated gotcha catalog, task-oriented docs, and a reference
architecture for reliable simulation automation.
[CONTRIBUTING.md](CONTRIBUTING.md) says what contributions do that
best (measured gotchas first), the pull-request process, and what to
run before opening one; the community standard is the
[Contributor Covenant](CODE_OF_CONDUCT.md) and vulnerabilities go
through [SECURITY.md](SECURITY.md).

Licensed under the **Apache License 2.0** ([LICENSE](LICENSE),
[NOTICE](NOTICE)) — patent grant, corporate standard, and compatible
with the surrounding ecosystem (MPh, FEABench) and academic use.
Contributions are accepted under the same license (Apache-2.0 §5). The
knowledge base (`data/comsol.db`) is derived from COMSOL's own
documentation and is not distributed — the scrapers are; CI enforces
this ([docs/data-provenance.md](docs/data-provenance.md)).

## Project status

Actively used from long-running modeling campaigns through the verbs
in the table above; the real-COMSOL paths have been exercised on Linux,
Windows and macOS 6.4 installs (see the verification records linked
above). The most mature parts are the linting / dimensional analysis,
the run harnesses with their telemetry, and the knowledge base; API
lookup is retrieval the agent must apply, not a gate (see
[What is actually checked](#what-is-actually-checked)). Improvement backlog and known limitations are tracked in
[`gaps.md`](gaps.md); linter false-positive backlog in
[`LINT_BACKLOG.md`](LINT_BACKLOG.md).


---

comsol-support was authored by Jeremy Adams at Commonwealth Fusion Systems.
