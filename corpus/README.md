# corpus/

Working directory for corpus mining (`comsol-support scrape corpus` and the
install scripts write here). Everything generated under it — the
`.mph → .java` exports in `java/`, `java_compiled/`, `mph_list.txt`,
`corpus_statistics.json` — is machine-specific and git-ignored; regenerate
per machine (see `docs/platform-setup.md`). The ad-hoc PDF probe scripts
that used to live here were removed 2026-08-25 (`docs/gaps.md` #5); the
real scraper is `comsol_support/refmanual_scraper.py`.
