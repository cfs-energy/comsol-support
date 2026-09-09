"""Phase 1 of the slot expected-unit catalog pipeline.

Orchestrates SlotHarvester.java across a corpus of .mph files. Appends
one JSONL record per expression-valued slot (and per variable unit
declaration) to a single dump file. The dump is the input to Phase 2
(aggregation into the SQLite catalog).

Design notes:
- Append-only output. A crash mid-run loses only the in-flight model,
  not prior ones. Re-running will append duplicate records; the Phase 2
  aggregator dedupes on (model_path, physics_tag, feature_tag,
  slot_property) so this is safe.
- Single JVM for the whole corpus via SlotHarvester's batch mode.
- Subprocess timeout is generous (per-model budget * N) but cappable
  via --timeout to avoid runaway.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from comsol_support.java_facade import _terminate_process_group

logger = logging.getLogger(__name__)

HARVESTER_SOURCE = "SlotHarvester.java"
HARVESTER_CLASS = "SlotHarvester"


class SlotHarvestError(Exception):
    """Slot harvesting failed."""


@dataclass
class HarvestReport:
    """Summary of a harvest run."""
    mph_count: int = 0
    models_processed: int = 0
    slots_emitted: int = 0
    var_units_emitted: int = 0
    errors: int = 0
    error_lines: list[str] = field(default_factory=list)
    dump_path: Path | None = None


def collect_mph_paths(
    applications_dir: Path,
    *,
    max_models: int | None = None,
) -> list[Path]:
    """Recursively find .mph files under applications_dir.

    Returns sorted paths (stable ordering across runs). Empty list if
    the directory doesn't exist.
    """
    applications_dir = Path(applications_dir)
    if not applications_dir.is_dir():
        return []
    paths = sorted(applications_dir.rglob("*.mph"))
    if max_models is not None:
        paths = paths[:max_models]
    return paths


def compile_harvester(facade, *, force: bool = False) -> Path:
    """Compile SlotHarvester.java if the .class is missing or stale.

    Returns the path to the compiled .class file. Raises SlotHarvestError
    on compile failure.
    """
    source = facade.java_source_dir / HARVESTER_SOURCE
    compiled = facade.compiled_dir / f"{HARVESTER_CLASS}.class"
    facade.compiled_dir.mkdir(parents=True, exist_ok=True)

    if not force and compiled.exists() and (
        source.stat().st_mtime <= compiled.stat().st_mtime
    ):
        return compiled

    javac = facade.find_java_executable("javac")
    cp = facade.get_full_classpath()
    r = subprocess.run(
        [javac, "-cp", cp, "-d", str(facade.compiled_dir), str(source)],
        capture_output=True, text=True, timeout=120,
        encoding="utf-8", errors="replace",
    )
    if r.returncode != 0:
        raise SlotHarvestError(
            f"Failed to compile {HARVESTER_SOURCE}:\n{r.stderr}"
        )
    return compiled


def detect_comsol_version(comsol_path: str | Path) -> str:
    """Read COMSOL version from the install directory.

    Tries, in order:
      1. Plain-text version files (`VERSION`, `version.txt`,
         `.comsol_version`).
      2. Parent-directory name pattern (`comsol64`, `comsol65`, …) —
         a heuristic used across common COMSOL install conventions.

    Returns "unknown" when no reliable source is found. Deliberately
    does NOT fall back to the OSGi bundle version
    (`com.comsol.core_1.0.0.jar`): every COMSOL 6.x release ships that
    same bundle version, so using it as a catalog key silently
    conflates distinct COMSOL releases. Callers with a reliable
    version should pass `version=...` explicitly.
    """
    comsol_path = Path(comsol_path)
    candidates = [
        comsol_path / "VERSION",
        comsol_path / "version.txt",
        comsol_path / ".comsol_version",
    ]
    for p in candidates:
        try:
            if p.is_file():
                text = p.read_text(encoding="utf-8", errors="ignore").strip()
                for line in text.splitlines():
                    line = line.strip()
                    if line:
                        return line
        except OSError:
            continue

    # Install-directory name heuristic. A path like
    # `/.../comsol64/multiphysics` encodes the 6.x major; distinct
    # versions are typically under distinct dirs (`comsol63`, `comsol64`,
    # `comsol65`). Return the numeric token if the directory name
    # matches the `comsolNN` convention.
    for parent in [comsol_path, comsol_path.parent, comsol_path.parent.parent]:
        if parent is None:
            continue
        # Case-insensitive: Windows/macOS installs use `COMSOL64`,
        # Linux uses `comsol64`.
        name = parent.name.lower()
        if name.startswith("comsol") and name[len("comsol"):].isdigit():
            digits = name[len("comsol"):]
            if len(digits) >= 2:
                return f"{digits[0]}.{digits[1:]}"

    logger.warning(
        "Could not detect COMSOL version from %s; falling back to "
        "'unknown'. Pass --version explicitly to avoid catalog key "
        "ambiguity across COMSOL releases.",
        comsol_path,
    )
    return "unknown"


def run_harvest(
    mph_paths: list[Path],
    output_jsonl: Path,
    *,
    comsol_path: str | Path,
    workspace_dir: Path,
    timeout_s: int = 7200,
    progress: bool = True,
    version: str | None = None,
) -> HarvestReport:
    """Run SlotHarvester over a list of .mph paths.

    Appends records to ``output_jsonl``. Returns a summary report.
    """
    from comsol_support.java_facade import JavaFacade

    report = HarvestReport(mph_count=len(mph_paths))
    report.dump_path = Path(output_jsonl)

    if not mph_paths:
        logger.warning("No .mph paths to harvest")
        return report

    facade = JavaFacade(
        comsol_path=str(comsol_path),
        workspace_dir=str(workspace_dir),
        java_source_dir=Path(__file__).parent / "java",
    )

    compile_harvester(facade)

    resolved_version = version or detect_comsol_version(comsol_path)

    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".txt", delete=False, prefix="mphlist_",
    ) as f:
        for p in mph_paths:
            f.write(str(p) + "\n")
        list_path = Path(f.name)

    try:
        java_exe = facade.find_java_executable("java")
        cp = facade.get_full_classpath()
        argv = [
            java_exe, "-Djava.awt.headless=true",
            "-cp", cp, HARVESTER_CLASS,
            "--list", str(list_path),
            "--out", str(output_jsonl),
            "--version", resolved_version,
        ]
        if progress:
            argv.append("--progress")

        logger.info(
            "Running SlotHarvester on %d models → %s", len(mph_paths),
            output_jsonl,
        )
        proc = subprocess.Popen(
            argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace",
            env=facade.get_comsol_env(),
            # Own the process group so a timeout releases the license seat
            # cleanly (see _terminate_process_group).
            start_new_session=(os.name == "posix"),
        )

        # Stream stdout for progress / done; collect stderr for errors.
        try:
            stdout_text, stderr_text = proc.communicate(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            _terminate_process_group(proc)
            proc.communicate()
            raise SlotHarvestError(
                f"SlotHarvester timed out after {timeout_s}s"
            )

        for line in stdout_text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("done"):
                report.models_processed = int(rec.get("models", 0))
                report.slots_emitted = int(rec.get("slots", 0))
                report.var_units_emitted = int(rec.get("var_units", 0))
                report.errors = int(rec.get("errors", 0))
            elif rec.get("progress"):
                if report.mph_count > 0 and rec.get("index", 0) % 25 == 0:
                    logger.info(
                        "  progress: %s/%s  %s",
                        rec.get("index"), rec.get("total"),
                        Path(rec.get("mph", "")).name,
                    )

        if stderr_text:
            for line in stderr_text.splitlines():
                line = line.strip()
                if not line:
                    continue
                report.error_lines.append(line)
                if line.startswith("LOAD_ERROR"):
                    logger.debug("%s", line)
                else:
                    logger.warning("%s", line)

        if proc.returncode != 0 and report.models_processed == 0:
            raise SlotHarvestError(
                f"SlotHarvester exited with code {proc.returncode}. "
                f"stderr tail: {stderr_text[-500:] if stderr_text else ''}"
            )

    finally:
        try:
            list_path.unlink()
        except OSError:
            pass

    return report


def read_dump(dump_path: Path) -> list[dict]:
    """Read a JSONL dump produced by run_harvest. One dict per record.

    Bad lines are silently skipped. Intended for testing and small-dump
    consumers; Phase 2 streams line-by-line instead.
    """
    records: list[dict] = []
    with Path(dump_path).open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return records


def iter_dump(dump_path: Path):
    """Stream JSONL records from a dump. Yields one dict at a time."""
    with Path(dump_path).open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue
