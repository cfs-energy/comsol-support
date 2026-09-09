"""query-mph — load an existing .mph, run a read-only query, never save.

The read-only sibling of `edit-mph`. Where `edit-mph` is built around
mutate → **save**, `query-mph` loads a model, hands it to a user-supplied
query class, prints the returned data as JSON, and discards the model —
no multi-minute, multi-GB write for what is purely introspection.

Query contract:
  public static java.util.Map<String,Object> query(
      com.comsol.model.Model m,
      java.util.Map<String,String> args)

The returned map is JSON-serialized into the run's telemetry and the
result envelope. What it inspects — a coupling-operator selection, mesh
element counts, geometry entity coordinates, an expression evaluated on a
stored solution — is entirely up to the query class; this command is
model- and topic-agnostic.

Implemented on top of `edit_mph(query_java=..., no_save=True)` so the
compile / classpath / telemetry / error-detail plumbing is shared.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from comsol_support import COMSOL_PATH
from comsol_support.edit_mph import edit_mph
from comsol_support.java_facade import JavaExecutionError, JavaNotFoundError
from comsol_support.mphgen import (
    ContractError,
    MphgenError,
    MphValidationError,
)


#: Shipped probe library: read-only query-contract classes
#: promoted from a large meshing campaign. `--query <ProbeName>` (no
#: path, no .java) resolves here. See docs/meshing.md §5.
PROBES_DIR = Path(__file__).resolve().parent / "java" / "probes"


def resolve_query_source(query: str) -> Path:
    """Resolve --query: an existing path wins; otherwise a bare probe
    name (`MeshStatsProbe` or `MeshStatsProbe.java`) resolves to the
    shipped library. Unknown names raise with the available roster."""
    p = Path(query)
    if p.exists():
        return p
    name = query if query.endswith(".java") else query + ".java"
    if "/" not in query and "\\" not in query:
        shipped = PROBES_DIR / name
        if shipped.is_file():
            return shipped
    available = sorted(x.stem for x in PROBES_DIR.glob("*.java"))
    raise FileNotFoundError(
        f"query source not found: {query!r} (not a file, and not one of "
        f"the shipped probes: {', '.join(available) or 'none'})"
    )


def cmd_query_mph(args: argparse.Namespace) -> int:
    query_args: dict[str, str] = {}
    for kv in (args.arg or []):
        if "=" not in kv:
            print(f"error: --arg expects key=value, got {kv!r}",
                  file=sys.stderr)
            return 2
        k, _, v = kv.partition("=")
        query_args[k] = v

    # In no-save mode --output only names the sidecars; default it next to
    # the input so the caller need not supply one.
    input_path = Path(args.input)
    output_path = (
        Path(args.output) if args.output
        else input_path.with_suffix(".query.mph")
    )

    mirror = sys.stderr if getattr(args, "stream", False) else None
    try:
        query_java = resolve_query_source(args.query)
    except FileNotFoundError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    try:
        result = edit_mph(
            input_mph=input_path,
            output_mph=output_path,
            query_java=query_java,
            mutator_args=query_args,
            solve_study=args.solve,
            no_save=True,
            license_timeout_s=args.license_timeout,
            comsol_path=args.comsol_path or COMSOL_PATH,
            workspace_dir=Path(args.workspace) if args.workspace else None,
            timeout_s=args.timeout,
            force=True,            # no file is written; never block on exists
            write_sidecar=not args.no_sidecar,
            run_linting=False,     # nothing saved to lint
            stream_mirror=mirror,
        )
    except ContractError as e:
        print(f"contract error: {e}", file=sys.stderr)
        return 3
    except MphValidationError as e:
        print(f"validation error: {e}", file=sys.stderr)
        return 4
    except (MphgenError, JavaNotFoundError, JavaExecutionError) as e:
        print(f"query-mph error: {e}", file=sys.stderr)
        return 1

    # The query payload is the headline output; print it as the primary
    # result, with a small provenance footer on stderr.
    print(json.dumps(result.query_result, indent=2))
    print(
        f"(query={result.query_class} solved={result.solved} "
        f"events={result.telemetry_event_count} "
        f"halt={result.telemetry_halt_reason})",
        file=sys.stderr,
    )
    return 0


def add_query_mph_subparser(subs: argparse._SubParsersAction) -> None:
    p = subs.add_parser(
        "query-mph",
        help="Load an existing .mph, run a read-only query class, print "
             "JSON, never save",
    )
    p.add_argument("--input", required=True,
                   help="Path to an existing .mph to load")
    p.add_argument("--query", required=True, metavar="JAVA_OR_PROBE",
                   help="Path to a .java exposing "
                        "Map<String,Object> query(Model, Map<String,String>), "
                        "OR the bare name of a shipped probe "
                        "(MeshStatsProbe, FeatureProblemProbe, "
                        "MeshSelectionProbe — see docs/meshing.md §5)")
    p.add_argument("--arg", action="append", default=[],
                   help="Key=value forwarded to query (repeatable)")
    p.add_argument("--solve", metavar="STUDY_TAG", default=None,
                   help="Run the named study before querying (e.g. to "
                        "inspect post-solve state). Still never saves.")
    p.add_argument("--output", "-o", default=None,
                   help="Optional path used only to name the telemetry/"
                        "provenance sidecars (no .mph is written). "
                        "Default: <input>.query.mph next to the input.")
    p.add_argument("--comsol-path",
                   help="COMSOL installation root (default: module constant)")
    p.add_argument("--workspace",
                   help="Build workspace for compiled .class files "
                        "(default: next to the query .java)")
    p.add_argument("--timeout", type=int, default=600,
                   help="JVM timeout in seconds (default: 600)")
    p.add_argument("--license-timeout", type=float, default=None,
                   help="Bound the COMSOL license checkout in seconds "
                        "(fails fast if no seat); falls back to "
                        "$COMSOL_LICENSE_TIMEOUT.")
    p.add_argument("--no-sidecar", action="store_true",
                   help="Skip writing the provenance sidecar")
    p.add_argument("--stream", action="store_true",
                   help="Mirror subprocess stdout/stderr live to stderr")
    p.set_defaults(func=cmd_query_mph)
