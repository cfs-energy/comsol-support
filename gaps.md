# Roadmap and known limitations — comsol-support

What is deliberately not done yet, and why. Items are listed only when
they would change what a user of this package can do; the reasoning is
kept so that picking one up does not start from scratch.

If you hit something here and need it, say so in an issue — several of
these are deferred for want of a concrete use, not for difficulty.

## Planned

**Real-COMSOL coverage in continuous integration.**
The COMSOL-free suite runs on Linux, macOS and Windows in CI. The
licensed suite (`-m real_comsol`) is verified by hand on each platform
before a release; the workflow to automate it exists
(`.github/workflows/real-comsol.yml`) but stays inert until a
self-hosted runner with COMSOL 6.4 is registered. Seat discipline is
already built into the workflow — a single-seat concurrency group and a
`license-status` probe before the suite runs. Next steps: register the
runner, trigger once by hand, then move to a nightly schedule and make
it a required check for changes to the Java facade, the generators or
the scrapers.

**A symptom-indexed `diagnose` command.**
`gotcha-search` covers the gotcha catalog and the MCP server exposes
fragment search, so a `diagnose` command would today be a thin
re-exposure of two surfaces that already exist. It becomes worthwhile
when there is a third source to fan out to — a durable record of prior
failures worth searching across.

**A feedback loop from finished work back into the catalogs.**
A command that walks a concluded body of modeling work, promotes
gotcha-class findings into `docs/known-gotchas.md`, mines reusable
patterns into the fragment corpus, and appends unit-mismatch findings
to the slot catalog. The mapping rules should be established by doing
the harvest manually once; automating before that would be guesswork.

## Known limitations

**The default JVM heap (`-Xmx48g`) is not RAM-adaptive.**
It is a cap rather than a reservation, so it is harmless on large
machines, but on a small-RAM box a large solve will be killed by the
operating system rather than bounded cleanly by the JVM. Override per
run with `--jvm-arg`.

**The Windows version probe reports no build number.**
`verify_environment` reads `about.txt` and resolves "6.4" but not the
patch build. `comsol.exe --version` cannot be used because it opens the
GUI. Version-sensitive behaviour keys on 6.4, not the build, so this is
cosmetic; the build number is visible in the installer log and in the
executable's file properties.

**Legacy Windows consoles can render Ω and µ as mojibake.**
Package I/O is pinned to UTF-8 and the data written to disk and to the
database is correct, but a cp1252 PowerShell 5.1 console may display
those glyphs wrongly. Windows Terminal, or `chcp 65001`, renders them
correctly.

**Preview stubs cap the corpus.**
A COMSOL installation ships part of its Application Libraries as small
placeholder files that `ModelUtil.load` refuses, which limits how many
models the slot harvest can read. Downloading the full libraries
through the COMSOL GUI and re-running `scrape slots` extends the
corpus. This is a property of the installation, not of this package.

**macOS installations ship no model `.java` exports.**
The license-free route to a fragment corpus — reading COMSOL's own
Java exports of the bundled models — exists only on Linux and Windows.
On macOS, either copy a staged `corpus/java/` from another platform or
run the licensed `.mph → .java` conversion. See `docs/macos-setup.md`.

**Some tests need a local reference model.**
The runtime-lint tests require a solved `.mph` that is not part of this
repository: set `COMSOL_REFERENCE_MPH` to it, and `COMSOL_REFERENCE_JAVA`
to a builder source for the Layer A false-positive check. Both skip
cleanly, naming the missing variable, when unset. The assertions are
structural, so any sufficiently rich model works.

**One catalog test depends on a free license seat.**
`test_promote_catalog_preserves_curated_metadata` converts a two-model
sample; if the site license lacks the modules those models need, or no
seat is free, it skips. Choosing a sample that needs only the base
product would remove the dependence. The skip message names the
converter's license error.

**Linter backlog.**
Known false-positive and precision issues in the Java linter are
tracked with fix sketches in `LINT_BACKLOG.md`.

## Not planned here

**Wiring the MCP tools into a consuming agent's allow-list.**
The MCP server is functional; what is missing is an entry in the
*consumer's* configuration. That file belongs to the consumer, not to
this package, so the integration is documented rather than automated —
start the server with `python -m comsol_support.mcp_server`, point
`COMSOL_DB` at the database you want it to read, and add the tools to
your agent's allow-list.

**Refactoring `ModelExporter`'s inlined solver heartbeat.**
Sharing the standalone `SolverHeartbeat` implementation would remove
roughly fifty duplicated lines, but the inlined version is covered by
the live `mphgen` tests and the refactor buys little. Worth revisiting
only if `SolverHeartbeat` grows behaviour that `ModelExporter` wants.
