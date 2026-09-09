"""Corpus mining pipeline for COMSOL application models.

Phases:
  A — Environment verification (COMSOL license, JARs, .mph format)
  B — Batch .mph → .java conversion via CorpusBatchConverter
  C — Java source analysis (regex-based feature/property extraction)
  D — Fragment ingestion into the fragments table

The parser operates on generated COMSOL Java source code, which follows
a deterministic template: sequential .create(), .set(), .run() calls
organized by model-tree order.
"""

import json
import logging
import os
import re
import shutil
import sqlite3
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from comsol_support.java_facade import _terminate_process_group, find_comsol_launcher
from comsol_support.db import (
    clear_fragments_by_source,
    store_fragment,
)
from comsol_support.refmanual_scraper import classify_feature_stage

logger = logging.getLogger("comsol_support.corpus_miner")


# ── Data classes ────────────────────────────────────────────────────────────


@dataclass
class EnvironmentReport:
    """Phase A results."""
    comsol_version: str | None = None
    license_ok: bool = False
    jars_found: int = 0
    jar_paths: list[str] = field(default_factory=list)
    mph_count: int = 0
    mph_format: str = ""          # "zip", "binary", or "unknown"
    java_files_found: int = 0     # Pre-existing .java companion files
    disk_free_gb: float = 0.0
    module_distribution: dict[str, int] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)

    @property
    def can_convert(self) -> bool:
        """True if batch conversion is feasible."""
        return self.jars_found > 0 and self.mph_count > 0


@dataclass
class ModelAnalysis:
    """Phase C output per .java file."""
    source_mph: str = ""
    java_path: str = ""
    features: dict[str, int] = field(default_factory=dict)
    properties: dict[str, list[str]] = field(default_factory=dict)
    physics_types: list[str] = field(default_factory=list)
    study_types: list[str] = field(default_factory=list)
    mesh_types: list[str] = field(default_factory=list)
    # Result/plot/probe feature class-names created under the result()
    # subtree AND the probe() subtree. Example: ["Surface", "Volume",
    # "DomainProbe"]. Populated by parse_java_file() from the
    # postprocessing stage block.
    result_types: list[str] = field(default_factory=list)
    stage_blocks: dict[str, str] = field(default_factory=dict)
    module_category: str = ""


@dataclass
class CorpusStatistics:
    """Phase C aggregate output."""
    total_models: int = 0
    parsed: int = 0
    failed: int = 0
    feature_freq: dict[str, int] = field(default_factory=dict)
    property_freq: dict[str, int] = field(default_factory=dict)
    physics_freq: dict[str, int] = field(default_factory=dict)
    study_freq: dict[str, int] = field(default_factory=dict)
    # Per-class frequency of result/plot/probe features observed in the
    # postprocessing stage across the corpus. Mirrors physics_freq /
    # study_freq; joined against native_interfaces on class_name.
    result_freq: dict[str, int] = field(default_factory=dict)
    co_occurrence: dict[str, dict[str, int]] = field(default_factory=dict)
    stage_exemplars: dict[str, list[tuple[str, str]]] = field(default_factory=dict)
    module_distribution: dict[str, int] = field(default_factory=dict)


@dataclass
class ConversionReport:
    """Phase B results."""
    converted: int = 0
    failed: int = 0
    total: int = 0
    errors: list[str] = field(default_factory=list)
    output_dir: str = ""


@dataclass
class IngestionReport:
    """Phase D results."""
    fragments_inserted: int = 0
    fragments_cleared: int = 0
    source_tag: str = ""
    stage_distribution: dict[str, int] = field(default_factory=dict)


# ── Regex patterns for COMSOL-generated Java ────────────────────────────────

# Feature creation: .create("tag", "FeatureType")
RE_CREATE = re.compile(r'\.create\("(\w+)",\s*"(\w+)"\)')

# Property set: .set("key", value)
RE_SET = re.compile(r'\.set\("(\w+)",\s*(.+?)\)\s*;')

# Property setIndex: .setIndex("key", value, index)
RE_SET_INDEX = re.compile(r'\.setIndex\("(\w+)",\s*(.+?),\s*(\d+)\)\s*;')

# Physics interface creation
RE_PHYSICS = re.compile(r'\.physics\(\)\.create\("(\w+)",\s*"(\w+)"')

