"""mphgen — generate GUI-openable .mph files from exploration-produced
COMSOL Java builders.

The deterministic floor of Tier 1. Given a .java file that exposes
`public static Model buildModel(Map<String,String> args)`, this module
compiles it, invokes ModelExporter, validates the resulting .mph,
and writes a sidecar JSON provenance record.

No DB writes. No agentic calls. Simple and robust.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import subprocess
import sys
import time
import zipfile
from dataclasses import asdict, dataclass, field
from pathlib import Path

from comsol_support import COMSOL_PATH
from comsol_support.java_facade import (
    JavaExecutionError,
    JavaFacade,
    JavaNotFoundError,
    _terminate_process_group,
    describe_abnormal_exit,
    resolve_license_timeout,
)

logger = logging.getLogger("comsol_support.mphgen")

EXPORTER_SOURCE = "ModelExporter.java"
EXPORTER_CLASS = "ModelExporter"
# Companion utility compiled alongside ModelExporter. Kept as a separate
# top-level .java for clarity; the Java facade has no package boundary.
TELEMETRY_SOURCE = "SolverTelemetry.java"

# Minimum structural pieces a valid COMSOL .mph must contain.
REQUIRED_MPH_MEMBERS = {"dmodel.xml", "model.xml", "fileversion"}


# ---- Exceptions ----

class MphgenError(Exception):
    """Base class for mphgen errors."""


class ContractError(MphgenError):
    """Builder .java does not expose the buildModel contract."""


class BuildFailure(MphgenError):
    """ModelExporter returned a failure envelope."""


class MphValidationError(MphgenError):
    """Generated .mph failed structural validation."""


# ---- Data ----

@dataclass
class MphgenResult:
    success: bool
    builder_java: str
    builder_class: str
    output_mph: str
    output_bytes: int = 0
    output_sha256: str = ""
    builder_args: dict = field(default_factory=dict)
    solved: bool = False
    solve_study: str | None = None
    elapsed_ms: int = 0
    sidecar_path: str = ""
    error: str = ""
    # Telemetry loop — always populated (possibly empty when the Java
    # facade emits no events, e.g. during tests with stubbed subprocesses).
    telemetry_sidecar_path: str = ""
    telemetry_event_count: int = 0
    telemetry_halt_reason: str | None = None
    telemetry_partial_save_path: str | None = None
    telemetry_digest: dict = field(default_factory=dict)


# ---- Contract detection ----

def has_build_model_contract(java_path: Path) -> bool:
    """Check whether a .java file declares the buildModel contract.

    Simple substring check — deliberately lenient on whitespace/generics.
    Accepts any declaration whose single line contains both
    `buildModel` and `Map` with `static` and `public`. Avoids AST parsing.
    """
    text = java_path.read_text(encoding="utf-8", errors="replace")
    for line in text.splitlines():
        s = line.strip()
        if ("buildModel" in s and "Map" in s
                and "static" in s and "public" in s):
            return True
    return False


# ---- Classname extraction ----

def extract_public_class_name(java_path: Path) -> str:
    """Return the declared public class name of a .java file.

    Falls back to the filename stem if no `public class` declaration
    is found on a single line.
    """
    text = java_path.read_text(encoding="utf-8", errors="replace")
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("public class ") or s.startswith("public final class "):
            parts = s.replace("public final class", "public class").split()
            idx = parts.index("class")
            if idx + 1 < len(parts):
                name = parts[idx + 1]
                for stop in ("{", "<", " "):
                    if stop in name:
                        name = name.split(stop, 1)[0]
                return name
    return java_path.stem


# ---- Validation ----

def validate_mph(mph_path: Path) -> None:
    """Assert the file is a COMSOL .mph with required structural pieces.

    Raises MphValidationError on any failure.
    """
    if not mph_path.exists():
        raise MphValidationError(f".mph not written: {mph_path}")
    if mph_path.stat().st_size < 1024:
        raise MphValidationError(
            f".mph suspiciously small ({mph_path.stat().st_size} bytes): "
            f"{mph_path}"
        )
    try:
        with zipfile.ZipFile(mph_path) as zf:
            names = set(zf.namelist())
    except zipfile.BadZipFile as e:
        raise MphValidationError(f"Not a valid zip archive: {e}") from e
    missing = REQUIRED_MPH_MEMBERS - names
    if missing:
        raise MphValidationError(
            f".mph missing required members {sorted(missing)} "
            f"(present: {sorted(names)[:10]}...)"
        )


def sha256_of_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


# ---- Main API ----

def generate_mph(
    builder_java: Path,
    output_mph: Path,
    builder_args: dict | None = None,
    *,
    solve_study: str | None = None,
    license_timeout_s: float | None = None,
    comsol_path: str = COMSOL_PATH,
    workspace_dir: Path | None = None,
    timeout_s: int = 600,
    force: bool = False,
    write_sidecar: bool = True,
    run_linting: bool = True,
    skip_layer_a: bool = False,
    skip_layer_b: bool = False,
    skip_layer_c: bool = False,
    db_conn=None,
    build_id: str | None = None,
    on_event=None,
    stream_mirror=None,
) -> MphgenResult:
    """Compile builder + ModelExporter, run, validate, write sidecar.

    Args:
        builder_java: Path to a .java file exposing buildModel.
        output_mph: Where to write the .mph. Parent dir created if missing.
        builder_args: Dict forwarded to buildModel as --arg key=value pairs.
        comsol_path: COMSOL installation root (default: module constant).
        workspace_dir: Build workspace for compiled .class files. Defaults
            to a tmpdir next to the builder.
        timeout_s: JVM execution timeout.
        force: Overwrite output if it exists.
        write_sidecar: Write <output>.mphgen.json with provenance.
        run_linting: After a successful build, run Layer A (source .java
            scan) + Layer B (Java ModelChecker on the .mph) and write
            <output>.units.json + <output>.descriptions.json. Lint
            failures are logged, never raised.
        skip_layer_a: Skip the static-source lint pass.
        skip_layer_b: Skip the runtime ModelChecker pass (useful when
            COMSOL cannot be initialized in the current environment).
        db_conn: Optional sqlite3.Connection. When supplied together
            with `build_id`, telemetry events emitted during the run are
            ingested into the `telemetry_events` table after the JSONL
            sidecar is written. mphgen otherwise remains DB-free. A
            DB-ingest failure never fails the build — it is logged and
            the run returns normally.
        build_id: Optional build ID associating ingested telemetry with
            a build row. Required together with `db_conn` for DB ingest.
        on_event: Optional callable invoked with each parsed telemetry
            event as it arrives. Runs on the draining thread — keep it
            cheap and non-blocking. Exceptions are logged, never raised.
        stream_mirror: Optional writeable stream (e.g. sys.stdout) that
            receives every raw subprocess line as it arrives — a cheap
            live-console view of solver progress.

    Returns:
        MphgenResult with success flag and provenance.

    Raises:
        MphgenError subclasses on validation/compile/run failures.
    """
    builder_java = Path(builder_java).resolve()
    output_mph = Path(output_mph).resolve()
    builder_args = dict(builder_args or {})

    if not builder_java.is_file():
        raise MphgenError(f"Builder not found: {builder_java}")
    if builder_java.suffix != ".java":
        raise MphgenError(f"Builder must be a .java file: {builder_java}")
    if output_mph.exists() and not force:
        raise MphgenError(
            f"Output .mph already exists: {output_mph}. Pass force=True "
            "or --force to overwrite."
        )
    if not has_build_model_contract(builder_java):
        raise ContractError(
            f"{builder_java.name} does not expose "
            "`public static Model buildModel(Map<String,String>)`. "
            "See docs/mphgen.md for the contract and adapter pattern."
        )

    builder_class = extract_public_class_name(builder_java)
    output_mph.parent.mkdir(parents=True, exist_ok=True)

    # Workspace: put compiled .class files next to the builder by default,
    # so dynamic classloading via -cp finds them. An explicit workspace_dir
    # overrides.
    if workspace_dir is None:
        workspace_dir = builder_java.parent
    workspace_dir = Path(workspace_dir).resolve()
    workspace_dir.mkdir(parents=True, exist_ok=True)

    facade = JavaFacade(
        comsol_path=comsol_path,
        workspace_dir=workspace_dir,
        java_source_dir=Path(__file__).parent / "java",
    )

    # ---- Compile ModelExporter + SolverTelemetry (once per workspace).
    # Compiled together because ModelExporter references SolverTelemetry.
    exporter_class_file = facade.compiled_dir / f"{EXPORTER_CLASS}.class"
    exporter_source = facade.java_source_dir / EXPORTER_SOURCE
    telemetry_source = facade.java_source_dir / TELEMETRY_SOURCE
    telemetry_class_file = facade.compiled_dir / "SolverTelemetry.class"
    needs_compile = (
        not exporter_class_file.exists()
        or not telemetry_class_file.exists()
        or exporter_source.stat().st_mtime > exporter_class_file.stat().st_mtime
        or telemetry_source.stat().st_mtime
            > telemetry_class_file.stat().st_mtime
    )
    if needs_compile:
        logger.info("Compiling %s + %s", EXPORTER_SOURCE, TELEMETRY_SOURCE)
        javac = facade.find_java_executable("javac")
        facade.compiled_dir.mkdir(parents=True, exist_ok=True)
        cp = facade.get_full_classpath()
        r = subprocess.run(
            [javac, "-cp", cp, "-d", str(facade.compiled_dir),
             str(telemetry_source), str(exporter_source)],
            capture_output=True, text=True, timeout=60,
            encoding="utf-8", errors="replace",
        )
        if r.returncode != 0:
            raise MphgenError(
                f"Failed to compile {EXPORTER_SOURCE} + "
                f"{TELEMETRY_SOURCE}:\n{r.stderr}"
            )

    # ---- Compile the builder into the workspace
    logger.info("Compiling %s", builder_java.name)
    cr = facade.compile_stage_code(builder_java)
    if not cr.success:
        raise MphgenError(
            f"Failed to compile {builder_java.name}:\n{cr.stderr}"
        )

    # ---- Run ModelExporter
    logger.info("Running ModelExporter (builder=%s)", builder_class)
    java_exe = facade.find_java_executable("java")
    cp = facade.get_full_classpath()
    argv = [
        java_exe, "-Djava.awt.headless=true",
        "-cp", cp,
        EXPORTER_CLASS,
        "--builder-class", builder_class,
        "--output", str(output_mph),
    ]
    for k, v in builder_args.items():
        argv += ["--arg", f"{k}={v}"]
    if solve_study:
        argv += ["--solve", solve_study]
    lic_timeout = resolve_license_timeout(license_timeout_s)
    if lic_timeout:
        argv += ["--license-timeout", str(lic_timeout)]

    # ---- Streaming telemetry loop -----------------------------------------
    # We run ModelExporter via Popen and drain stdout line-by-line so that
    # TELEMETRY: events are appended to the JSONL sidecar as they arrive —
    # giving the user (and any interfacing agent) real-time, chunked
    # visibility during long solves, and preserving a complete record even
    # if the JVM is killed mid-run. stderr is merged into stdout to avoid
    # pipe-buffer deadlocks.
    from comsol_support.telemetry import (
        digest as _digest,
        ingest_sidecar_to_db,
        partial_path_for,
        stream_telemetry_lines,
        truncate_telemetry_sidecar,
    )
    telemetry_sidecar = truncate_telemetry_sidecar(output_mph)
    # Crash-safe raw-log tee: full unfiltered JVM stream to
    # disk line-by-line, so a killed/hung run still leaves evidence.
    jvm_log = output_mph.with_suffix(output_mph.suffix + ".jvm.log")

    # One-COMSOL-JVM discipline — see jvm_slot.py. Slot
    # outcome goes to the run log; the sidecar stays purely JVM-emitted
    # (it must exist EMPTY at Popen time for tail -f consumers).
    from comsol_support.jvm_slot import acquire_jvm_slot, warn_leaked_jvms
    warn_leaked_jvms(logger)
    jvm_slot = acquire_jvm_slot(f"mphgen {output_mph.name}")

    # Drain stdout on a background thread so a hung child (blocked solver
    # emitting no output) cannot starve the timeout watchdog on the main
    # thread. The drainer returns when the child closes its stdout, which
    # happens on normal exit OR when we proc.kill() on timeout.
    import threading
    t0 = time.time()
    proc = subprocess.Popen(
        argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        # COMSOL solver logs may carry non-UTF8 bytes; never let a stray
        # byte abort the drain thread mid-solve. Pin UTF-8 so Windows
        # doesn't decode with the legacy locale codepage.
        encoding="utf-8", errors="replace",
        bufsize=1, env=facade.get_comsol_env(),
        # Own the JVM's process group so a timeout can SIGTERM the whole
        # group — a clean TCP close lets the floating-license server
        # reclaim the seat immediately instead of leaving a ghost checkout.
        start_new_session=(os.name == "posix"),
    )
    drain_result: dict = {"stdout": "", "events": [], "error": None}

    def _drain():
        try:
            txt, evs = stream_telemetry_lines(
                proc.stdout, telemetry_sidecar,
                on_event=on_event, mirror=stream_mirror,
                raw_log_path=jvm_log,
            )
            drain_result["stdout"] = txt
            drain_result["events"] = evs
        except BaseException as e:  # noqa: BLE001 — record and exit thread
            drain_result["error"] = e

    drainer = threading.Thread(target=_drain, name="mphgen-drain", daemon=True)
    drainer.start()
    try:
        proc.wait(timeout=timeout_s)
    except subprocess.TimeoutExpired as e:
        _terminate_process_group(proc)
        drainer.join(timeout=5)
        raise BuildFailure(
            f"ModelExporter timed out after {timeout_s}s; "
            f"telemetry sidecar: {telemetry_sidecar}"
        ) from e
    finally:
        drainer.join(timeout=10)
        if proc.stdout is not None:
            try:
                proc.stdout.close()
            except Exception:
                pass
        jvm_slot.release()

    if drain_result["error"] is not None:
        raise drain_result["error"]

    telemetry_events = drain_result["events"]

    # Synthesize a lightweight CompletedProcess-like object so the rest of
    # this function (envelope parsing, error reporting) reads naturally.
    class _StreamedProc:
        pass
    _sp = _StreamedProc()
    _sp.stdout = drain_result["stdout"]
    _sp.stderr = ""
    _sp.returncode = proc.returncode
    proc = _sp

    telemetry_digest = _digest(telemetry_events)

    # Opt-in DB ingest. Closes the telemetry loop for callers that
    # supply both a connection and a build_id. Wrapped in try/except
    # so a DB failure never fails the build.
    if db_conn is not None and build_id is not None and telemetry_events:
        try:
            ingest_sidecar_to_db(
                db_conn, build_id, output_mph, events=telemetry_events,
            )
        except Exception as e:
            logger.warning(
                "telemetry DB ingest skipped for build %s: %s", build_id, e,
            )

    # Parse the last JSON-looking line on stdout. The JVM may print
    # banner lines and TELEMETRY: lines; the emit() convention is one
    # JSON envelope object — take the last non-empty line that is not a
    # telemetry line and parse it.
    stdout_lines = [
        l for l in proc.stdout.splitlines()
        if l.strip() and not l.startswith("TELEMETRY: ")
    ]
    envelope = None
    for line in reversed(stdout_lines):
        try:
            envelope = json.loads(line)
            break
        except json.JSONDecodeError:
            continue
    if envelope is None:
        # NOTE: proc.stderr is None here (merged into stdout by the
        # streaming drain) — the old message sliced it and raised
        # TypeError, masking the real failure. Use the drained text.
        forensics = describe_abnormal_exit(
            proc.returncode, getattr(proc, "pid", None),
            [Path.cwd(), Path(workspace_dir)],
        )
        try:
            jvm_log_empty = jvm_log.stat().st_size == 0
        except OSError:
            jvm_log_empty = True
        raise BuildFailure(
            f"ModelExporter produced no JSON envelope. "
            f"exit={proc.returncode}; "
            f"stdout={drain_result['stdout'][:500]}\n"
            + (f"{forensics}\n" if forensics else "")
            + f"telemetry sidecar: {telemetry_sidecar}\n"
            f"raw JVM log: {jvm_log}"
            + (" (EMPTY — process died before emitting any output)"
               if jvm_log_empty else "")
        )
    if not envelope.get("success"):
        err = envelope.get("error", "unknown error")
        trace = envelope.get("stack_trace", "")
        partial = telemetry_digest.partial_save_path
        halt = telemetry_digest.halt_reason
        msg = f"ModelExporter failed: {err}"
        if halt:
            msg += f"\nhalt_reason: {halt}"
        if partial:
            msg += f"\npartial .mph: {partial}"
        msg += f"\ntelemetry sidecar: {telemetry_sidecar}"
        if trace:
            msg += f"\nTrace: {trace[:500]}"
        raise BuildFailure(msg)

    # ---- Validate .mph
    validate_mph(output_mph)

    # Stale-partial cleanup: if a prior failed run left a .partial.mph
    # sibling, the successful run supersedes it. Removal is best-effort
    # — a stale partial is confusing but never a correctness issue, so
    # any error here is logged and ignored.
    stale_partial = partial_path_for(output_mph)
    if stale_partial.exists():
        try:
            stale_partial.unlink()
            logger.info("cleaned up stale partial %s", stale_partial.name)
        except OSError as e:
            logger.warning("could not remove stale partial %s: %s",
                           stale_partial, e)

    result = MphgenResult(
        success=True,
        builder_java=str(builder_java),
        builder_class=builder_class,
        output_mph=str(output_mph),
        output_bytes=output_mph.stat().st_size,
        output_sha256=sha256_of_file(output_mph),
        builder_args=builder_args,
        solved=bool(envelope.get("solved", False)),
        solve_study=envelope.get("study"),
        elapsed_ms=int((time.time() - t0) * 1000),
        telemetry_sidecar_path=str(telemetry_sidecar),
        telemetry_event_count=telemetry_digest.event_count,
        telemetry_halt_reason=telemetry_digest.halt_reason,
        telemetry_partial_save_path=telemetry_digest.partial_save_path,
        telemetry_digest=telemetry_digest.as_dict(),
    )

    if write_sidecar:
        sidecar = output_mph.with_suffix(output_mph.suffix + ".mphgen.json")
        sidecar.write_text(json.dumps(asdict(result), indent=2) + "\n",
                           encoding="utf-8")
        result.sidecar_path = str(sidecar)

    # Optional linting: Layer A on the source .java, Layer B on the .mph.
    # Imports are local so mphgen remains importable without linting deps.
    if run_linting:
        try:
            _run_linting_after_build(
                builder_java=builder_java, output_mph=output_mph,
                comsol_path=comsol_path, workspace_dir=workspace_dir,
                skip_layer_a=skip_layer_a, skip_layer_b=skip_layer_b,
                skip_layer_c=skip_layer_c,
            )
        except Exception as e:  # never fail the build on lint errors
            logger.warning("linting skipped after build: %s", e)

    logger.info(
        "Wrote %s (%d bytes) in %d ms",
        output_mph.name, result.output_bytes, result.elapsed_ms,
    )
    return result


def _run_linting_after_build(
    *,
    builder_java: Path,
    output_mph: Path,
    comsol_path: str,
    workspace_dir: Path,
    skip_layer_a: bool,
    skip_layer_b: bool,
    skip_layer_c: bool,
) -> None:
    """Run all three linting layers post-build and write merged sidecars.

    Never raises — the caller catches exceptions because linting must
    not block the build.
    """
    from comsol_support.linting import (
        run_layer_b, scan_java_source, summarise_sidecars, write_sidecars,
    )

    layer_a = None
    if not skip_layer_a:
        try:
            layer_a = scan_java_source(builder_java)
        except Exception as e:
            logger.warning("Layer A scan failed: %s", e)

    units_b = descr_b = symbols_b = None
    if not skip_layer_b:
        try:
            units_b, descr_b, symbols_b = run_layer_b(
                output_mph,
                comsol_path=comsol_path,
                workspace_dir=workspace_dir,
            )
        except Exception as e:
            logger.warning("Layer B runtime check failed: %s", e)

    layer_c_units = None
    if not skip_layer_c and symbols_b is not None:
        try:
            from comsol_support.dimensional import (
                analyze_model, findings_to_sidecar,
            )
            findings = analyze_model(symbols_b)
            layer_c_units = findings_to_sidecar(findings)
        except Exception as e:
            logger.warning("Layer C dimensional analysis failed: %s", e)

    units_path, descr_path = write_sidecars(
        output_mph,
        layer_a_report=layer_a,
        layer_b_units=units_b,
        layer_b_descriptions=descr_b,
        layer_c_units=layer_c_units,
    )
    s = summarise_sidecars(layer_a, units_b, descr_b, layer_c_units)
    logger.info(
        "Linted %s — units: %d warnings, %d errors; "
        "descriptions: %d missing, %d placeholder; "
        "dimensional: %d warnings",
        output_mph.name, s["units_warnings"], s["units_errors"],
        s["descr_missing"], s["descr_placeholder"],
        s["dimensional_warnings"],
    )
    logger.info("  %s", units_path)
    logger.info("  %s", descr_path)


# ---- CLI adapter ----

def print_gotcha_hints(error_text: str) -> None:
    """Best-effort: surface known-gotcha entries matching a failure.

    Closes the build → fail → diagnose loop without a separate command.
    Suggestions are advisory, printed to stderr, and never affect the
    exit code; any internal error is swallowed.
    """
    try:
        from comsol_support.gotcha_search import suggest_for_error
        hits = suggest_for_error(error_text)
    except Exception:
        return
    if not hits:
        return
    print("possibly related known gotchas:", file=sys.stderr)
    for g in hits:
        symptom = g.symptom_line()
        suffix = f" — {symptom[:100]}" if symptom else ""
        print(f"  {g.id}{suffix}", file=sys.stderr)
    print("  (details: comsol-support gotcha-search '<id>')", file=sys.stderr)


def cmd_mphgen(args: argparse.Namespace) -> int:
    """CLI handler dispatched from cli.py."""
    builder_args = {}
    for kv in (args.arg or []):
        if "=" not in kv:
            print(f"error: --arg expects key=value, got {kv!r}",
                  file=sys.stderr)
            return 2
        k, _, v = kv.partition("=")
        builder_args[k] = v

    output = Path(args.output) if args.output else \
        Path(args.builder).with_suffix("").parent / (
            extract_public_class_name(Path(args.builder)) + "_unsolved.mph"
        )

    if args.ingest_db and not args.ingest_build_id:
        print("error: --ingest-db requires --ingest-build-id",
              file=sys.stderr)
        return 2

    db_conn = None
    if args.ingest_db:
        from comsol_support.db import init_db
        db_conn = init_db(Path(args.ingest_db))

    mirror = sys.stderr if getattr(args, "stream", False) else None
    try:
        result = generate_mph(
            builder_java=Path(args.builder),
            output_mph=output,
            builder_args=builder_args,
            solve_study=args.solve,
            license_timeout_s=args.license_timeout,
            comsol_path=args.comsol_path or COMSOL_PATH,
            workspace_dir=Path(args.workspace) if args.workspace else None,
            timeout_s=args.timeout,
            force=args.force,
            write_sidecar=not args.no_sidecar,
            run_linting=not args.no_lint,
            skip_layer_a=args.skip_layer_a,
            skip_layer_b=args.skip_layer_b,
            skip_layer_c=args.skip_layer_c,
            db_conn=db_conn,
            build_id=args.ingest_build_id,
            stream_mirror=mirror,
        )
    except ContractError as e:
        print(f"contract error: {e}", file=sys.stderr)
        return 3
    except MphValidationError as e:
        print(f"validation error: {e}", file=sys.stderr)
        return 4
    except (MphgenError, JavaNotFoundError, JavaExecutionError) as e:
        print(f"mphgen error: {e}", file=sys.stderr)
        print_gotcha_hints(str(e))
        return 1
    finally:
        if db_conn is not None:
            db_conn.close()

    print(json.dumps(asdict(result), indent=2))
    return 0


def add_mphgen_subparser(subs: argparse._SubParsersAction) -> None:
    """Attach the `mphgen` subparser. Called from cli.build_parser()."""
    p = subs.add_parser(
        "mphgen",
        help="Generate a GUI-openable .mph from a COMSOL Java builder",
    )
    p.add_argument("--builder", required=True,
                   help="Path to a .java builder exposing "
                        "buildModel(Map<String,String>)")
    p.add_argument("--output", "-o",
                   help="Output .mph path (default: "
                        "<ClassName>_unsolved.mph next to the builder)")
    p.add_argument("--arg", action="append", default=[],
                   help="Key=value pair forwarded to buildModel "
                        "(repeatable)")
    p.add_argument("--solve", metavar="STUDY_TAG", default=None,
                   help="Run the named study before saving. Default is "
                        "unsolved — omit this flag unless a solved .mph "
                        "is explicitly needed.")
    p.add_argument("--comsol-path",
                   help="COMSOL installation root (default: module constant)")
    p.add_argument("--workspace",
                   help="Build workspace for compiled .class files "
                        "(default: next to the builder)")
    p.add_argument("--timeout", type=int, default=600,
                   help="JVM timeout in seconds (default: 600)")
    p.add_argument("--license-timeout", type=float, default=None,
                   help="Bound the COMSOL license checkout in seconds "
                        "(fails fast as halt_reason=license_timeout if no "
                        "seat); falls back to $COMSOL_LICENSE_TIMEOUT.")
    p.add_argument("--force", action="store_true",
                   help="Overwrite an existing output .mph")
    p.add_argument("--no-sidecar", action="store_true",
                   help="Skip writing <output>.mphgen.json provenance file")
    p.add_argument("--no-lint", action="store_true",
                   help="Skip all linting after build")
    p.add_argument("--skip-layer-a", action="store_true",
                   help="Skip the static .java source lint pass")
    p.add_argument("--skip-layer-b", action="store_true",
                   help="Skip the runtime ModelChecker pass on the .mph")
    p.add_argument("--skip-layer-c", action="store_true",
                   help="Skip Layer C dimensional analysis (pure-Python unit algebra)")
    p.add_argument("--ingest-db",
                   help="If set, ingest telemetry into this SQLite DB after "
                        "the build (closes the telemetry loop for MCP queries). "
                        "Requires --ingest-build-id.")
    p.add_argument("--ingest-build-id",
                   help="Build ID to associate with ingested telemetry. "
                        "Required when --ingest-db is given.")
    p.add_argument("--stream", action="store_true",
                   help="Mirror the subprocess' stdout/stderr live to the "
                        "user's stderr as it arrives (solver progress + "
                        "heartbeats). Telemetry is always written to the "
                        "JSONL sidecar regardless of this flag.")
