# Installing comsol-support

> **You need COMSOL Multiphysics 6.4 installed and licensed on this
> machine.** comsol-support is built and verified against 6.4 only;
> with another version installed the CLI warns on every command and
> nothing downstream is guaranteed (`COMSOL_VERSIONS.md`). Nothing here
> downloads or installs COMSOL.

Works on **Linux**, **macOS**, and **Windows**. Pick your platform,
run one command, and you are done. Installation is an editable install
of this checkout; the command is `comsol-support` (`comsol-agent`, the
pre-1.0 name, still works as an alias).

> **Source checkout only.** Everything below installs the repository
> in place (`uv sync` = editable install). Building and installing a
> wheel is *not* supported: the wheel omits the Java sources, the probe
> library and `docs/known-gotchas.md`, and an installed wheel fails
> explicitly (`comsol-support doctor` → blocking "Source checkout";
> `scripts/wheel_smoke.sh` pins this).

```bash
git clone https://github.com/cfs-energy/comsol-support.git
cd comsol-support
```

> On a corporate network that inspects TLS, the clone itself may fail
> with `unable to get local issuer certificate`. Your proxy re-signs
> traffic with a private root that git's bundled CA list does not
> carry: `git config --global http.sslBackend schannel` (Windows) or
> point `http.sslCAInfo` at your organisation's CA bundle
> (Linux/macOS). Same cause as the `uv` case in
> [Troubleshooting](#troubleshooting).

| Platform | Install | Preflight only |
|---|---|---|
| Linux | `./scripts/install.sh` | `./scripts/install.sh --check` |
| macOS | `./scripts/install.sh` | `./scripts/install.sh --check` |
| Windows | `.\scripts\install.ps1` | `.\scripts\install.ps1 -Check` |

Both scripts are idempotent — re-run them freely. `--no-corpus` /
`-NoCorpus` skips the slow `.mph` mining pass.

**Stuck at any point, on any platform:**

```
python scripts/doctor.py      # works from a bare checkout, before installing
uv run comsol-support doctor    # after installing
```

`doctor` names what is missing *and how to fix it on your OS*. Both
installers run it, so a green preflight and a green `doctor` are the
same statement.

---

## What you need

| Requirement | Notes |
|---|---|
| **COMSOL Multiphysics 6.4** | Verified on `6.4.0.293` (Linux), `6.4.0.429` (Windows), `6.4.0.378` (macOS/Apple Silicon). The ontology, gotcha catalog, and slot catalog are all 6.4-specific. |
| **A COMSOL license** | Node-locked or floating. Nothing here needs a *running* COMSOL server — see [No server required](#no-server-required). |
| **`uv`** | Linux `curl -LsSf https://astral.sh/uv/install.sh \| sh` · macOS `brew install uv` · Windows `winget install astral-sh.uv` |
| **Python 3.10+** | Provisioned by `uv`; stdlib only at runtime. |
| **Java** | Not separately required — COMSOL ships its own JDK, which the package discovers and *prefers* (its JARs are built for Java 21; a system JDK of another vintage is the usual cause of `UnsupportedClassVersionError`). |
| **Poppler (`pdftotext`)** | Optional; only for `scrape refmanual`. Linux `apt install poppler-utils` · macOS `brew install poppler` · Windows `winget install oschwartz10612.Poppler` |

Everything else (JARs, the native library path, the SLF4J binding fix)
is resolved at runtime by `JavaFacade`.

> **Windows/winget:** newly installed tools land on `PATH` only for
> *new* terminals. `install.ps1` looks in the winget shim directories
> anyway, so it works in the shell you already have open.

## Pointing at your COMSOL install

When `COMSOL_PATH` is unset the package probes the conventional roots
for your OS and takes the first that exists:

| Platform | Probed, in order |
|---|---|
| Linux | `/usr/local/comsol64/multiphysics/`, `/opt/comsol64/multiphysics/` |
| macOS | `/Applications/COMSOL64/Multiphysics` |
| Windows | `C:\Program Files\COMSOL\COMSOL64\Multiphysics` |
| any | the install that owns a `comsol` launcher found on `PATH` (symlinks resolved; accepted only if that root carries `plugins/`) — probed after the rows above, before giving up |

Anywhere else, set `COMSOL_PATH` to the directory *containing*
`bin/` — the `multiphysics` directory, not the version directory above
it:

```bash
export COMSOL_PATH=/opt/comsol64/multiphysics/          # Linux/macOS
```
```powershell
$env:COMSOL_PATH = 'D:\COMSOL\COMSOL64\Multiphysics'    # Windows (session)
[Environment]::SetEnvironmentVariable('COMSOL_PATH', 'D:\COMSOL\COMSOL64\Multiphysics', 'User')
```

Every entry point reads the same variable, so setting it once in your
shell profile is enough. `doctor` prints which path it used and whether
it came from `COMSOL_PATH` or the probe.

## What the install produces

| Step | Result | Cost |
|---|---|---|
| `uv sync --extra dev` | `.venv/` + the `comsol-support` console script | seconds |
| Java compile | `.class` files for every COMSOL-dependent source | ~1 min |
| `scrape javadoc` | ~3,700 knowledge rows (446 API classes) | seconds |
| `scrape refmanual` | ~10,500 property records from the Programming Reference Manual PDF | seconds |
| `scrape corpus` | `.mph` → `.java` for the Application Libraries, mined into `fragments` | ~10 min, holds a license seat |
| `scrape slots` *(optional)* | Layer C prescriptive slot catalog | ~20 min–2 h, holds a license seat |

The knowledge base lands in `data/comsol.db`, which is gitignored — it
is rebuilt per machine, never shared.

> **Running from another directory.** `data/comsol.db` is resolved
> relative to the current directory, so `comsol-support` invoked from
> elsewhere addresses a *different*, empty database. Export
> `COMSOL_DB=/path/to/comsol-support/data/comsol.db` (the same variable
> the MCP server uses) to make every entry point agree.

## Verifying

```bash
uv run pytest -q                                  # 721 tests, ~80 s
COMSOL_E2E=1 uv run pytest -m real_comsol -q      # 29 gated tests, needs a license
```

Both suites should finish with zero failures. Tests that depend on
model fixtures outside this repo **skip**; they must never error.

## Things that will look like breakage but aren't

**`scrape corpus` reports a few hundred failures.** Expected. Two
causes, both benign, and the CLI names them in its summary:

- *Application Library preview stubs* — placeholder `.mph` files whose
  real content is downloaded on demand through the COMSOL GUI.
- *Missing module licenses* — a model needing the Acoustics Module
  cannot be opened without a seat for it.

On a reference Linux run, 612 of 875 models converted: 148 stubs and
115 license-gated. A count in that neighbourhood is healthy. **Zero
converted is not** — that means the pipeline is broken, not your
license. (`doctor` reports the stub count for your install.)

**SLF4J "multiple bindings" warnings.** Deliberate. COMSOL ships an
OSGi binding that cannot initialize on a flat classpath, so
`JavaFacade` prepends the plain JDK14 binding ahead of it. The warning
is the JVM narrating that fix.

**`comsol-support search` returns nothing on a fresh install.** That
subcommand searches past *builds*, not the API ontology. For the
ontology use the MCP `search_api` tool or
`comsol_support.db.search_knowledge`.

**Windows: Ω or µ render as `?` in the console.** Display only — the
data on disk and in the database is UTF-8 and correct. Windows
Terminal, or `chcp 65001`, renders it properly.

## No server required

comsol-support drives COMSOL through the standalone Java API
(`ModelUtil.initStandalone`). It talks to your license manager directly
and never connects to a COMSOL server, so **you do not need
`comsol mphserver` running** for `mphgen`, `edit-mph`, `check`,
`query-mph`, or any of the scrapers.

You only need a server for the things that are inherently
client/server: `mphclient`, MATLAB LiveLink, or the Python `mph`
package. Note that `comsol mphserver` prompts for a username and
password on first run and exits if it cannot read one — that credential
is COMSOL Server's own, unrelated to your FlexNet license.

## Troubleshooting

Run `python scripts/doctor.py` first: it checks COMSOL, the bundled
JDK, the plugin JARs, the doc trees, the optional tools, and the
knowledge base, and prints a per-OS remedy for anything missing. The
cases it cannot self-diagnose:

**`ModuleNotFoundError: comsol_support`** — `uv run` resolves the package
relative to the current project. Run from the repo root, or pass
`uv --project /path/to/comsol-support run comsol-support …`. The
installed console script does not care where it is run from.

**`COMSOL 6.4 not found`** — set `COMSOL_PATH` to the directory
containing `bin/` (see above). `doctor` echoes the path it tried.

**`UnsupportedClassVersionError` on any JVM launch** — a system JDK
older than 21 shadowed COMSOL's bundled one. Discovery prefers the
bundled JDK; this only appears when the install has none, so check
`doctor`'s "Java (JDK)" line.

**Corporate TLS interception** (`invalid peer certificate:
UnknownIssuer` from `uv`, or `unable to get local issuer certificate`
from `git`). Your proxy re-signs traffic with a private root that is in
the OS trust store but not in the tool's bundled one:

```bash
export UV_SYSTEM_CERTS=1          # uv >= 0.9  (older uv: UV_NATIVE_TLS=1)
git config --global http.sslBackend schannel   # Windows only
```

The installers export `UV_SYSTEM_CERTS` for you.

**A JVM outlives its command.** COMSOL leaves non-daemon threads
running, so any harness that returns from `main()` without
`System.exit()`/`Runtime.halt()` parks forever holding a license seat.
Find orphans with `pgrep -af 'jre/bin/java'` (Linux/macOS) or
`Get-Process java` (Windows) and kill them. `comsol-support` warns about
pre-existing COMSOL JVMs on all three platforms at launch. See
`docs/known-gotchas.md` → `G-DISCONNECT-HANGS`.

---

## Deeper reading

| Doc | Topic |
|---|---|
| [docs/platform-setup.md](docs/platform-setup.md) | Per-platform setup detail, COMSOL discovery, knowledge-base build |
| [docs/macos-setup.md](docs/macos-setup.md) | macOS specifics (Apple Silicon platform dirs, bundle-layout JDK) |
| [gaps.md](gaps.md) | Roadmap and known limitations |
| [README.md](README.md) | What the tool does and the full command table |