# Material creation
RE_MATERIAL = re.compile(r'\.material\(\)\.create\("(\w+)"')

# Study type creation
RE_STUDY = re.compile(
    r'\.study\("(\w+)"\)\.create\("(\w+)",\s*"(\w+)"\)'
)

# Mesh creation
RE_MESH_CREATE = re.compile(r'\.mesh\("(\w+)"\)\.create\("(\w+)",\s*"(\w+)"\)')

# Selection creation
RE_SELECTION = re.compile(r'\.selection\(\)\.create\("(\w+)"')

# Geometry run (stage boundary marker)
RE_GEOM_RUN = re.compile(r'\.geom\("(\w+)"\)\.run\(')

# Stage detection patterns (which API subtree is being called)
_STAGE_PATTERNS = [
    (re.compile(r'\.geom\("'), "geometry"),
    (re.compile(r'\.selection\(\)'), "selections"),
    (re.compile(r'\.material\('), "materials"),
    (re.compile(r'\.physics\('), "physics"),
    (re.compile(r'\.mesh\('), "mesh"),
    (re.compile(r'\.study\('), "studies"),
    (re.compile(r'\.result\('), "postprocessing"),
    (re.compile(r'\.param\(\)'), "parameters"),
    (re.compile(r'\.func\(\)'), "functions"),
]


# ── Phase A: Environment Verification ──────────────────────────────────────


def verify_environment(
    comsol_path: Path,
    applications_dir: Path | None = None,
    output_dir: Path | None = None,
) -> EnvironmentReport:
    """Check COMSOL license, JARs, .mph format, disk space.

    This method can be run standalone to determine feasibility
    before committing to the full batch conversion pipeline.

    Args:
        comsol_path: Root of COMSOL installation.
        applications_dir: Where .mph files live (default: comsol_path/applications).
        output_dir: Where .java files will be written. Disk space is checked
            here if provided, otherwise at comsol_path.
    """
    report = EnvironmentReport()

    # 1. Check COMSOL JARs (all plugin JARs — COMSOL's OSGi bundles
    #    have deep cross-dependencies; the public API is in
    #    com.comsol.api_1.0.0.jar, not com.comsol.model*.jar)
    plugins_dir = comsol_path / "plugins"
    if plugins_dir.is_dir():
        for jar in sorted(plugins_dir.iterdir()):
            if jar.suffix == ".jar":
                report.jar_paths.append(str(jar))
        report.jars_found = len(report.jar_paths)
    else:
        report.errors.append(f"Plugins directory not found: {plugins_dir}")

    # 2. Try COMSOL version check
    comsol_bin = find_comsol_launcher(comsol_path)
    if comsol_bin is not None and os.name == "nt":
        # On Windows `comsol.exe --version` opens the GUI (and orphans
        # a ComsolUI child past any subprocess timeout) — read the
        # version statically from about.txt instead. License validity
        # is probed separately by `comsol-support license-status`.
        about = comsol_path / "about.txt"
        try:
            head = about.read_text(encoding="utf-8",
                                   errors="replace")[:2000]
            m = re.search(r"COMSOL\D*(\d+\.\d+)", head)
            if m:
                report.comsol_version = m.group(1)
            else:
                report.errors.append(
                    f"Could not parse COMSOL version from {about}")
        except OSError as e:
            report.errors.append(f"Could not read {about}: {e}")
    elif comsol_bin is not None:
        try:
            result = subprocess.run(
                [str(comsol_bin), "--version"],
                capture_output=True, text=True, timeout=30,
                encoding="utf-8", errors="replace",
            )
            if result.returncode == 0:
                report.comsol_version = result.stdout.strip()
                report.license_ok = True
            else:
                report.errors.append(
                    f"COMSOL version check failed: {result.stderr[:200]}"
                )
        except (subprocess.TimeoutExpired, OSError) as e:
            report.errors.append(f"COMSOL binary error: {e}")
    else:
        report.errors.append(
            f"COMSOL launcher binary not found under {comsol_path / 'bin'}"
        )

    # 3. Count .mph files and check format
    if applications_dir is None:
        applications_dir = comsol_path / "applications"

    if applications_dir.is_dir():
        mph_files = list(applications_dir.rglob("*.mph"))
        report.mph_count = len(mph_files)

        # Check format of first .mph file
        if mph_files:
            _check_mph_format(mph_files[0], report)

        # Module distribution
        for mph in mph_files:
            # Use parent directory as module category
            rel = mph.parent
            try:
                rel = mph.relative_to(applications_dir)
                category = str(rel).split(os.sep)[0] if os.sep in str(rel) else str(rel)
            except ValueError:
                category = mph.parent.name
            report.module_distribution[category] = (
                report.module_distribution.get(category, 0) + 1
            )
    else:
        report.errors.append(f"Applications directory not found: {applications_dir}")

    # 4. Check for pre-existing .java files
    doc_dir = comsol_path / "doc"
    if doc_dir.is_dir():
        report.java_files_found = sum(
            1 for _ in doc_dir.rglob("*.java")
        )

    # 5. Disk space — check at output_dir (where .java files go), not
    # comsol_path. shutil.disk_usage is cross-platform (os.statvfs does
    # not exist on Windows).
    disk_check_path = output_dir if output_dir else comsol_path
    try:
        report.disk_free_gb = shutil.disk_usage(
            str(disk_check_path)).free / (1024 ** 3)
    except OSError:
        pass

    return report


