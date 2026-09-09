# Platform setup — Windows / macOS / Linux

> **Installing?** Start at [INSTALL.md](../INSTALL.md) — one command
> per platform, plus `python scripts/doctor.py` when something is
> missing. This document is the reference behind it: what the ports
> changed, why, and the verification record for each platform.

What each supported platform needs, what the cross-platform port
and the macOS verification changed, and how
to verify a fresh install end to end.

**Status by platform**

| Platform | Status |
|---|---|
| Linux | Original development platform. Verified against COMSOL 6.4.0.293. |
| Windows | Fully ported and verified against COMSOL 6.4.0.429 (`C:\Program Files\COMSOL\COMSOL64`): the default sweep passes, the license probe checks out a real seat, and Javadoc + RefManual + fragment corpus extract cleanly. |
| macOS | Fully ported and verified against COMSOL 6.4.0.378 on Apple Silicon (`/Applications/COMSOL64/Multiphysics`): default sweep green, real-COMSOL suite 25/25, license probe checks out a real seat, Javadoc + RefManual extracted, slot catalog harvested (14,416 keys), Layer C validated end to end. Five defects fixed (`macarm64` platform dir, JDK bundle layout, bundled-JDK preference, leak-sweep case fold, sample picker) — see [macos-setup.md](macos-setup.md). One limitation: no shipped model `.java` exports, so the fragment corpus needs licensed conversion or a staged copy from another platform. |

---

## 1. Prerequisites

- **COMSOL Multiphysics 6.4** installed locally. Nothing newer
  (`COMSOL_VERSIONS.md` — 6.4 is the ceiling). No separate JDK is
  needed: the JDK bundled with COMSOL (`java/{plat}/jre/bin`; on
  macOS `java/{plat}/jre/Contents/Home/bin`) is discovered
  automatically, including `javac`, and is **preferred over any
  system JDK** — it is the only one guaranteed version-compatible
  with the COMSOL plugin JARs (6.4 ships Java 21 class files; the
  macOS `/usr/bin/javac` shim was JDK 11 and could not load them).
- **Python ≥ 3.10** and **[uv](https://docs.astral.sh/uv/)**.
  - Windows: `winget install astral-sh.uv`
  - macOS: `brew install uv`
  - Linux: `curl -LsSf https://astral.sh/uv/install.sh | sh`
- **Poppler (`pdftotext`)** — only needed for `scrape refmanual`.
  - Windows: `winget install oschwartz10612.Poppler` (or
    `conda install -c conda-forge poppler`). Ensure the Poppler `bin`
    directory ends up on `PATH` (a new terminal after winget install).
  - macOS: `brew install poppler`
  - Linux: `apt install poppler-utils`

Hardware note: the JVM default heap cap is `-Xmx48g` (a cap, not a
reservation — fine on smaller machines, but big solves want real RAM).
Override per run with `--jvm-arg` on `run-harness`.

## 2. Install

```bash
git clone https://github.com/cfs-energy/comsol-support
cd comsol-support
uv sync          # installs the package + dev group (pytest)
uv run comsol-support --help
```

`uv sync` now installs pytest by default via the PEP 735
`[dependency-groups]` table (previously it was only an optional
extra, so `uv run pytest` failed on a fresh clone). pip users:
`pip install -e .[dev]`.

Windows on a managed corporate network: if git fails with
`SSL certificate … unable to get local issuer certificate`, the
TLS-inspecting proxy's root CA lives in the Windows cert store but
not in git's bundled OpenSSL CA file — fix with
`git config http.sslBackend schannel`.

## 3. COMSOL discovery

`comsol_support.COMSOL_PATH` is resolved at import time:

1. The `COMSOL_PATH` environment variable, if set (and non-empty).
2. Otherwise the first *existing* directory among the platform's
   defaults:

| Platform | Probed roots (in order) |
|---|---|
| Linux | `/usr/local/comsol64/multiphysics/`, `/opt/comsol64/multiphysics/` |
| Windows | `C:\Program Files\COMSOL\COMSOL64\Multiphysics` |
| macOS | `/Applications/COMSOL64/Multiphysics` |
| any | the install that owns a `comsol` launcher found on `PATH` (symlinks resolved; accepted only if that root carries `plugins/`) — probed after the rows above, before giving up |

Non-standard install location → set `COMSOL_PATH` (User environment
variable on Windows, shell profile export elsewhere).

## 4. Verify the install

```bash
# 1. Default test sweep (no COMSOL runtime, no license) — ~1 min
uv run pytest -q

# 2. Environment verification (JARs, corpus, disk; no license)
uv run comsol-support scrape corpus --verify-only --output-dir corpus

# 3. Bounded license checkout probe (compiles a probe class with the
#    bundled JDK, launches a real JVM, checks out + releases one seat)
uv run comsol-support license-status --timeout 60

# 4. Full real-COMSOL integration suite (opt-in; holds seats briefly)
COMSOL_E2E=1 uv run pytest -m real_comsol
```

On Windows (PowerShell), step 4 is
`$env:COMSOL_E2E = "1"; uv run pytest -m real_comsol`.

## 5. Build the knowledge base (documentation corpus)

The SQLite knowledge base (`data/comsol.db`) is **not** checked in —
each machine extracts it from its local COMSOL install. All defaults
derive from the discovered `COMSOL_PATH`; no path flags needed on a
standard install.

```bash
# Javadoc API ontology (446 classes / ~3.7k rows; seconds; no license)
uv run comsol-support scrape javadoc

# Programming Reference Manual property tables
# (~10.5k rows; needs pdftotext; no license)
uv run comsol-support scrape refmanual

# Fragment corpus from COMSOL's shipped model .java exports
# (906 models -> ~355 fragments; no license) — see below
uv run comsol-support scrape corpus --skip-conversion --output-dir corpus

# Native-interface catalog corpus frequencies (pure Python)
uv run comsol-support promote-catalog --java-dir corpus/java --db data/comsol.db
```

### Fragment corpus without a license seat

The original Linux pipeline converted `applications/*.mph` to `.java`
via a licensed COMSOL JVM (hours). COMSOL also **ships** the same
model exports as `.java` files inside its documentation tree:

```
{COMSOL_PATH}/doc/help/wtpwebapps/ROOT/doc/com.comsol.help.models.<module>.<model>/<model>.java
```

Staging those into `corpus/java/<module>/` and running
`scrape corpus --skip-conversion` produces the fragment corpus with
no license and in ~2 minutes. PowerShell staging one-liner:

```powershell
$src = "$env:COMSOL_PATH\doc\help\wtpwebapps\ROOT\doc"   # or the default install root
$dst = "corpus\java"
Get-ChildItem $src -Directory -Filter "com.comsol.help.models.*" | ForEach-Object {
  $module = ($_.Name -split '\.')[4]
  New-Item -ItemType Directory -Force (Join-Path $dst $module) | Out-Null
  Get-ChildItem $_.FullName -Filter *.java |
    Where-Object Extension -eq '.java' |
    Copy-Item -Destination (Join-Path $dst $module) -Force
}
```

Equivalent bash (Linux):

```bash
src="${COMSOL_PATH:-/usr/local/comsol64/multiphysics}/doc/help/wtpwebapps/ROOT/doc"
for d in "$src"/com.comsol.help.models.*; do
  module=$(basename "$d" | cut -d. -f5)
  files=("$d"/*.java)
  [ -e "${files[0]}" ] || continue
  mkdir -p "corpus/java/$module"
  cp "${files[@]}" "corpus/java/$module/"
done
```

**macOS caveat**: the macOS COMSOL distribution ships the model docs
*without* the `.java` exports (verified on 6.0–6.4 installs — zero
model `.java` files anywhere in the install tree), so this shortcut
only works on Linux/Windows. On a Mac, either copy a staged
`corpus/java/` tree from another platform or use the licensed
conversion — see [macos-setup.md](macos-setup.md).

The licensed `.mph → .java` conversion path remains available
(`scrape corpus` without `--skip-conversion`) and is still the way to
mine models that have no shipped export.

### Slot expected-unit catalog (licensed, ~20 min)

Layer C's prescriptive catalog (`slot_expected_units`) is harvested
from the `.mph` corpus by a real COMSOL JVM (single JVM, one license
seat for the whole sweep). Name the dump `*.slots.jsonl` so it stays
git-ignored, and smoke-test before the full sweep:

```bash
# smoke first (~1 min) — verify records + version stamp
uv run comsol-support scrape slots --output corpus/smoke.slots.jsonl \
    --max-models 5 --harvest-only
# full sweep (~20 min on 875 models), then aggregate
uv run comsol-support scrape slots --output corpus/harvest.slots.jsonl \
    --harvest-only --timeout 14400
uv run comsol-support scrape slots --aggregate-only \
    --output corpus/harvest.slots.jsonl --clear-catalog
```

The COMSOL version stamped into records is auto-detected from the
install directory (case-insensitive, so `COMSOL64` works) and
overridable with `--version`. Models requiring modules absent from
your license fail to load and are skipped (195 of 875 here) — the
catalog covers the loadable corpus. `variable_declared_units`
(Source B) came back empty on this corpus: application-library models
carry units inside expressions, not as variable declarations — so the
SymbolResolver seed stays empty and Layer C relies on the slot
catalog + expression inference. Without the catalog, Layer C still
runs in descriptive mode — linting works, it just doesn't
contract-check units against corpus expectations.

## 6. Claude skill registration

Copy [`skill/SKILL.md`](../skill/SKILL.md) to:

- Linux/macOS: `~/.claude/skills/comsol-support/SKILL.md`
- Windows: `%USERPROFILE%\.claude\skills\comsol-support\SKILL.md`

---

## 7. What the cross-platform port changed

All changes keep Linux behavior identical; Windows gains first-class
support; macOS gains pathing (later corrected on real hardware —
the macOS verification fixed the platform-dir name, the
JDK bundle layout, and the JDK search order; see
[macos-setup.md](macos-setup.md)).

### COMSOL discovery & defaults

- `comsol_support/__init__.py` — `COMSOL_PATH` now falls back to
  per-platform default probing (table above) instead of a hardcoded
  site-specific string. An *empty* `COMSOL_PATH` env var now
  also falls through to probing.
- `comsol_support/cli.py` — the four `scrape` subcommands
  (`javadoc` api-dir, `refmanual` pdf, `corpus`/`slots` comsol-path)
  derive their defaults from `COMSOL_PATH` instead of hardcoded
  site-specific paths.
- `tests/test_real_comsol_integration.py`,
  `tests/test_javadoc_scraper.py`, `tests/test_refmanual_scraper.py`
  — real-install test paths derive from `COMSOL_PATH`, so the gated
  suites run on any platform with COMSOL present.

### Java facade (`comsol_support/java_facade.py`)

- **Classpath**: `get_full_classpath()` now supplies plugins via a
  single `plugins/*` classpath wildcard (expanded natively by
  `java`/`javac`) instead of enumerating 329 absolute JAR paths.
  Explicit enumeration exceeded Windows' ~32 KB command-line limit
  (`WinError 206`). Entry order is preserved, so the standalone
  SLF4J binding still precedes the OSGi one.
- **Bundled JDK discovery**: probes `javac.exe`/`java.exe` as well as
  the bare names under `java/{plat}/jre/bin` and `JAVA_HOME/bin`
  (the Windows COMSOL "jre" is a full JDK and includes `javac.exe` —
  verified).
- **Native libraries**: `get_comsol_env()` prepends the COMSOL
  `lib/win64` + `ext/*/win64` directories to `PATH` on Windows
  (Windows resolves DLLs via `PATH`; `LD_LIBRARY_PATH` is a no-op
  there). Linux/macOS behavior unchanged.
- **Launcher discovery**: new `find_comsol_launcher()` — `bin/comsol`
  on POSIX, `bin/win64/comsol.exe` on Windows.
- **Signals**: `signal.SIGKILL` does not exist on Windows; a module
  constant (`_SIGKILL = getattr(signal, "SIGKILL", 9)`) guards the
  POSIX group-kill path. `describe_abnormal_exit()` decodes `-N`
  returncodes against a fixed POSIX signal table so Linux-recorded
  telemetry decodes identically on any platform (Windows numbers
  SIGABRT as 22 and lacks SIGKILL entirely).

### Subprocess & file-I/O encoding

Windows still defaults text I/O to the legacy locale codepage
(cp1252), which mangles or crashes on Ω, µ, °, — routinely present in
COMSOL sources and docs. All load-bearing text I/O is now explicit:

- Every `subprocess` call that captures javac/JVM/COMSOL/
  pdftotext output passes `encoding="utf-8", errors="replace"`.
- `.java` source reads (mphgen/edit-mph contract checks, linting
  Layer A, corpus parsing, `_public_class_name`), JSON sidecar
  reads/writes, gotcha-doc reads, and
  config reads are explicit UTF-8.
- `pdftotext` is invoked with `-enc UTF-8` so its output encoding is
  pinned on every platform.

### Process management

- `comsol_support/cli.py` — background builds (`build -b`) detach with
  `creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP` on
  Windows (`start_new_session` is a silent no-op there).
- `comsol_support/claude_cli.py` — subprocess `cwd` falls back to
  `tempfile.gettempdir()` instead of `/tmp`. *(File since removed with the
  orchestrator; kept here as the port record.)*
- `comsol_support/jvm_slot.py` — the one-COMSOL-JVM advisory slot now
  works on Windows via an `msvcrt` byte-range lock (at a high file
  offset so the holder-info JSON stays readable by contenders, since
  Windows locks are mandatory). POSIX `flock` path unchanged;
  other platforms still no-op. The leaked-JVM `/proc` scan remains
  Linux-only by design.
- `comsol_support/corpus_miner.py` — `verify_environment()` uses
  `shutil.disk_usage` (`os.statvfs` does not exist on Windows) and
  `find_comsol_launcher()`. On Windows it reads the COMSOL version
  from `about.txt` instead of running `comsol.exe --version`, which
  opens the GUI and orphans a `ComsolUI` child past the probe's
  timeout.

### Packaging & docs

- `pyproject.toml` — added PEP 735 `[dependency-groups] dev` so
  `uv sync` installs pytest by default (the `[project.optional-dependencies]`
  extra is kept for pip). `uv.lock` regenerated accordingly.
- `skill/SKILL.md` — the Claude skill definition is now versioned in
  the repo (previously it lived only in a home directory).
- README, `COMSOL_VERSIONS.md` — discovery, dependency, and skill
  sections updated; this document added.
- `.gitignore` — corpus staging/output artifacts
  (`corpus/java/`, `mph_list.txt`, `corpus_statistics.json`,
  `exemplars.json`) are generated per machine and ignored.

### Test-suite portability fixes

- `tests/test_cli.py` — background-detach assertion is
  platform-conditional.
- `tests/test_java_facade.py` — classpath assertions updated to the
  wildcard contract; POSIX-only `os.getpgid`/`os.killpg` patches use
  `create=True`; SIGKILL assertion uses the module's fallback.
- `tests/test_jvm_slot.py` — slot tests now run on Windows too.
- `tests/test_lint_selftest.py` — SLF4J-ordering test splits the
  classpath on `os.pathsep` (was `":"`, which breaks on `C:\` drive
  letters) and asserts against the wildcard entry.
- `tests/test_linting.py`, `tests/test_telemetry.py` — fixtures write
  UTF-8 explicitly and JSON-escape embedded Windows paths (the real
  Java emitters already escape via `escapeJson`; only the fixtures
  were unescaped).
