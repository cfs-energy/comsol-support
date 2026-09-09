---
name: comsol-support
description: >
  Deterministic support layer for COMSOL Multiphysics 6.4 automation (6.4 only).
  Use whenever a task mentions COMSOL, .mph files, FlException,
  study.run, mphgen, edit-mph, query-mph, run-harness, COMSOL units
  linting, solver telemetry, or a COMSOL API type / property key.
  Provides the comsol-support CLI, the typed Java facade, the
  three-layer linter, and the knowledge / fragment / gotcha databases.
---

# comsol-support

A deterministic floor for COMSOL Multiphysics workflows. You (the
agent) plan and compose; comsol-support's machinery does the typed
Java glue, the linting, the ontology lookups, and the telemetry
plumbing. Nothing in it calls an LLM — the build orchestrator that
once sat on top was retired in 2026-08; its modeling knowledge is in
`docs/modeling-practice.md`.

**Design goal:** never *guess* a COMSOL API string — look up every
type name and property key (`comsol-support search`) or copy it from a
known-good fragment, and lint every unit before the solver sees it.
Be clear about what the tooling enforces: unit linting and telemetry
are checked by code; API-name correctness is *retrievable* by code but
*applied* by you — nothing rejects an unknown string (README, "What is
actually checked").

## Where it lives

The repo root contains the `comsol-support` CLI (run via `uv run
comsol-support …` from the repo root, or the installed console script
from anywhere). The COMSOL install is auto-discovered per platform
and overridable with the `COMSOL_PATH` environment variable. See
`INSTALL.md` / `docs/platform-setup.md` for setup on Windows / macOS /
Linux; `comsol-support doctor` checks a machine.

**Which database:** the knowledge base, fragments, telemetry and
catalogs live in one SQLite file — `<checkout>/data/comsol.db` from
any working directory (1.0; `COMSOL_DB` or `--db` override). If
`search` finds nothing, the file has not been populated: run
`comsol-support scrape javadoc` + `scrape refmanual` once, or point
`COMSOL_DB` at a populated one. **COMSOL 6.4 only** — the CLI warns
when another version is found.

## Reach for these first

| Task | Command |
|---|---|
| Compile a `.java` builder to an unsolved `.mph` | `comsol-support mphgen --builder B.java --output out.mph` |
| Load, mutate, solve, save an `.mph` | `comsol-support edit-mph --input in.mph --output out.mph [--mutator M.java] [--solve std1]` |
| Build a mesh on an existing model (census + per-feature records) | `comsol-support edit-mph --input in.mph --output out.mph --mesh mesh1` — read `docs/meshing.md` first |
| Read-only introspection (never saves); shipped probes by bare name | `comsol-support query-mph --input in.mph --query MeshStatsProbe` |
| Run any COMSOL-dependent Java class, teeing the full log | `comsol-support run-harness Harness.java` |
| Check license-seat availability (bounded probe) | `comsol-support license-status` |
| Lint a `.java` or `.mph` (units / descriptions / dimensional) | `comsol-support check <file>` |
| Look up an API type / property key (Javadoc + Reference Manual) | `comsol-support search "<query>" [--stage physics]` (MCP `search_api` is the same backend) |
| Search a recurring symptom against the gotcha catalog | `comsol-support gotcha-search "<symptom>"` |
| Enter a modeling stage (geometry, selections, physics, mesh, …) | read that stage in `docs/modeling-practice.md` |

## Rules of engagement

- Never call the COMSOL Java API ad hoc — go through the CLI or the
  `JavaFacade` so classpath, native-library paths, telemetry, and the
  one-JVM-at-a-time slot are handled for you.
- Never free-form a type or key (`FreeTriangular` when the API string
  is `FreeTri`; `"density"` when you mean `"rho"`): `comsol-support
  search` first. The verification ladder in `docs/modeling-practice.md`
  (known-good fragment → adapted exemplar → free synthesis with every
  `.create` type and `.set` key looked up) is yours to follow.
- Before debugging a solve failure, read the `.telemetry.jsonl`
  sidecar (`error_events`, not just `halt_reason` — a mutator that
  swallows its exception still reports `success`) and run
  `gotcha-search` on the symptom; failing `mphgen`/`edit-mph` already
  print matching gotcha ids.
- Solver runs hold floating-license seats: prefer `license-status`
  before long solves, and never leave JVMs running (the slot machinery
  warns about leaks).
- Use named selections (`SelectionAlgebra`), never integer entity
  indices, downstream of geometry — `docs/facade-conventions.md`.
- COMSOL 6.4 is the version ceiling — never plan around a newer
  release (`COMSOL_VERSIONS.md`).

## Doc pointers

- `README.md` — architecture, "What is actually checked", full command table
- `docs/modeling-practice.md` — stage-by-stage practice: working discipline, native-first ladders, mindsets / invariants / pitfalls / verified API quick-refs
- `docs/facade-conventions.md` — `TagRegistry` prefixes + `SelectionAlgebra` discipline
- `docs/mphgen.md`, `docs/mphedit.md`, `docs/meshing.md`, `docs/query-mph.md`,
  `docs/run-harness.md` — per-command reference
- `docs/linting.md` — three-layer linting architecture
- `docs/known-gotchas.md` — COMSOL 6.4 + headless-solve pitfalls (33 measured entries)
- `docs/solver-progress.md` — heartbeat pattern for long solves
- `INSTALL.md`, `docs/platform-setup.md` — Windows / macOS / Linux install