def _check_mph_format(mph_path: Path, report: EnvironmentReport) -> None:
    """Test whether an .mph file is a ZIP archive."""
    try:
        with open(mph_path, "rb") as f:
            magic = f.read(4)
        if magic[:2] == b"PK":
            report.mph_format = "zip"
        else:
            report.mph_format = "binary"
    except OSError as e:
        report.mph_format = "unknown"
        report.errors.append(f"Cannot read .mph file: {e}")


# ── Phase B: Batch Conversion ──────────────────────────────────────────────


def generate_mph_list(
    applications_dir: Path,
    output_path: Path,
) -> int:
    """Scan applications_dir for .mph files, write one path per line.

    Returns count of .mph files found.
    """
    mph_files = sorted(applications_dir.rglob("*.mph"))
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        for mph in mph_files:
            f.write(f"{mph}\n")
    return len(mph_files)


def run_batch_conversion(
    java_facade,
    mph_list_path: Path,
    output_dir: Path,
    *,
    resume: bool = True,
) -> ConversionReport:
    """Invoke CorpusBatchConverter via JavaFacade.

    Args:
        java_facade: JavaFacade instance with COMSOL classpath.
        mph_list_path: File containing one .mph path per line.
        output_dir: Where to write .java files.
        resume: If True, skip already-converted files.

    Returns:
        ConversionReport with conversion statistics.
    """
    report = ConversionReport(output_dir=str(output_dir))
    output_dir.mkdir(parents=True, exist_ok=True)

    # Nothing else compiles CorpusBatchConverter, so on a fresh clone the
    # JVM below would die with ClassNotFoundException — which this function
    # reads as "0 converted" because it only parses stdout.
    compile_result = java_facade.compile_comsol_class("CorpusBatchConverter.java")
    if not compile_result.success:
        report.errors.append(
            "Failed to compile CorpusBatchConverter.java: "
            + (compile_result.stderr or "").strip()[:500]
        )
        return report

    java = java_facade.find_java_executable("java")
    cp = java_facade.get_full_classpath()

    cmd = [
        java, "-Djava.awt.headless=true",
        "-cp", cp,
        "CorpusBatchConverter",
        str(mph_list_path),
        str(output_dir),
    ]
    if resume:
        cmd.append("--resume")

    # Launch in its own process group so a timeout releases the (long-held,
    # up-to-2-hour) license seat cleanly via SIGTERM-then-SIGKILL on the
    # whole group, rather than orphaning the JVM as a ghost checkout.
    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        encoding="utf-8", errors="replace",
        env=java_facade.get_comsol_env(),
        start_new_session=(os.name == "posix"),
    )
    try:
        stdout_text, _stderr_text = proc.communicate(timeout=7200)
    except subprocess.TimeoutExpired:
        _terminate_process_group(proc)
        proc.communicate()
        report.errors.append("Batch conversion timed out after 2 hours")
        return report

    class _R:  # minimal shim so the existing parse code is untouched
        pass
    result = _R()
    result.stdout = stdout_text or ""
    result.returncode = proc.returncode

    # Parse JSON progress lines from stdout
    for line in result.stdout.strip().split("\n"):
        if not line.strip():
            continue
        try:
            msg = json.loads(line)
            if msg.get("done"):
                report.converted = msg.get("converted", 0)
                report.failed = msg.get("failed", 0)
                report.total = msg.get("total", 0)
            elif not msg.get("success", True):
                report.errors.append(msg.get("error", "unknown error"))
        except json.JSONDecodeError:
            pass  # Non-JSON output (e.g., COMSOL init messages)

    if result.returncode != 0 and not report.converted:
        report.errors.append(f"Process exited with code {result.returncode}")

    return report


