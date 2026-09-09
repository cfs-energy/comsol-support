"""license-status — report COMSOL license-seat availability via a bounded
checkout probe.

The bundled `lmutil`/`lmstat` cannot query every FlexNet server (version
mismatch), so this instead *attempts* a base-license checkout under a
wall-clock bound: `ModelUtil.initStandalone` followed by
`ModelUtil.checkoutLicense("COMSOL")` — the explicit checkout matters,
because initStandalone succeeds with no free seat (the seat is only taken
at model load). If the checkout returns true a seat was available; false
is reported as `reason: no_seat`; if the probe blocks past the timeout
the process group is killed (clean release) and the seat is reported
unavailable. Model- and module-agnostic.
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

PROBE_SOURCE = "LicenseProbe.java"


def license_status(
    *,
    comsol_path: str = COMSOL_PATH,
    workspace_dir: Path | None = None,
    timeout_s: float = 30.0,
) -> dict:
    """Run the bounded checkout probe and return a status dict.

    Returns a dict with at least {"available": bool}; on a successful
    checkout also {"checkout_ms": int}; when every seat is in use
    {"reason": "no_seat", "error": str}; on timeout {"reason": "timeout",
    "timeout_s": float}; on a probe error {"error": str}.
    """
    workspace_dir = Path(workspace_dir).resolve() if workspace_dir \
        else Path.cwd()
    workspace_dir.mkdir(parents=True, exist_ok=True)
    facade = JavaFacade(
        comsol_path=comsol_path,
        workspace_dir=workspace_dir,
        java_source_dir=Path(__file__).parent / "java",
    )
    probe_src = facade.java_source_dir / PROBE_SOURCE

    try:
        rc, out = facade.run_class(
            probe_src,
            stream=False,
            log_path=workspace_dir / "license_probe.log",
            timeout=timeout_s,
        )
    except JavaExecutionError:
        # Timed out — the checkout blocked, i.e. no seat was free. The
        # process group was already terminated by run_class.
        return {"available": False, "reason": "timeout",
                "timeout_s": timeout_s}

    # Parse the probe's JSON line (last JSON object on stdout).
    for line in reversed(out.splitlines()):
        line = line.strip()
        if line.startswith("{") and line.endswith("}"):
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                continue
    return {"available": rc == 0, "reason": "no_probe_output",
            "returncode": rc}


def cmd_license_status(args: argparse.Namespace) -> int:
    try:
        status = license_status(
            comsol_path=args.comsol_path or COMSOL_PATH,
            workspace_dir=Path(args.workspace) if args.workspace else None,
            timeout_s=args.timeout,
        )
    except JavaCompileError as e:
        print(f"compile error: {e}", file=sys.stderr)
        return 3
    except JavaNotFoundError as e:
        print(f"license-status error: {e}", file=sys.stderr)
        return 1

    print(json.dumps(status, indent=2))
    return 0 if status.get("available") else 2


def add_license_status_subparser(subs: argparse._SubParsersAction) -> None:
    p = subs.add_parser(
        "license-status",
        help="Report COMSOL license-seat availability via a bounded "
             "checkout probe (no lmutil dependency)",
    )
    p.add_argument("--comsol-path",
                   help="COMSOL installation root (default: module constant)")
    p.add_argument("--workspace",
                   help="Workspace for the compiled probe + log "
                        "(default: cwd)")
    p.add_argument("--timeout", type=float, default=30.0,
                   help="Seconds to wait for the checkout before declaring "
                        "the seat unavailable (default: 30)")
    p.set_defaults(func=cmd_license_status)
