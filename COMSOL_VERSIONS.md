# COMSOL versions

**COMSOL Multiphysics 6.4 is the target and the ceiling.** The
knowledge base (Javadoc + Reference Manual scrape), the gotcha catalog,
the slot expected-unit catalog and every real-COMSOL test were built
and measured against 6.4:

| Platform | Build verified | Record |
|---|---|---|
| Linux | 6.4.0.293 | this repository's default sweep + real-COMSOL suite |
| Windows | 6.4.0.429 | [docs/platform-setup.md](docs/platform-setup.md) |
| macOS (Apple Silicon) | 6.4.0.378 | [docs/macos-setup.md](docs/macos-setup.md) |

Other 6.x releases are untested; the Java API surface and the
property-key tables the linter relies on change between releases, so
do not assume the catalogs transfer. Never plan around a release newer
than 6.4 — if something genuinely requires one, raise it with the
project owners rather than working around it.

## Where the install is found

`comsol_support.COMSOL_PATH` resolves, in order: the `COMSOL_PATH`
environment variable (the directory *containing* `bin/`, `plugins/`,
`java/`), then the first existing conventional root for the platform:

| Platform | Probed roots (in order) |
|---|---|
| Linux | `/usr/local/comsol64/multiphysics/`, `/opt/comsol64/multiphysics/` |
| Windows | `C:\Program Files\COMSOL\COMSOL64\Multiphysics` |
| macOS | `/Applications/COMSOL64/Multiphysics` |
| any | the install that owns a `comsol` launcher found on `PATH` (symlinks resolved; accepted only if that root carries `plugins/`) — probed after the rows above, before giving up |

`comsol-support doctor` reports which root was chosen and why. Several
COMSOL versions can coexist on one machine; point `COMSOL_PATH` at the
6.4 root explicitly if a different one is found first.

## What comes from the install (read-only)

The knowledge base is built from files shipped inside the COMSOL
install — nothing is written there:

| Resource | Path under `COMSOL_PATH` | Consumer |
|---|---|---|
| Javadoc API tree (~470 HTML pages) | `doc/help/wtpwebapps/ROOT/doc/com.comsol.help.comsol/api/` | `comsol-support scrape javadoc` |
| Programming Reference Manual PDF | `doc/pdf/COMSOL_Multiphysics/COMSOL_ProgrammingReferenceManual.pdf` | `comsol-support scrape refmanual` (needs `pdftotext`) |
| Application-library models (~875 `.mph`) | `applications/` | `comsol-support scrape corpus`, `scrape slots` (need a license seat) |
| Bundled JDK (Java 21) | `java/<plat>/jre/` | every compile/run (preferred over a system JDK) |
| Plugin JARs | `plugins/` | classpath (`plugins/*` wildcard) |

All outputs go to the repository's `data/` (or `$COMSOL_DB`) and
`corpus/` directories, which are git-ignored.

## Cross-references

- Install on each platform: `INSTALL.md`, `docs/platform-setup.md`
