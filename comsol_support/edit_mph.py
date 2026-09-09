"""edit-mph — load an existing .mph, optionally apply a mutator class,
optionally solve, and save.

Sister to `mphgen`. Where mphgen is `.java builder → .mph`, edit-mph is
`existing.mph → mutate → .mph`. Both go through the same
`ModelExporter` Java entry point so solve, telemetry, partial-save, and
linting plumbing is identical across modes.

Mutator contract (when supplied):
  public static com.comsol.model.Model mutate(
      com.comsol.model.Model m,
      java.util.Map<String,String> args)

If no mutator is supplied, the loaded model is re-saved verbatim — which
is the canonical "solve an existing pre-solve .mph" workflow when
combined with --solve.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
import threading
import time
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
from comsol_support.mphgen import (
    EXPORTER_CLASS,
    EXPORTER_SOURCE,
    TELEMETRY_SOURCE,
    BuildFailure,
    ContractError,
    MphgenError,
    MphValidationError,
    _run_linting_after_build,
    extract_public_class_name,
    sha256_of_file,
    validate_mph,
)

logger = logging.getLogger("comsol_support.edit_mph")


@dataclass
class EditMphResult:
    success: bool
    input_mph: str
    mutator_java: str | None
    mutator_class: str | None
    output_mph: str
    output_bytes: int = 0
    output_sha256: str = ""
    mutator_args: dict = field(default_factory=dict)
    query_class: str | None = None
    query_result: dict | None = None
    saved: bool = True
    solved: bool = False
    solve_study: str | None = None
    elapsed_ms: int = 0
    sidecar_path: str = ""
    error: str = ""
    telemetry_sidecar_path: str = ""
    telemetry_event_count: int = 0
    telemetry_halt_reason: str | None = None
    telemetry_error_detail: list = field(default_factory=list)
    telemetry_partial_save_path: str | None = None
    telemetry_digest: dict = field(default_factory=dict)
    # Crash-safe on-disk tee of the FULL raw JVM stream.
    jvm_log_path: str = ""
    # First-class mesh build (--mesh): the tag that ran and
    # whether ms.run() completed without throwing. mesh_success=False
    # with success=True means a PARTIAL mesh was saved (score it via the
    # mesh_census in the telemetry digest, not via this flag alone).
    mesh_tag: str | None = None
    mesh_success: bool | None = None


def has_mutator_contract(java_path: Path) -> bool:
    """Detect `static Model mutate(Model, Map<String,String>)` declaration.

    Substring-level check, same shape as `has_build_model_contract`.
    """
    text = java_path.read_text(encoding="utf-8", errors="replace")
    for line in text.splitlines():
        s = line.strip()
        if ("mutate" in s and "Model" in s and "Map" in s
                and "static" in s and "public" in s):
            return True
    return False


def has_query_contract(java_path: Path) -> bool:
    """Detect `static Map<String,Object> query(Model, Map<String,String>)`.

    Substring-level check, same shape as `has_mutator_contract`.
    """
    text = java_path.read_text(encoding="utf-8", errors="replace")
    for line in text.splitlines():
        s = line.strip()
        if ("query" in s and "Model" in s and "Map" in s
                and "static" in s and "public" in s):
            return True
    return False


def _discard_inplace_tmp(tmp: Path | None) -> None:
    """Best-effort removal of the in-place-edit temp output on failure."""
    if tmp is None:
        return
    try:
        if tmp.exists():
            tmp.unlink()
    except OSError as e:
        logger.warning("could not remove in-place temp %s: %s", tmp, e)


def edit_mph(
    input_mph: Path,
    output_mph: Path,
    *,
    mutator_java: Path | None = None,
    mutator_args: dict | None = None,
    solve_study: str | None = None,
    mesh_tag: str | None = None,
    jvm_args: list[str] | None = None,
    no_save: bool = False,
    query_java: Path | None = None,
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
) -> EditMphResult:
    """Load + optionally mutate + optionally solve + save.

    Args:
        input_mph: Path to an existing .mph to load.
        output_mph: Path to write. May equal `input_mph` for in-place edit
            (the loaded model is saved to the same path).
        mutator_java: Optional .java with `static Model mutate(Model, Map)`.
        mutator_args: Dict forwarded to mutate as --arg key=value pairs.
        solve_study: Optional study tag to run before saving.
        Other args mirror generate_mph().

    Returns:
        EditMphResult with success flag, provenance, and telemetry digest.

    Raises:
        MphgenError subclasses on validation/compile/run failures.
        (Reuses the mphgen exception hierarchy so callers handle one set.)
    """
    input_mph = Path(input_mph).resolve()
    output_mph = Path(output_mph).resolve()
    mutator_args = dict(mutator_args or {})

    if not input_mph.is_file():
        raise MphgenError(f"Input .mph not found: {input_mph}")
    # Output-exists guard before any other expensive check — caller's
    # most common mistake. Skipped in no-save mode: nothing is written, so
    # the output path only names the telemetry/provenance sidecars.
    if (not no_save and output_mph.exists() and not force
            and output_mph != input_mph):
        raise MphgenError(
            f"Output .mph already exists: {output_mph}. Pass force=True "
            "or --force to overwrite. (To edit in place, set output == input.)"
        )
    # Pre-validate input is a real .mph zip — fail fast before launching JVM.
    try:
        validate_mph(input_mph)
    except MphValidationError as e:
        raise MphValidationError(f"Input .mph invalid: {e}") from e

    mutator_class: str | None = None
    if mutator_java is not None:
        mutator_java = Path(mutator_java).resolve()
        if not mutator_java.is_file():
            raise MphgenError(f"Mutator not found: {mutator_java}")
        if mutator_java.suffix != ".java":
            raise MphgenError(f"Mutator must be a .java file: {mutator_java}")
        if not has_mutator_contract(mutator_java):
            raise ContractError(
                f"{mutator_java.name} does not expose "
                "`public static Model mutate(Model, Map<String,String>)`. "
                "See docs/mphedit.md for the contract."
            )
        mutator_class = extract_public_class_name(mutator_java)

    query_class: str | None = None
    if query_java is not None:
        query_java = Path(query_java).resolve()
        if not query_java.is_file():
            raise MphgenError(f"Query class not found: {query_java}")
        if query_java.suffix != ".java":
            raise MphgenError(f"Query must be a .java file: {query_java}")
        if not has_query_contract(query_java):
            raise ContractError(
                f"{query_java.name} does not expose "
                "`public static Map<String,Object> "
                "query(Model, Map<String,String>)`. "
                "See docs/query-mph.md for the contract."
            )
        query_class = extract_public_class_name(query_java)
        # A query is inherently read-only — never write the model.
        no_save = True

    output_mph.parent.mkdir(parents=True, exist_ok=True)

    # In-place edits must never write the input path directly: a JVM death
    # mid-model.save() (timeout group-kill, OOM, full disk) would destroy
    # the only copy of the model. Save to a temp sibling instead and
    # atomically rename over the input only after the new file validates.
    inplace_tmp: Path | None = None
    jvm_output = output_mph
    if not no_save and output_mph == input_mph:
        inplace_tmp = output_mph.parent / (output_mph.name + ".inplace-tmp.mph")
        if inplace_tmp.exists():
            inplace_tmp.unlink()  # leftover from a prior crashed run
        jvm_output = inplace_tmp

    if workspace_dir is None:
        workspace_dir = (
            mutator_java.parent if mutator_java is not None
            else query_java.parent if query_java is not None
            else output_mph.parent
        )
    workspace_dir = Path(workspace_dir).resolve()
    workspace_dir.mkdir(parents=True, exist_ok=True)

    facade = JavaFacade(
        comsol_path=comsol_path,
        workspace_dir=workspace_dir,
        java_source_dir=Path(__file__).parent / "java",
    )

    # ---- Compile ModelExporter + SolverTelemetry (once per workspace).
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

    # ---- Compile the mutator if supplied
    if mutator_java is not None:
        logger.info("Compiling %s", mutator_java.name)
        cr = facade.compile_stage_code(mutator_java)
        if not cr.success:
            raise MphgenError(
                f"Failed to compile {mutator_java.name}:\n{cr.stderr}"
            )

    # ---- Compile the query class if supplied
    if query_java is not None:
        logger.info("Compiling %s", query_java.name)
        cr = facade.compile_stage_code(query_java)
        if not cr.success:
            raise MphgenError(
                f"Failed to compile {query_java.name}:\n{cr.stderr}"
            )

    # ---- Run ModelExporter in edit mode
    logger.info(
        "Running ModelExporter (edit mode, input=%s, mutator=%s)",
        input_mph.name, mutator_class,
    )
    java_exe = facade.find_java_executable("java")
    cp = facade.get_full_classpath()
    argv = [
        java_exe, "-Djava.awt.headless=true",
        # User JVM flags (e.g. -Xmx96g for large meshes) —
        # appended after the headless flag so they can override defaults.
        *(jvm_args or []),
        "-cp", cp,
        EXPORTER_CLASS,
        "--input", str(input_mph),
        "--output", str(jvm_output),
    ]
    if mutator_class is not None:
        argv += ["--mutator-class", mutator_class]
    if query_class is not None:
        argv += ["--query-class", query_class]
    for k, v in mutator_args.items():
        argv += ["--arg", f"{k}={v}"]
    if solve_study:
        argv += ["--solve", solve_study]
    if mesh_tag:
        argv += ["--mesh", mesh_tag]
    if no_save and query_class is None:
        # --query-class already implies no-save on the Java side; avoid a
        # redundant flag.
        argv += ["--no-save"]
    lic_timeout = resolve_license_timeout(license_timeout_s)
    if lic_timeout:
        argv += ["--license-timeout", str(lic_timeout)]

    # ---- Streaming telemetry loop (mirrors mphgen.generate_mph) ----
    from comsol_support.telemetry import (
        digest as _digest,
        ingest_sidecar_to_db,
        partial_path_for,
        stream_telemetry_lines,
        truncate_telemetry_sidecar,
    )
    telemetry_sidecar = truncate_telemetry_sidecar(output_mph)
    # Crash-safe raw-log tee: the full unfiltered JVM stream (solver/mesh
    # log, thread dumps, envelope) goes to disk line-by-line, so a killed
    # or hung run still leaves evidence. Previously the raw stream
    # lived only in memory until exit — a post-campaign audit traced a
    # fabricated-OOM narrative to that hole (six 0-byte logs).
    jvm_log = output_mph.with_suffix(output_mph.suffix + ".jvm.log")

    # One-COMSOL-JVM discipline: warn about pre-existing
    # COMSOL JVMs (leaks hold license seats) and take the advisory slot
    # (warn-and-proceed by default; COMSOL_AGENT_EXCLUSIVE=1 enforces).
    # The outcome lives in the run log — the sidecar stays purely
    # JVM-emitted (it must exist EMPTY at Popen time for tail -f
    # consumers, a contract pinned by the telemetry tests).
    from comsol_support.jvm_slot import JvmSlotTimeout, acquire_jvm_slot, \
        warn_leaked_jvms
    warn_leaked_jvms(logger)
    try:
        jvm_slot = acquire_jvm_slot(f"edit-mph {input_mph.name}")
    except JvmSlotTimeout:
        _discard_inplace_tmp(inplace_tmp)
        raise

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

    drainer = threading.Thread(target=_drain, name="editmph-drain", daemon=True)
    drainer.start()
    try:
        proc.wait(timeout=timeout_s)
    except subprocess.TimeoutExpired as e:
        _terminate_process_group(proc)
        drainer.join(timeout=5)
        _discard_inplace_tmp(inplace_tmp)
        raise BuildFailure(
            f"ModelExporter timed out after {timeout_s}s; "
            f"telemetry sidecar: {telemetry_sidecar}; "
            f"raw JVM log: {jvm_log}"
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
        _discard_inplace_tmp(inplace_tmp)
        raise drain_result["error"]

    telemetry_events = drain_result["events"]
    telemetry_digest = _digest(telemetry_events)

    if db_conn is not None and build_id is not None and telemetry_events:
        try:
            ingest_sidecar_to_db(
                db_conn, build_id, output_mph, events=telemetry_events,
            )
        except Exception as e:
            logger.warning(
                "telemetry DB ingest skipped for build %s: %s", build_id, e,
            )

    stdout_lines = [
        l for l in drain_result["stdout"].splitlines()
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
        _discard_inplace_tmp(inplace_tmp)
        # Evidence-discipline note: if the raw JVM
        # log is EMPTY, the process died before emitting anything —
        # absence of output is a fact about the death, never a
        # measurement of what the run did.
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
            + (" (EMPTY — process died before emitting any output; do "
               "not cite absence of output as a measurement)"
               if jvm_log_empty else "")
        )
    if not envelope.get("success"):
        err = envelope.get("error", "unknown error")
        trace = envelope.get("stack_trace", "")
        partial = telemetry_digest.partial_save_path
        halt = telemetry_digest.halt_reason
        # Prefer the structured COMSOL diagnostic harvested from the
        # exception chain (envelope error_detail, falling back to the
        # telemetry digest) over the generic envelope error string.
        detail = envelope.get("error_detail") or telemetry_digest.error_detail
        msg = f"ModelExporter failed: {err}"
        if halt:
            msg += f"\nhalt_reason: {halt}"
        if detail:
            msg += "\nerror_detail:\n" + "\n".join(
                f"  - {line}" for line in detail)
        if partial:
            msg += f"\npartial .mph: {partial}"
        msg += f"\ntelemetry sidecar: {telemetry_sidecar}"
        if trace:
            msg += f"\nTrace: {trace[:500]}"
        _discard_inplace_tmp(inplace_tmp)
        raise BuildFailure(msg)

    # In no-save mode no .mph is written, so there is nothing to validate
    # or clean up — skip straight to reporting.
    if not no_save:
        try:
            validate_mph(jvm_output)
        except MphValidationError:
            _discard_inplace_tmp(inplace_tmp)
            raise
        if inplace_tmp is not None:
            # Validated replacement in hand — atomically supersede the
            # input. os.replace is atomic on the same filesystem (same
            # directory by construction).
            os.replace(inplace_tmp, output_mph)

        for stale_partial in {partial_path_for(output_mph),
                              partial_path_for(jvm_output)}:
            if stale_partial.exists():
                try:
                    stale_partial.unlink()
                    logger.info("cleaned up stale partial %s",
                                stale_partial.name)
                except OSError as e:
                    logger.warning("could not remove stale partial %s: %s",
                                   stale_partial, e)

    result = EditMphResult(
        success=True,
        input_mph=str(input_mph),
        mutator_java=str(mutator_java) if mutator_java else None,
        mutator_class=mutator_class,
        output_mph=str(output_mph),
        output_bytes=0 if no_save else output_mph.stat().st_size,
        output_sha256="" if no_save else sha256_of_file(output_mph),
        mutator_args=mutator_args,
        query_class=query_class,
        query_result=envelope.get("query_result"),
        saved=not no_save,
        solved=bool(envelope.get("solved", False)),
        solve_study=envelope.get("study"),
        elapsed_ms=int((time.time() - t0) * 1000),
        telemetry_sidecar_path=str(telemetry_sidecar),
        telemetry_event_count=telemetry_digest.event_count,
        telemetry_halt_reason=telemetry_digest.halt_reason,
        telemetry_error_detail=list(telemetry_digest.error_detail),
        telemetry_partial_save_path=telemetry_digest.partial_save_path,
        telemetry_digest=telemetry_digest.as_dict(),
        jvm_log_path=str(jvm_log),
        mesh_tag=envelope.get("mesh_tag"),
        mesh_success=envelope.get("mesh_success"),
    )

    if write_sidecar:
        sidecar = output_mph.with_suffix(output_mph.suffix + ".mphedit.json")
        sidecar.write_text(json.dumps(asdict(result), indent=2) + "\n",
                           encoding="utf-8")
        result.sidecar_path = str(sidecar)

    # Mid-run "*_error" events (e.g. a mutator that swallowed a mesh
    # exception and reported it via SolverTelemetry.emitError) do not
    # fail the run — a preserved partial mesh is a legitimate outcome —
    # but they must not be invisible either (halt_reason stays
    # "success"). Surface a one-line notice; full detail is in the
    # digest's error_event_detail (sidecar + result).
    if telemetry_digest.error_events:
        head = telemetry_digest.error_event_detail[:1]
        logger.warning(
            "%d mid-run error event(s) recorded in telemetry despite "
            "halt_reason=%s%s — see error_event_detail in the digest "
            "(%s)",
            telemetry_digest.error_events,
            telemetry_digest.halt_reason,
            f" (first: {head[0]})" if head else "",
            result.sidecar_path or "telemetry sidecar",
        )

    # Optional linting: scan the mutator .java (Layer A) and the output
    # .mph (Layer B + C). The mutator is the analogue of mphgen's builder
    # source; lint it as such. When no mutator is supplied (pure
    # load-resave or solve-existing), Layer A is skipped because there is
    # no new .java source to scan.
    # In no-save mode there is no output .mph on disk, so the runtime
    # passes (Layer B + C) have nothing to open — only the static Layer A
    # mutator scan is meaningful.
    if run_linting:
        try:
            _run_linting_after_build(
                builder_java=mutator_java if mutator_java is not None
                    else output_mph.with_suffix(".java"),  # placeholder
                output_mph=output_mph,
                comsol_path=comsol_path,
                workspace_dir=workspace_dir,
                skip_layer_a=skip_layer_a or mutator_java is None,
                skip_layer_b=skip_layer_b or no_save,
                skip_layer_c=skip_layer_c or no_save,
            )
        except Exception as e:
            logger.warning("linting skipped after edit: %s", e)

    if no_save:
        logger.info(
            "Loaded %s, ran %s, discarded (no-save) in %d ms",
            input_mph.name,
            query_class or mutator_class or "no mutator",
            result.elapsed_ms,
        )
    else:
        logger.info(
            "Wrote %s (%d bytes) in %d ms",
            output_mph.name, result.output_bytes, result.elapsed_ms,
        )
    return result


# ---- CLI adapter ----

def cmd_editmph(args: argparse.Namespace) -> int:
    mutator_args: dict[str, str] = {}
    for kv in (args.arg or []):
        if "=" not in kv:
            print(f"error: --arg expects key=value, got {kv!r}",
                  file=sys.stderr)
            return 2
        k, _, v = kv.partition("=")
        mutator_args[k] = v

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
        result = edit_mph(
            input_mph=Path(args.input),
            output_mph=Path(args.output),
            mutator_java=Path(args.mutator) if args.mutator else None,
            mutator_args=mutator_args,
            solve_study=args.solve,
            mesh_tag=args.mesh,
            jvm_args=args.jvm_arg or None,
            no_save=args.no_save,
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
        print(f"edit-mph error: {e}", file=sys.stderr)
        from comsol_support.mphgen import print_gotcha_hints
        print_gotcha_hints(str(e))
        return 1
    finally:
        if db_conn is not None:
            db_conn.close()

    print(json.dumps(asdict(result), indent=2))
    return 0


def add_editmph_subparser(subs: argparse._SubParsersAction) -> None:
    p = subs.add_parser(
        "edit-mph",
        help="Load an existing .mph, optionally mutate, optionally solve, save",
    )
    p.add_argument("--input", required=True,
                   help="Path to an existing .mph to load")
    p.add_argument("--mutator",
                   help="Path to a .java mutator exposing "
                        "mutate(Model, Map<String,String>). "
                        "Optional — omit for pure load→save (e.g. to "
                        "solve an existing pre-solve model).")
    p.add_argument("--output", "-o", required=True,
                   help="Output .mph path. May equal --input for in-place "
                        "edit.")
    p.add_argument("--arg", action="append", default=[],
                   help="Key=value forwarded to mutate (repeatable)")
    p.add_argument("--solve", metavar="STUDY_TAG", default=None,
                   help="Run the named study before saving.")
    p.add_argument("--mesh", metavar="MESH_TAG", default=None,
                   help="Run the named mesh sequence (whole-sequence, "
                        "after any mutator, before any solve) with "
                        "mesh_heartbeat liveness + RSS, a swallowed-but-"
                        "reported failure path (partial meshes still "
                        "save), and an automatic post-run element census "
                        "+ per-feature build records. See docs/meshing.md.")
    p.add_argument("--jvm-arg", action="append", default=[],
                   help="Extra JVM flag (repeatable), e.g. -Xmx96g for "
                        "large meshes. Appended after the headless flag.")
    p.add_argument("--no-save", action="store_true",
                   help="Load (+mutate/+solve) and report via telemetry, "
                        "but DO NOT write the output .mph — for fast "
                        "read-only diagnostics on large models. --output is "
                        "still used only to name the telemetry/provenance "
                        "sidecars.")
    p.add_argument("--comsol-path",
                   help="COMSOL installation root (default: module constant)")
    p.add_argument("--workspace",
                   help="Build workspace for compiled .class files "
                        "(default: next to the mutator, else next to output)")
    p.add_argument("--timeout", type=int, default=600,
                   help="JVM timeout in seconds (default: 600)")
    p.add_argument("--license-timeout", type=float, default=None,
                   help="Bound the COMSOL license checkout (initStandalone "
                        "+ load) in seconds, so a run with no free seat "
                        "fails fast as halt_reason=license_timeout instead "
                        "of blocking. Falls back to $COMSOL_LICENSE_TIMEOUT; "
                        "omitted/0 = wait indefinitely.")
    p.add_argument("--force", action="store_true",
                   help="Overwrite an existing output .mph (no-op when "
                        "output == input)")
    p.add_argument("--no-sidecar", action="store_true",
                   help="Skip writing <output>.mphedit.json provenance")
    p.add_argument("--no-lint", action="store_true",
                   help="Skip all linting after edit")
    p.add_argument("--skip-layer-a", action="store_true",
                   help="Skip the static .java source lint pass on the "
                        "mutator (auto-skipped when no mutator)")
    p.add_argument("--skip-layer-b", action="store_true",
                   help="Skip the runtime ModelChecker pass on the output")
    p.add_argument("--skip-layer-c", action="store_true",
                   help="Skip Layer C dimensional analysis")
    p.add_argument("--ingest-db",
                   help="If set, ingest telemetry into this SQLite DB "
                        "(closes the telemetry loop for MCP queries). "
                        "Requires --ingest-build-id.")
    p.add_argument("--ingest-build-id",
                   help="Build ID to associate with ingested telemetry")
    p.add_argument("--stream", action="store_true",
                   help="Mirror subprocess stdout/stderr live to user's "
                        "stderr as it arrives (solver progress + heartbeats)")
    p.set_defaults(func=cmd_editmph)
