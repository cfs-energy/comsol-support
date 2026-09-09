# Contributing to comsol-support

## Why this project exists

Headless-COMSOL competence is scarce. It is learned by painful trial
and error — the `ModelUtil.disconnect()` that hangs, the swept mesh
that "succeeds" with zero elements, the exception a mutator has to
swallow to save a partial mesh — and it is currently locked in the
heads of individual experts. comsol-support exists to make that
competence **durable and teachable**:

- a **curated gotcha catalog** of measured failure modes with root
  causes and workarounds (`docs/known-gotchas.md`, searchable with
  `comsol-support gotcha-search` and the MCP `search_gotchas` tool);
- **task-oriented docs** — how to build, edit, mesh, solve, probe and
  lint a model from Java, and what to do when it fails
  (`docs/mphgen.md`, `docs/mphedit.md`, `docs/meshing.md`,
  `docs/query-mph.md`, `docs/linting.md`, `docs/modeling-practice.md`);
- a **reference architecture for reliable simulation automation**:
  stage-transactional builds (compile and dry-run before any solve;
  atomic in-place edits; named selections instead of entity indices),
  tiered synthesis (compose from known-good fragments before adapting
  exemplars before free synthesis, with every API string looked up
  first), and corpus-grounded validation (Layer C dimensional analysis
  against a slot catalog harvested from the 875-model application
  library; API knowledge scraped from the shipped Javadoc and
  Reference Manual).

Every contribution should make one of those three things more
complete, more correct, or easier to apply. The repository is currently
private to its owning organisation and is being prepared for an
open-source release under the Apache License 2.0 (see `LICENSE`); this
policy is written for that release.

## How to contribute

The standard pull-request process — no CLA, no mailing list. The
community standard is the [Contributor Covenant](CODE_OF_CONDUCT.md);
security problems go through [SECURITY.md](SECURITY.md), not issues.

1. Fork (or branch, if you have write access) from `main`. Fetch first;
   `main` moves.
2. Make the change with its tests and docs (below).
3. Open a pull request (the template asks the questions below) that
   says **what you measured**: the COMSOL build (`comsol-support doctor`
   output), what was run against a real install and what was mocked. "Tests pass" on a machine without
   COMSOL means the Python tests passed — say so.
4. CI runs the COMSOL-free suite on Linux, macOS and Windows, the
   lint + provenance guard, and the wheel smoke; the real-COMSOL checks
   (`.github/workflows/real-comsol.yml` on a self-hosted runner, or a
   maintainer by hand) run before merging anything that touches Java,
   the facade, the run harnesses or the scrapers.

By submitting a pull request you agree that your contribution is
licensed under the Apache License 2.0, the same license as the
project (Apache-2.0 §5, "Submission of Contributions"). Keep the
license header/notice files intact; do not add code you do not have
the right to license this way.

## What is most valuable

In rough order:

1. **Measured gotchas.** A COMSOL behaviour you hit through this
   tooling, root-caused, with the workaround — as a new
   `G-DOMAIN-PHENOMENON` entry in `docs/known-gotchas.md` (follow the
   entry format there; `comsol-support gotcha-search` must find it).
   Include the COMSOL build number and the telemetry sidecar or probe
   output that shows it. This is the single highest-leverage
   contribution: one measured entry saves the next person the days
   you spent.
2. **Doctrine and task docs.** Corrections and additions to
   `docs/modeling-practice.md` (mindsets / invariants / pitfalls /
   API quick-refs — verify API names against the Programming Reference
   Manual, mark anything unverified ⚠), `docs/meshing.md`, and the
   per-command docs. A doc change that turns a tribal rule into a
   written one counts as much as code.
3. **Probes and mutators that generalise.** Read-only `query(Model,
   Map)` probes for `comsol_support/java/probes/` and reusable mutators,
   written against the contracts in `docs/query-mph.md` /
   `docs/mphedit.md`, topic-agnostic (no model-specific tags baked in).
4. **Platform and install fixes** — anything `comsol-support doctor`,
   `scripts/install.*` or a fresh clone gets wrong on your machine.
5. **Linter precision.** False positives/negatives in Layers A/B/C,
   with a minimal `.java` or expression that reproduces them
   (`LINT_BACKLOG.md` lists known ones).
6. **Knowledge-base coverage** — scraper fixes that add missing API
   types/property keys or correct their stage classification.

Please open an issue before starting on: new LLM/agent layers inside
this repo (the calling agent owns planning — `docs/orchestrator-
deprecation-plan.md` records why the previous one was removed; the
architecture is meant to be consumed by agents, not to contain one),
support for COMSOL versions other than 6.4, new external runtime
dependencies (stdlib-only is deliberate), or anything model-,
campaign- or site-specific (that belongs in your workspace, using the
`templates/`).

## Before you open a pull request

- **Run the suite.** `uv run pytest -q` must be green. CI runs it
  without COMSOL; if you have COMSOL 6.4, run it there too, and run the
  gated real-COMSOL tests if you touched Java, the facade, mphgen /
  edit-mph / query-mph, or the scrapers:
  `COMSOL_E2E=1 uv run pytest -m real_comsol` (needs a license seat;
  `INSTALL.md`).
- **Compile the Java.** `tests/test_lint_selftest.py` compiles every
  source in `comsol_support/java/` against the COMSOL classpath — it only
  runs where COMSOL is installed, and it is the check that would have
  caught the historical `SlotHarvester` regression.
- **Lint.** `uvx ruff check --select F comsol_support tests scripts`
  (what CI enforces — the full pyflakes rule set).
- **Provenance guard.** `bash scripts/check_no_derived_data.sh` (CI
  runs it): no COMSOL model file (`.mph`) tracked, no scraped database,
  corpus export or slot dump tracked, no COMSOL documentation prose in
  fixtures, no site-specific strings (`docs/data-provenance.md`).
- **Document behaviour, not just code.** A new verb or flag needs its
  `docs/*.md` section and a CHANGELOG entry; a new module needs a
  MANIFEST row. If you found a limitation you are not fixing, add it
  to `gaps.md`.
- **Keep it topic-agnostic and site-agnostic.** Nothing model-,
  campaign- or site-specific goes into `comsol_support/`, `docs/` or
  `tests/fixtures/` — no hostnames, user home paths or benchmark
  assets (use environment variables such as `COMSOL_REFERENCE_MPH`).
- **One concern per PR**, squashed fixups, a commit message that says
  *why* and *what was verified*.

## Working in the repo

- Source checkout only: `git clone` + `uv sync` (`INSTALL.md`). Wheels
  are not a supported distribution — `scripts/wheel_smoke.sh` asserts
  that a wheel install fails explicitly.
- One SQLite database (`data/comsol.db` or `$COMSOL_DB`) holds the
  knowledge base, fragments, telemetry and catalogs; never commit it.
  The knowledge base is derived from COMSOL's own documentation —
  redistribute the *scrapers*, not the scraped database
  (`docs/data-provenance.md`).
- Read `README.md` "What is actually checked" before claiming a
  guarantee in a doc: unit linting and telemetry are enforced by code;
  API-name correctness is retrievable by code but applied by the
  caller.

## Reporting problems

Open an issue with: COMSOL build (`comsol-support doctor` output), OS,
the exact command, and the sidecars it produced
(`*.telemetry.jsonl`, `*.jvm.log`, `*.mphedit.json` / `*.mphgen.json`).
Search `docs/known-gotchas.md` (or `comsol-support gotcha-search
"<symptom>"`) first — many solver and mesh failures are already
catalogued with a workaround. If yours is, and the workaround did not
apply, that is itself a valuable report.