# ── Phase C: Java Source Analysis ──────────────────────────────────────────


def parse_java_file(java_path: Path, source_mph: str = "") -> ModelAnalysis:
    """Parse a single COMSOL-generated .java file.

    Extracts features, properties, physics/study/mesh types,
    and stage-level code blocks from the generated Java source.
    """
    analysis = ModelAnalysis(
        source_mph=source_mph,
        java_path=str(java_path),
    )

    try:
        text = java_path.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        logger.warning("Cannot read %s: %s", java_path, e)
        return analysis

    lines = text.split("\n")

    # Track current stage for block extraction
    current_stage = None
    stage_lines: dict[str, list[str]] = {}

    for line in lines:
        # Skip comments and blank lines
        stripped = line.strip()
        if not stripped or stripped.startswith("//") or stripped.startswith("*"):
            continue

        # Detect stage transitions
        for pattern, stage_name in _STAGE_PATTERNS:
            if pattern.search(line):
                if stage_name != current_stage:
                    current_stage = stage_name
                    if stage_name not in stage_lines:
                        stage_lines[stage_name] = []
                break

        if current_stage:
            stage_lines.setdefault(current_stage, []).append(line)

        # Extract features (.create calls)
        for match in RE_CREATE.finditer(line):
            _, feature_type = match.groups()
            analysis.features[feature_type] = (
                analysis.features.get(feature_type, 0) + 1
            )

        # Extract properties (.set calls)
        for match in RE_SET.finditer(line):
            prop_key, value = match.groups()
            if prop_key not in analysis.properties:
                analysis.properties[prop_key] = []
            # Store up to 5 unique values per property
            if value not in analysis.properties[prop_key] and len(analysis.properties[prop_key]) < 5:
                analysis.properties[prop_key].append(value)

        # Extract .setIndex calls (same as .set for our purposes)
        for match in RE_SET_INDEX.finditer(line):
            prop_key = match.group(1)
            value = match.group(2)
            if prop_key not in analysis.properties:
                analysis.properties[prop_key] = []
            if value not in analysis.properties[prop_key] and len(analysis.properties[prop_key]) < 5:
                analysis.properties[prop_key].append(value)

        # Extract physics types
        for match in RE_PHYSICS.finditer(line):
            physics_type = match.group(2)
            if physics_type not in analysis.physics_types:
                analysis.physics_types.append(physics_type)

        # Extract study types
        for match in RE_STUDY.finditer(line):
            study_type = match.group(3)
            if study_type not in analysis.study_types:
                analysis.study_types.append(study_type)

        # Extract mesh types
        for match in RE_MESH_CREATE.finditer(line):
            mesh_type = match.group(3)
            if mesh_type not in analysis.mesh_types:
                analysis.mesh_types.append(mesh_type)

    # Build stage blocks from accumulated lines
    for stage_name, slines in stage_lines.items():
        analysis.stage_blocks[stage_name] = "\n".join(slines)

    # Extract result/plot/probe class names from the postprocessing block.
    # We scope to the postprocessing stage block (not the full file) so
    # class names like "Surface" — which are also valid feature types in
    # other stages — are not double-counted. Probes live under
    # model.probe(); in the existing stage-pattern set that also routes
    # into the postprocessing block since result/probe are adjacent
    # post-solve concerns for our catalog purposes.
    pp_block = analysis.stage_blocks.get("postprocessing", "")
    if pp_block:
        for match in RE_CREATE.finditer(pp_block):
            _, class_name = match.groups()
            if class_name and class_name not in analysis.result_types:
                analysis.result_types.append(class_name)

    return analysis


