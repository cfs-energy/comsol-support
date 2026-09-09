# comsol-support CHANGELOG

## 1.0.0 — first release

First public release. `comsol-support` is a deterministic support layer
for COMSOL Multiphysics 6.4 automation: it generates, edits, runs and
lints COMSOL model code, and answers API questions from a local
knowledge base. Nothing in it calls an LLM — a calling agent (or a
person) plans and composes; this package supplies the typed Java glue,
the lookups, the linting and the telemetry.

### Requirements and install

- **COMSOL Multiphysics 6.4.** Other versions are unverified; the CLI
  prints one stderr warning when the discovered install is not 6.4, and
  `COMSOL_VERSIONS.md` explains what would break.
- **Source-checkout install only.** The package needs
  `comsol_support/java/**/*.java` and `docs/known-gotchas.md` at run
  time, which a wheel does not carry; installing a built wheel fails
  with an explicit message.
- **Python 3.10–3.13**, verified in CI (3.10–3.13 on Linux, 3.12 on
  macOS and Windows).
- **Zero external runtime dependencies** — standard library only.
- **Linux, macOS and Windows** are first-class. `scripts/install.sh`
  (Linux/macOS) and `scripts/install.ps1` (Windows) set a checkout up;
  `comsol-support doctor` diagnoses a machine. See `INSTALL.md`.

### What is in the release

- **CLI:** `search`, `scrape`, `slot-stats`, `mphgen`, `edit-mph`,
  `run-harness`, `query-mph`, `license-status`, `check`, `doctor`,
  `gotcha-search`, `ingest-telemetry`, `promote-catalog`.
- **MCP server** exposing 10 tools for agent consumers.
- **Typed Java facade** (`TagRegistry`, `SelectionAlgebra`,
  `ModelExporter`, `ModelChecker`, `SlotHarvester`, `SolverHeartbeat`,
  `SolverTelemetry`, and a probe library) over `com.comsol.model.*`.
- **Three-layer linter** for COMSOL Java builders, including
  dimensional analysis in pure Python.
- **Knowledge base** (SQLite + FTS5) built by three ingest pipelines:
  a Javadoc ontology scraper, a Reference Manual property-key
  extractor, and a corpus miner over the bundled application models.
  The database is built locally and is never distributed — see
  `docs/data-provenance.md`.
- **Gotcha catalog** (`docs/known-gotchas.md`) with a search CLI.
- **Solver telemetry** sidecars and an ingest path for cross-run search.

### Naming

The distribution, CLI and import name are `comsol-support` /
`comsol_support`. The pre-1.0 names remain as working aliases: the
`comsol-agent` console script, and a `comsol_agent` shim package that
registers every `comsol_support.<module>` under the old name as the
same module object. Pre-1.0 MCP configs keep working. The
`COMSOL_AGENT_EXCLUSIVE` and `COMSOL_AGENT_SLOT_TIMEOUT` environment
variables keep their names.

### Database location

The default database resolves to `<checkout>/data/comsol.db` from any
working directory. `COMSOL_DB` and `--db` override it.

---

*This project was developed privately before 1.0.0. The pre-release
development log is not reproduced here; `git log` retains it.*
