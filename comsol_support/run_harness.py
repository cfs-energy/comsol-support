"""run-harness — compile and run an arbitrary COMSOL-dependent Java class.

The generic standalone-harness runner. Where `mphgen` and `edit-mph` are
purpose-built (`builder → .mph`, `.mph → mutate → .mph`), `run-harness`
runs *any* class you hand it against the assembled COMSOL classpath and
native-library environment, teeing the full raw output to a log. It
exists to collapse the throwaway "compile + classpath + env + Popen +
filter stdout" scripts that every ad-hoc diagnostic otherwise re-creates.

The class is expected to define a standard `public static void main`.
Nothing here is model- or physics-specific: the harness decides what to
do; this command only runs it and preserves its output.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from comsol_support import COMSOL_PATH
from comsol_support.java_facade import (
    JavaCompileError,
    JavaExecutionError,
    JavaFacade,
    JavaNotFoundError,
)


def run_harness(
    java_class: Path,
    harness_args: list[str] | None = None,
    *,
    comsol_path: str = COMSOL_PATH,
    workspace_dir: Path | None = None,
    log_path: Path | None = None,
    timeout_s: int | None = None,
    stream: bool = True,
    jvm_args: list[str] | None = None,
) -> tuple[int, str, Path]:
    """Compile (if a .java) and run a class via JavaFacade.run_class.

    Args:
        java_class: Path to a `.java` source (compiled on demand) or a
            bare class name already present in the workspace.
        harness_args: Arguments forwarded to the class's main.
        workspace_dir: Where compiled classes / the default log live
            (default: next to the source, else cwd).
        log_path: Full-output log file (default:
            <workspace>/<class>.harness.log). The log always captures the
            complete unfiltered stdout+stderr.
        timeout_s: Seconds before the process group is terminated.
        stream: Mirror output live to stderr as it arrives.

    Returns:
        (returncode, full_stdout, log_path).
    """
    harness_args = list(harness_args or [])
    src = Path(java_class)
    is_source = src.suffix == ".java"
    if is_source and not src.is_file():
        raise FileNotFoundError(f"Harness source not found: {src}")

    if workspace_dir is None:
        workspace_dir = src.parent if is_source else Path.cwd()
    workspace_dir = Path(workspace_dir).resolve()
    workspace_dir.mkdir(parents=True, exist_ok=True)

    if log_path is None:
        stem = src.stem if is_source else str(java_class)
        log_path = workspace_dir / f"{stem}.harness.log"
    log_path = Path(log_path)

    facade = JavaFacade(
        comsol_path=comsol_path,
        workspace_dir=workspace_dir,
        java_source_dir=src.parent if is_source else None,
    )

    kwargs: dict = {}
    if jvm_args is not None:
        kwargs["jvm_args"] = jvm_args
    rc, out = facade.run_class(
        src if is_source else str(java_class),
        harness_args,
        stream=stream,
        log_path=log_path,
        timeout=timeout_s,
        **kwargs,
    )
    return rc, out, log_path


# ---- CLI adapter ----

def cmd_run_harness(args: argparse.Namespace) -> int:
    try:
        rc, _out, log_path = run_harness(
            java_class=Path(args.harness),
            harness_args=args.harness_args,
            comsol_path=args.comsol_path or COMSOL_PATH,
            workspace_dir=Path(args.workspace) if args.workspace else None,
            log_path=Path(args.log) if args.log else None,
            timeout_s=args.timeout,
            stream=not args.quiet,
            jvm_args=args.jvm_arg or None,
        )
    except FileNotFoundError as e:
        print(f"run-harness error: {e}", file=sys.stderr)
        return 2
    except JavaCompileError as e:
        print(f"compile error: {e}", file=sys.stderr)
        return 3
    except (JavaExecutionError, JavaNotFoundError) as e:
        print(f"run-harness error: {e}", file=sys.stderr)
        return 1

    # Evidence discipline: an EMPTY log means the
    # process emitted nothing before it ended — say so loudly, so absent
    # output is never later cited as a measurement (the campaign's
    # fabricated-OOM narrative was built on a 0-byte log).
    log_empty = True
    try:
        log_empty = log_path.stat().st_size == 0
    except OSError:
        pass
    if log_empty:
        print(
            f"run-harness WARNING: log is EMPTY ({log_path}) — the "
            "process emitted no output before it ended. Do not cite "
            "absence of output as a measurement.",
            file=sys.stderr,
        )

    print(json.dumps({
        "success": rc == 0,
        "returncode": rc,
        "log": str(log_path),
        "log_bytes": 0 if log_empty else log_path.stat().st_size,
    }, indent=2))
    return 0 if rc == 0 else 1


def add_run_harness_subparser(subs: argparse._SubParsersAction) -> None:
    p = subs.add_parser(
        "run-harness",
        help="Compile + run an arbitrary COMSOL-dependent Java class, "
             "teeing full output to a log",
    )
    p.add_argument("harness",
                   help="Path to a .java source (compiled on demand) or a "
                        "bare class name already on the workspace classpath")
    p.add_argument("harness_args", nargs="*",
                   help="Arguments forwarded to the harness' main(String[])")
    p.add_argument("--comsol-path",
                   help="COMSOL installation root (default: module constant)")
    p.add_argument("--workspace",
                   help="Build workspace for compiled .class files and the "
                        "default log (default: next to the source)")
    p.add_argument("--log",
                   help="Full-output log path (default: "
                        "<workspace>/<class>.harness.log). Always captures "
                        "the complete unfiltered stdout+stderr.")
    p.add_argument("--timeout", type=int, default=None,
                   help="Seconds before the process group is terminated "
                        "(default: no timeout)")
    p.add_argument("--jvm-arg", action="append", default=[],
                   help="Extra JVM flag (repeatable). Overrides the default "
                        "headless + -Xmx48g set when supplied.")
    p.add_argument("--quiet", action="store_true",
                   help="Do not mirror output live to stderr (the log file "
                        "still captures everything)")
    p.set_defaults(func=cmd_run_harness)