def analyze_corpus(java_dir: Path) -> CorpusStatistics:
    """Aggregate analysis across all .java files in a directory tree.

    Returns corpus-wide feature frequencies, co-occurrence matrix,
    and per-stage exemplar code blocks (attached to fragments on ingest).
    """
    stats = CorpusStatistics()

    java_files = sorted(java_dir.rglob("*.java"))
    stats.total_models = len(java_files)

    for java_path in java_files:
        try:
            analysis = parse_java_file(java_path)
        except Exception as e:
            logger.warning("Failed to parse %s: %s", java_path, e)
            stats.failed += 1
            continue

        stats.parsed += 1

        # Module distribution from directory structure
        try:
            rel = java_path.relative_to(java_dir)
            category = rel.parts[0] if len(rel.parts) > 1 else "root"
        except ValueError:
            category = java_path.parent.name
        stats.module_distribution[category] = (
            stats.module_distribution.get(category, 0) + 1
        )

        # Aggregate feature frequencies
        for feat, count in analysis.features.items():
            stats.feature_freq[feat] = stats.feature_freq.get(feat, 0) + 1

        # Aggregate property frequencies
        for prop in analysis.properties:
            stats.property_freq[prop] = stats.property_freq.get(prop, 0) + 1

        # Aggregate physics frequencies
        for ptype in analysis.physics_types:
            stats.physics_freq[ptype] = stats.physics_freq.get(ptype, 0) + 1

        # Aggregate study frequencies
        for stype in analysis.study_types:
            stats.study_freq[stype] = stats.study_freq.get(stype, 0) + 1

        # Aggregate result/plot/probe frequencies (class-name granularity)
        for rtype in analysis.result_types:
            stats.result_freq[rtype] = stats.result_freq.get(rtype, 0) + 1

        # Co-occurrence: for each pair of features in this model
        feature_list = list(analysis.features.keys())
        for i, feat_a in enumerate(feature_list):
            if feat_a not in stats.co_occurrence:
                stats.co_occurrence[feat_a] = {}
            for feat_b in feature_list[i + 1:]:
                stats.co_occurrence[feat_a][feat_b] = (
                    stats.co_occurrence[feat_a].get(feat_b, 0) + 1
                )
                # Symmetric
                if feat_b not in stats.co_occurrence:
                    stats.co_occurrence[feat_b] = {}
                stats.co_occurrence[feat_b][feat_a] = (
                    stats.co_occurrence[feat_b].get(feat_a, 0) + 1
                )

        # Collect stage exemplars (shortest 10 per stage). They supply the
        # java_code attached to each corpus fragment in ingest_fragments().
        for stage_name, code_block in analysis.stage_blocks.items():
            if stage_name not in stats.stage_exemplars:
                stats.stage_exemplars[stage_name] = []
            exemplar_list = stats.stage_exemplars[stage_name]
            model_name = java_path.stem
            exemplar_list.append((model_name, code_block))
            # Keep only the 10 shortest code blocks (most focused exemplars)
            if len(exemplar_list) > 10:
                exemplar_list.sort(key=lambda x: len(x[1]))
                stats.stage_exemplars[stage_name] = exemplar_list[:10]

    return stats


# ── Phase D: Fragment Ingestion ────────────────────────────────────────────


def ingest_fragments(
    conn: sqlite3.Connection,
    stats: CorpusStatistics,
    *,
    source_tag: str = "corpus-6.4",
    min_freq: int = 3,
) -> IngestionReport:
    """Store top-frequency patterns as Tier B fragments.

    Only features appearing in min_freq or more models get fragments.
    Clears existing corpus fragments (idempotent re-mine).
    """
    report = IngestionReport(source_tag=source_tag)

    # Clear previous corpus fragments
    report.fragments_cleared = clear_fragments_by_source(conn, source_tag)

    for feature_type, freq in sorted(
        stats.feature_freq.items(), key=lambda x: -x[1]
    ):
        if freq < min_freq:
            continue

        # Classify stage
        stage = classify_feature_stage(feature_type) or "untagged"

        # Build co-occurrence JSON (top 10)
        co_occ = stats.co_occurrence.get(feature_type, {})
        top_co = dict(sorted(co_occ.items(), key=lambda x: -x[1])[:10])
        co_json = json.dumps(top_co) if top_co else None

        # Find best exemplar code block for this feature
        java_code = _find_exemplar_code(feature_type, stats)

        description = (
            f"Corpus-mined pattern: {feature_type} "
            f"(found in {freq}/{stats.parsed} models)"
        )

        store_fragment(
            conn,
            stage=stage,
            pattern_name=feature_type,
            java_code=java_code,
            tier="B",
            description=description,
            corpus_freq=freq,
            co_occurrence_json=co_json,
            source=source_tag,
        )

        report.fragments_inserted += 1
        report.stage_distribution[stage] = (
            report.stage_distribution.get(stage, 0) + 1
        )

    return report


