"""doctor - one cross-platform preflight for a comsol-support install.

Every platform's installer (`scripts/install.sh`, `scripts/install.ps1`)
and the `comsol-support doctor` subcommand run *this* code, so the checks
and their remedies never drift between Linux, macOS, and Windows.

Two entry points, deliberately:

* ``comsol-support doctor`` - after `uv sync`, for "why is this not
  working?"
* ``python scripts/doctor.py`` - before anything is installed. This
  module is stdlib-only and imports nothing that needs installing, so
  it runs from a bare checkout with any Python 3.10+.

Never raises: every probe is guarded, because a diagnostic that
crashes is worse than no diagnostic.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import re
import shutil
import sqlite3
import subprocess
import sys
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path

MIN_PYTHON = (3, 10)

OK, WARN, FAIL = "ok", "warn", "FAIL"

#: Application Library "preview stub" ceiling - placeholder .mph files
#: COMSOL ships until the real model is downloaded through the GUI.
PREVIEW_STUB_MAX_BYTES = 100_000


@dataclass
class Check:
    """One diagnostic line. `blocking` means the install cannot work."""
    name: str
    status: str
    detail: str = ""
    fix: str = ""
    blocking: bool = False

    def as_dict(self) -> dict:
        return {"name": self.name, "status": self.status,
                "detail": self.detail, "fix": self.fix,
                "blocking": self.blocking}


@dataclass
class Report:
    checks: list[Check] = field(default_factory=list)

    def add(self, *args, **kwargs) -> Check:
        c = Check(*args, **kwargs)
        self.checks.append(c)
        return c

    @property
    def blocking(self) -> list[Check]:
        return [c for c in self.checks if c.status == FAIL and c.blocking]

    @property
    def warnings(self) -> list[Check]:
        return [c for c in self.checks if c.status == WARN]

    def as_dict(self) -> dict:
        return {
            "platform": platform.platform(),
            "ok": not self.blocking,
            "blocking": len(self.blocking),
            "warnings": len(self.warnings),
            "checks": [c.as_dict() for c in self.checks],
        }


# ---- per-platform remedies -------------------------------------------

def _os_key() -> str:
    return {"Linux": "linux", "Darwin": "macos",
            "Windows": "windows"}.get(platform.system(), "linux")


_COMSOL_HINT = {
    "linux": "Install COMSOL 6.4, or point at it: "
             "export COMSOL_PATH=/path/to/comsol64/multiphysics",
    "macos": "Install COMSOL 6.4, or point at it: "
             "export COMSOL_PATH=/Applications/COMSOL64/Multiphysics",
    "windows": "Install COMSOL 6.4, or point at it: $env:COMSOL_PATH = "
               "'C:\\Program Files\\COMSOL\\COMSOL64\\Multiphysics'",
}

_PDFTOTEXT_HINT = {
    "linux": "apt install poppler-utils (only needed for "
             "`comsol-support scrape refmanual`)",
    "macos": "brew install poppler (only needed for "
             "`comsol-support scrape refmanual`)",
    "windows": "winget install oschwartz10612.Poppler, then open a NEW "
               "terminal (only needed for `scrape refmanual`)",
}


# ---- checks ----------------------------------------------------------

def _check_source_checkout(report: Report) -> None:
    """The repo assets the package needs at run time must be present."""
    from comsol_support._source_checkout import (
        INSTALL_HINT, SOURCE_ROOT, missing_source_assets,
    )
    missing = missing_source_assets()
    if missing:
        report.add("Source checkout", FAIL,
                   f"missing under {SOURCE_ROOT}: {', '.join(missing)}",
                   fix=INSTALL_HINT, blocking=True)
    else:
        report.add("Source checkout", OK, str(SOURCE_ROOT))


def _check_python(report: Report) -> None:
    v = sys.version_info
    got = f"{v.major}.{v.minor}.{v.micro}"
    if (v.major, v.minor) >= MIN_PYTHON:
        report.add("Python", OK, got)
    else:
        report.add("Python", FAIL,
                   f"{got} (need >= {MIN_PYTHON[0]}.{MIN_PYTHON[1]})",
                   fix="uv provides a suitable Python: uv sync",
                   blocking=True)


def comsol_version(root: Path) -> str:
    """Read the COMSOL version without launching it.

    Deliberately static: on Windows `comsol.exe --version` opens the
    GUI and orphans a child process (docs/platform-setup.md).
    """
    for name in ("about.txt", "VERSION", "version.txt"):
        p = root / name
        try:
            if not p.is_file():
                continue
            head = p.read_text(encoding="utf-8", errors="replace")[:2000]
        except OSError:
            continue
        m = re.search(r"COMSOL\D{0,20}(\d+\.\d+(?:\.\d+){0,2})", head)
        if m:
            return m.group(1)
        for line in head.strip().splitlines():
            if line.strip():
                return line.strip()[:40]
    return "unknown"


def _check_comsol(report: Report, comsol_path: str) -> Path | None:
    root = Path(comsol_path)
    if os.environ.get("COMSOL_PATH"):
        source = "COMSOL_PATH"
    else:
        from comsol_support import _DEFAULT_CANDIDATES
        known = {c.rstrip("/\\") for cs in _DEFAULT_CANDIDATES.values() for c in cs}
        source = ("default probe" if comsol_path.rstrip("/\\") in known
                  else "comsol launcher on PATH")
    if not root.is_dir():
        report.add("COMSOL 6.4", FAIL, f"not found at {root} ({source})",
                   fix=_COMSOL_HINT[_os_key()], blocking=True)
        return None

    version = comsol_version(root)
    if version != "unknown" and not version.startswith("6.4"):
        report.add("COMSOL", WARN, f"{version} at {root} ({source})",
                   fix="The ontology, gotcha catalog, and slot catalog are "
                       "6.4-specific (COMSOL_VERSIONS.md).")
    else:
        shown = version if version != "unknown" else "version unreadable"
        report.add("COMSOL", OK, f"{shown} at {root} ({source})")
    return root


def _check_platform_libs(report: Report, root: Path) -> None:
    from comsol_support.java_facade import _platform_subdir

    plat = _platform_subdir(root)
    lib_dir = root / "lib" / plat
    if lib_dir.is_dir():
        n_ext = 0
        ext = root / "ext"
        if ext.is_dir():
            try:
                n_ext = sum(1 for sub in ext.iterdir()
                            if (sub / plat).is_dir())
            except OSError:
                pass
        report.add("Native libraries", OK,
                   f"{plat} ({lib_dir}, {n_ext} ext dirs)")
    else:
        report.add("Native libraries", FAIL, f"no lib/{plat} under {root}",
                   fix=f"Incomplete install, or this build uses a platform "
                       f"directory this release does not know - report the "
                       f"contents of {root / 'lib'}.",
                   blocking=True)


def _check_jdk(report: Report, root: Path) -> None:
    from comsol_support._source_checkout import SourceCheckoutRequired
    from comsol_support.java_facade import JavaFacade, JavaNotFoundError

    try:
        facade = JavaFacade(comsol_path=str(root), workspace_dir=Path.cwd())
    except SourceCheckoutRequired:
        # Already reported as a blocking "Source checkout" problem; keep
        # rendering the rest of the report instead of aborting.
        report.add("Java (JDK)", WARN, "not checked (not a source checkout)")
        return
    found: dict[str, str | None] = {}
    for tool in ("java", "javac"):
        try:
            found[tool] = facade.find_java_executable(tool)
        except JavaNotFoundError:
            found[tool] = None

    if found["java"] and found["javac"]:
        javac = found["javac"] or ""
        if str(root) in javac:
            report.add("Java (JDK)", OK, f"COMSOL-bundled: {javac}")
        else:
            report.add("Java (JDK)", WARN, f"system JDK: {javac}",
                       fix="COMSOL 6.4's JARs are built for Java 21. No "
                           "bundled JDK was found, so a system JDK is in "
                           "use - if JVM launches fail with "
                           "UnsupportedClassVersionError, that is why.")
    else:
        missing = ", ".join(t for t, v in found.items() if not v)
        report.add("Java (JDK)", FAIL, f"missing: {missing}",
                   fix="COMSOL ships its own JDK, so a missing one means an "
                       "incomplete install. Otherwise install JDK 21 and "
                       "set JAVA_HOME.",
                   blocking=True)


def _check_jars(report: Report, root: Path) -> None:
    plugins = root / "plugins"
    try:
        jars = [p for p in plugins.iterdir() if p.suffix == ".jar"]
    except OSError:
        jars = []
    if jars:
        report.add("COMSOL plugin JARs", OK, f"{len(jars)} in {plugins}")
    else:
        report.add("COMSOL plugin JARs", FAIL, f"none under {plugins}",
                   fix="Without the plugin JARs nothing can compile or run "
                       "against the COMSOL API.",
                   blocking=True)


def _check_doc_assets(report: Report, root: Path) -> None:
    api = (root / "doc" / "help" / "wtpwebapps" / "ROOT" / "doc"
           / "com.comsol.help.comsol" / "api")
    if api.is_dir():
        try:
            n = sum(1 for _ in api.rglob("*.html"))
        except OSError:
            n = 0
        report.add("Javadoc tree", OK, f"{n} pages")
    else:
        report.add("Javadoc tree", WARN, "not installed",
                   fix="`scrape javadoc` will find nothing - install the "
                       "COMSOL documentation to build the API ontology.")

    pdf = (root / "doc" / "pdf" / "COMSOL_Multiphysics"
           / "COMSOL_ProgrammingReferenceManual.pdf")
    if pdf.is_file():
        try:
            mb = pdf.stat().st_size // (1024 * 1024)
        except OSError:
            mb = 0
        report.add("Reference Manual PDF", OK, f"{mb} MB")
    else:
        report.add("Reference Manual PDF", WARN, "not installed",
                   fix="`scrape refmanual` will find nothing.")

    apps = root / "applications"
    if not apps.is_dir():
        report.add("Application models", WARN, "no applications/ directory",
                   fix="`scrape corpus` and `scrape slots` need the "
                       "Application Libraries.")
        return
    try:
        mphs = list(apps.rglob("*.mph"))
    except OSError:
        mphs = []
    stubs = 0
    for p in mphs:
        try:
            if p.stat().st_size <= PREVIEW_STUB_MAX_BYTES:
                stubs += 1
        except OSError:
            pass
    detail = f"{len(mphs)} .mph"
    fix = ""
    if stubs:
        detail += f" ({stubs} preview stubs)"
        fix = ("Preview stubs cannot be opened until the full model is "
               "downloaded in the COMSOL GUI (Application Libraries "
               "window); `scrape corpus`/`scrape slots` skip them.")
    report.add("Application models", OK if mphs else WARN, detail, fix=fix)


def _tool_version(exe: str, *args: str) -> str:
    try:
        r = subprocess.run([exe, *args], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=15)
        out = (r.stdout or r.stderr or "").strip().splitlines()
        return out[0][:60] if out else "present"
    except Exception:  # noqa: BLE001 - a version probe must never break doctor
        return "present"


def _check_optional_tools(report: Report) -> None:
    uv = shutil.which("uv")
    if uv:
        report.add("uv", OK, _tool_version(uv, "--version"))
    else:
        report.add("uv", WARN, "not on PATH",
                   fix="https://docs.astral.sh/uv/ - needed for the "
                       "documented install flow (pip works too).")

    pdftotext = shutil.which("pdftotext")
    if pdftotext:
        report.add("pdftotext (Poppler)", OK, pdftotext)
    else:
        report.add("pdftotext (Poppler)", WARN, "not on PATH",
                   fix=_PDFTOTEXT_HINT[_os_key()])



_DB_TABLES = ("knowledge", "fragments", "slot_expected_units",
              "native_interfaces")


def _check_knowledge_base(report: Report, db_path: Path) -> None:
    """Report DB contents. Read-only: never creates or migrates."""
    if not db_path.is_file():
        report.add("Knowledge base", WARN, f"{db_path} not built yet",
                   fix="uv run comsol-support scrape javadoc (then refmanual "
                       "/ corpus), or run the install script.")
        return

    counts: dict[str, int] = {}
    try:
        # Read-only URI. The path is percent-encoded because a '?' or
        # '#' anywhere in it (or in a user's home directory name) would
        # otherwise be parsed as URI syntax.
        uri = "file:" + urllib.parse.quote(db_path.as_posix()) + "?mode=ro"
        conn = sqlite3.connect(uri, uri=True)
    except sqlite3.Error as e:
        report.add("Knowledge base", WARN, f"{db_path} unreadable: {e}")
        return
    try:
        for table in _DB_TABLES:
            try:
                counts[table] = conn.execute(
                    f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            except sqlite3.Error:
                counts[table] = 0
    finally:
        conn.close()

    detail = ", ".join(f"{k} {v}" for k, v in counts.items())
    if not counts.get("knowledge"):
        report.add("Knowledge base", WARN,
                   f"{detail} - ontology not ingested",
                   fix="uv run comsol-support scrape javadoc")
        return

    hints = []
    if not counts.get("fragments"):
        hints.append("fragments empty - run `scrape corpus`")
    if not counts.get("slot_expected_units"):
        hints.append("slot_expected_units empty - Layer C stays "
                     "descriptive-only until `scrape slots` runs")
    report.add("Knowledge base", WARN if hints else OK, detail,
               fix="; ".join(hints))


# ---- driver ----------------------------------------------------------

def run_checks(comsol_path: str | None = None,
               db_path: str | Path | None = None) -> Report:
    """Run every check and return the report. Never raises."""
    report = Report()
    _check_python(report)
    _check_source_checkout(report)

    if comsol_path is None:
        from comsol_support import COMSOL_PATH
        comsol_path = COMSOL_PATH
    root = _check_comsol(report, comsol_path)
    if root is not None:
        _check_platform_libs(report, root)
        _check_jdk(report, root)
        _check_jars(report, root)
        _check_doc_assets(report, root)
    _check_optional_tools(report)

    if db_path is None:
        from comsol_support.config import default_db_path
        db_path = default_db_path()
    _check_knowledge_base(report, Path(db_path))
    return report


_COLOR = {OK: "\033[32m", WARN: "\033[33m", FAIL: "\033[31m"}


def render(report: Report, *, color: bool = True) -> str:
    lines = [f"comsol-support doctor - {platform.platform()}", ""]
    for c in report.checks:
        tag = (f"{_COLOR[c.status]}{c.status}\033[0m"
               if color and c.status in _COLOR else c.status)
        pad = " " * max(0, 4 - len(c.status))
        lines.append(f"  {tag}{pad}  {c.name}: {c.detail}")
        if c.fix and c.status != OK:
            lines.append(f"          -> {c.fix}")
    lines.append("")
    if report.blocking:
        lines.append(f"{len(report.blocking)} blocking problem(s); "
                     f"{len(report.warnings)} warning(s).")
    else:
        lines.append(f"Ready. {len(report.warnings)} warning(s) - optional "
                     "features only.")
    return "\n".join(lines)


def add_doctor_subparser(subs: argparse._SubParsersAction) -> None:
    p = subs.add_parser(
        "doctor",
        help="Check that this machine can run comsol-support (source "
             "checkout, COMSOL, JDK, JARs, docs, knowledge base)",
    )
    p.add_argument("--comsol-path",
                   help="COMSOL installation root (default: discovered)")
    p.add_argument("--db", help="Knowledge-base path to inspect")
    p.add_argument("--json", action="store_true",
                   help="Emit the report as JSON")
    p.add_argument("--strict", action="store_true",
                   help="Exit non-zero on warnings as well as failures")
    p.set_defaults(func=cmd_doctor)


def cmd_doctor(args: argparse.Namespace) -> int:
    report = run_checks(
        comsol_path=getattr(args, "comsol_path", None),
        db_path=getattr(args, "db", None),
    )
    if getattr(args, "json", False):
        print(json.dumps(report.as_dict(), indent=2))
    else:
        color = sys.stdout.isatty() and os.environ.get("NO_COLOR") is None
        print(render(report, color=color))
    if report.blocking:
        return 1
    if getattr(args, "strict", False) and report.warnings:
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="comsol-support doctor",
        description="Preflight/diagnostics for a comsol-support install",
    )
    parser.add_argument("--comsol-path")
    parser.add_argument("--db")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--strict", action="store_true")
    return cmd_doctor(parser.parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
