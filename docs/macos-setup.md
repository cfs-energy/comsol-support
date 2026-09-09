# macOS setup — platform specifics

What differs on macOS, verified on Apple Silicon: the platform
directory and JDK layout, the JVM discovery order, the leaked-JVM
sweep, and the one standing limitation (COMSOL ships no model `.java`
exports on macOS).

Read [platform-setup.md](platform-setup.md) first for the general
install flow — this file covers only what is macOS-specific.

## macOS-specific behaviour and fixes

Three fixes in `comsol_support/java_facade.py`, one in
`comsol_support/jvm_slot.py`, one test-portability fix — all keep
Linux/Windows behavior intact.

### 1. Apple Silicon platform directory is `macarm64`, not `maca64`

The port guessed COMSOL's macOS ARM platform-subdirectory name as
`maca64`. A real 6.4 Apple Silicon install ships `bin/macarm64`,
`lib/macarm64`, `java/macarm64` — so JDK discovery and
`DYLD_LIBRARY_PATH` construction pointed at directories that don't
exist. `_platform_subdir()` now defaults to `macarm64` on macOS ARM
and, when it has a COMSOL root to look at, probes the install
(`bin/`, `lib/`, `java/`) so a renamed platform dir in a future
release degrades gracefully instead of failing silently.

### 2. macOS bundled JDK lives under `jre/Contents/Home/bin`

On Linux/Windows the COMSOL-bundled JDK binaries are at
`java/{plat}/jre/bin`. On macOS the JDK is a `.app`-style bundle:
`java/macarm64/jre/Contents/Home/bin/{java,javac}`. Discovery now
probes both layouts.

### 3. System JDK shadowed the bundled one — and was too old to load COMSOL

`find_java_executable()` preferred the system `PATH` over the
COMSOL-bundled JDK. macOS ships a `/usr/bin/javac` shim that resolved
to JDK 11 on this machine, while COMSOL 6.4's plugin JARs are compiled
for Java 21 (class file 61) — every JVM launch died with
`UnsupportedClassVersionError` before reaching COMSOL code. The search
order is now **bundled JDK → system PATH → JAVA_HOME** on every
platform: the bundled JDK is the only one guaranteed
version-compatible with the plugin JARs, and installs without one fall
through to `PATH` exactly as before.

### 4. Leaked-JVM scan now works on macOS

`jvm_slot.scan_leaked_jvms()` returned `[]` on macOS (no `/proc`).
It now sweeps `ps -axo pid=,etime=,rss=,args=`
output — same record shape (pid, age, RSS, cmdline head) as the Linux
`/proc` and Windows CIM paths, hardened to never raise. An explicit
`proc_root` argument still selects the `/proc`-style tree scan, so the
fake-`/proc` unit tests stay meaningful everywhere.

Live validation against the real slot-harvest JVM caught a bug in the
first cut: marker matching must be **case-insensitive** (as on
Windows), because the macOS install path spells it `COMSOL64` and the
`plugins/*` classpath wildcard keeps lowercase `com.comsol` jar names
off the command line — the harvest JVM's only marker was the
uppercase path. `SlotHarvester`/`ModelChecker` were also added to the
marker list (they are launched as main classes on every platform).
The fixed sweep detects a live 1.8 GB COMSOL JVM correctly.

### 5. Real-COMSOL sample picker chose physics-less models

`test_promote_catalog_end_to_end` picks the smallest full model per
module to convert; on this install the smallest "full" models are
`*_geom_sequence.mph` — geometry-construction companions with no
physics — which broke the "at least one physics class extracted"
invariant. `_full_models_only()` now excludes these companions
alongside the preview stubs.

## Known macOS limitations

### No shipped model `.java` exports on macOS

The license-free fragment-corpus shortcut (staging
`doc/help/wtpwebapps/ROOT/doc/com.comsol.help.models.*/<model>.java`,
platform-setup.md §5) does not work on macOS: the macOS COMSOL
distribution ships the model documentation (HTML/PDF) **without** the
`.java` exports — verified across 6.0/6.2/6.3/6.4 installs on this
machine, zero model `.java` files anywhere in the install trees.

Two ways to populate the fragment corpus on a Mac:

1. **Copy `corpus/java/` from a Linux or Windows machine** that has
   staged the shipped exports (the staged tree is plain `.java` text,
   ~906 files), then run
   `uv run comsol-support scrape corpus --skip-conversion --output-dir corpus`
   and `uv run comsol-support promote-catalog --java-dir corpus/java --db data/comsol.db`.
2. **Licensed conversion** (`scrape corpus` without
   `--skip-conversion`): converts `applications/*.mph` → `.java` with
   a real COMSOL JVM. Needs a license seat and hours.

Without the fragment corpus everything else still works — mphgen,
edit-mph, query-mph, linting, javadoc/refmanual search; only the MCP
`search_fragments` / `get_fragment` tools come back empty.

## macOS quick start (condensed)

```bash
brew install uv poppler
git clone https://github.com/cfs-energy/comsol-support
cd comsol-support
uv sync
uv run pytest -q                                   # default sweep, no COMSOL needed
uv run comsol-support scrape corpus --verify-only --output-dir corpus
uv run comsol-support scrape javadoc
uv run comsol-support scrape refmanual
mkdir -p ~/.claude/skills/comsol-support && cp skill/SKILL.md ~/.claude/skills/comsol-support/
uv run comsol-support license-status --timeout 60    # needs license-server reachability

# Licensed extras (one seat each):
COMSOL_E2E=1 uv run pytest -m real_comsol          # ~2 min
uv run comsol-support scrape slots --output corpus/harvest.slots.jsonl --harvest-only --timeout 14400   # ~20 min
uv run comsol-support scrape slots --aggregate-only --output corpus/harvest.slots.jsonl --clear-catalog
```

No JDK install is needed — the COMSOL-bundled JDK 21 is discovered
automatically (and preferred over any system JDK).
