# Data provenance and redistribution

comsol-support builds its knowledge from files that ship inside a
licensed COMSOL Multiphysics 6.4 install. This page states, artifact
by artifact, what is derived from what, what the repository ships, and
the rule the project follows: **the repository redistributes scrapers,
harvesters and hand-authored knowledge — never COMSOL-derived data.**
CI enforces the "never" half (`scripts/check_no_derived_data.sh`).

## Artifacts

| Artifact | Derived from | Where it lives | In the repo? |
|---|---|---|---|
| `knowledge` table (Javadoc rows: class / method / signature / description) | COMSOL's Javadoc HTML tree under `COMSOL_PATH/doc/help/…/api/` | `data/comsol.db` (or `$COMSOL_DB`) | **No** — rebuilt per machine by `comsol-support scrape javadoc` |
| `knowledge` table (Reference Manual rows: feature / property key / value type / default / description) | `COMSOL_PATH/doc/pdf/COMSOL_Multiphysics/COMSOL_ProgrammingReferenceManual.pdf` via `pdftotext` | `data/comsol.db` | **No** — `comsol-support scrape refmanual` |
| `fragments` table + `corpus/java/*.java` exports, `corpus/corpus_statistics.json`, `corpus/mph_list.txt` | the ~875 application-library models under `COMSOL_PATH/applications/` (converted `.mph → .java` with a licensed JVM) | `data/comsol.db`, `corpus/` | **No** — `comsol-support scrape corpus`; `corpus/` is git-ignored except its README |
| `slot_expected_units`, `variable_declared_units` tables + `data/slots_dump.jsonl` | property/unit slots harvested from the same application-library models | `data/comsol.db`, `data/slots_dump.jsonl` | **No** — `comsol-support scrape slots` (needs a license seat) |
| `native_interfaces`, `domain_synonyms` seed rows | hand-curated (interface identifiers are API facts; ranks and synonyms are ours) | `comsol_support/native_catalog.py` | Yes |
| `docs/known-gotchas.md`, `docs/meshing.md`, `docs/modeling-practice.md` | measured behaviour and practice written by the project; API identifiers quoted as facts | `docs/` | Yes |
| Test fixtures `tests/fixtures/javadoc/`, `tests/fixtures/refmanual/` | hand-authored in the Javadoc HTML / `pdftotext -layout` **layout**; identifiers, keys, value types and defaults are API facts; **all prose is synthetic** (`tests/fixtures/refmanual/README.md`) | `tests/fixtures/` | Yes |
| Test fixtures `tests/fixtures/corpus/*.java`, `fragments/*.json`, `mphgen/*.java`, `lint_selftest/*.java` | written for the tests | `tests/fixtures/` | Yes |
| COMSOL model files (`*.mph`) | not derived — they are *inputs*: either COMSOL's application library or the user's own models | `COMSOL_PATH/applications/`, or wherever the user keeps their work | **No** — never. The tools read models in place and copy none into the tree; every `.mph` name appearing in the repo is a placeholder in a test or a doc example |

## The rule

1. **Ship the process, not the product.** Scrapers (`javadoc_scraper.py`,
   `refmanual_scraper.py`, `corpus_miner.py`, `slot_harvest.py` +
   `SlotHarvester.java`) and the install scripts that run them are in
   the repo. Every database, dump, export and statistics file they
   produce is git-ignored (`*.db`, `data/slots_dump.jsonl`,
   `corpus/java/`, `corpus/*.json`, `corpus/mph_list.txt`) and is
   rebuilt on each machine from that machine's own COMSOL install,
   under that machine's COMSOL license.
2. **No documentation prose in the repo.** API identifiers (class,
   method, property-key and feature-type names, value types, defaults)
   are facts and may be quoted; sentences from the Javadoc or the
   manuals may not be copied into docs, fixtures or code comments.
   The sanitization pass replaced the last such prose in the scraper
   fixtures with synthetic text.
3. **Nothing site-specific either.** License-server hostnames, user
   home paths and internal benchmark assets stay out
   (`CONTRIBUTING.md`); site assets are reached through environment
   variables (`COMSOL_PATH`, `COMSOL_DB`, `COMSOL_REFERENCE_MPH`).
4. **Sharing a built database is the user's call under their COMSOL
   license, not the project's.** `COMSOL_DB` exists so one populated
   file can serve several workspaces on the same licensed site; the
   project does not publish one and does not recommend publishing one.

## Why this closes the question

Whether COMSOL's license permits redistributing documentation-derived
data is a question the project never has to answer as long as it does
not redistribute any: everything derived is regenerated locally by the
licensee, from the licensee's own install. The remaining exposure —
someone committing a `.db`, a corpus export or a model file by
accident — is what the CI guard removes.

## Guard

`scripts/check_no_derived_data.sh` (run by CI on every push and PR)
fails if any tracked file is

- a COMSOL model file (`.mph`, `.mphbin`, `.mphtxt`) — the user's own
  models and COMSOL's alike; none belongs in this repository,
- a product of the scrapers or harvesters (any `.db` / `.sqlite`, the
  slot dump, the corpus exports and statistics), or
- a fixture under `tests/fixtures/refmanual/` or
  `tests/fixtures/javadoc/` containing a line that looks like manual /
  Javadoc boilerplate copied verbatim.

`.gitignore` covers the same file types, but only stops an accidental
`git add`; the guard also catches a forced one, which is the case that
would otherwise reach a release. Run it locally before committing new
fixtures.