def _find_exemplar_code(
    feature_type: str,
    stats: CorpusStatistics,
) -> str:
    """Find the best code exemplar for a feature type.

    Searches stage exemplars for code blocks containing the feature.
    Returns the shortest matching block, or a placeholder.
    """
    candidates = []

    for _stage, exemplars in stats.stage_exemplars.items():
        for model_name, code_block in exemplars:
            if feature_type in code_block:
                candidates.append(code_block)

    if candidates:
        # Return shortest matching block (most focused)
        candidates.sort(key=len)
        return candidates[0]

    return f"// No corpus exemplar found for {feature_type}"


# ── End-to-end pipeline ────────────────────────────────────────────────────


def mine_corpus(
    comsol_path: Path,
    applications_dir: Path,
    output_dir: Path,
    conn: sqlite3.Connection,
    *,
    source_tag: str = "corpus-6.4",
    skip_conversion: bool = False,
    java_facade=None,
) -> dict:
    """Full corpus mining pipeline: verify → convert → analyze → ingest.

    Args:
        comsol_path: Root of COMSOL installation.
        applications_dir: Path to .mph application library.
        output_dir: Where to write .java files and reports.
        conn: Database connection.
        source_tag: Source tag for fragment provenance.
        skip_conversion: If True, skip Phase B (use existing .java files).
        java_facade: JavaFacade instance (required if not skipping conversion).

    Returns:
        dict with env_report, conversion_report, corpus_stats, ingestion_report.
    """
    results = {}

    # Phase A: Environment verification
    env = verify_environment(comsol_path, applications_dir, output_dir=output_dir)
    results["env_report"] = env
    logger.info(
        "Phase A: %d JARs, %d .mph files, format=%s, license=%s",
        env.jars_found, env.mph_count, env.mph_format,
        "OK" if env.license_ok else "UNKNOWN",
    )

    if not skip_conversion:
        if not env.can_convert:
            logger.error("Cannot convert: missing JARs or .mph files")
            return results

        # Phase B: Batch conversion
        mph_list_path = output_dir / "mph_list.txt"
        generate_mph_list(applications_dir, mph_list_path)

        if java_facade is not None:
            conv = run_batch_conversion(
                java_facade, mph_list_path, output_dir / "java",
            )
            results["conversion_report"] = conv
            logger.info(
                "Phase B: %d converted, %d failed, %d total",
                conv.converted, conv.failed, conv.total,
            )

    # Phase C: Java source analysis
    java_dir = output_dir / "java"
    if java_dir.is_dir():
        stats = analyze_corpus(java_dir)
        results["corpus_stats"] = stats
        logger.info(
            "Phase C: %d models parsed, %d features, %d properties",
            stats.parsed, len(stats.feature_freq), len(stats.property_freq),
        )

        # Phase D: Fragment ingestion
        ing = ingest_fragments(conn, stats, source_tag=source_tag)
        results["ingestion_report"] = ing
        logger.info(
            "Phase D: %d fragments inserted (%d cleared)",
            ing.fragments_inserted, ing.fragments_cleared,
        )

        # Write statistics report
        _write_stats_report(output_dir / "corpus_statistics.json", stats)
    else:
        logger.warning("No java directory found at %s — skipping analysis", java_dir)

    return results


def _write_stats_report(path: Path, stats: CorpusStatistics) -> None:
    """Write corpus statistics to JSON file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "total_models": stats.total_models,
        "parsed": stats.parsed,
        "failed": stats.failed,
        "feature_freq": dict(sorted(stats.feature_freq.items(), key=lambda x: -x[1])),
        "property_freq": dict(sorted(stats.property_freq.items(), key=lambda x: -x[1])[:50]),
        "physics_freq": dict(sorted(stats.physics_freq.items(), key=lambda x: -x[1])),
        "study_freq": dict(sorted(stats.study_freq.items(), key=lambda x: -x[1])),
        "module_distribution": stats.module_distribution,
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


