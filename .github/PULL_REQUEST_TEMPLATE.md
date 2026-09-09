<!-- Thanks. CONTRIBUTING.md has the full checklist; the questions below
     are the ones reviewers cannot answer for you. Delete what does not apply. -->

## What and why

<!-- One paragraph: the behaviour before, the behaviour after, and why. -->

## What was measured

- COMSOL build (`comsol-support doctor` output, first lines):
- Platform(s):
- Run against a **real** COMSOL install: <!-- which commands / tests -->
- Mocked only: <!-- what was NOT exercised for real -->

## Checklist

- [ ] `uv run pytest -q` green (say where: with / without COMSOL)
- [ ] `COMSOL_E2E=1 uv run pytest -m real_comsol` green — required if this touches `comsol_support/java/`, the facade, mphgen / edit-mph / query-mph / run-harness, or the scrapers
- [ ] `uvx ruff check --select F comsol_support tests scripts` and `bash scripts/check_no_derived_data.sh` clean
- [ ] Docs updated (`docs/*.md` section, `CHANGELOG.md`, `MANIFEST.md` row for new modules)
- [ ] Nothing model-, campaign- or site-specific added (no hostnames, home paths, benchmark assets, COMSOL documentation prose)
- [ ] New gotcha entries follow the `G-DOMAIN-PHENOMENON` format and are findable with `comsol-support gotcha-search`
