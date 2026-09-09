#!/usr/bin/env python3
"""Standalone preflight — runs from a bare checkout, before any install.

`comsol-support doctor` needs the package installed; this shim does not.
It puts the repo root on sys.path and calls the same implementation, so
there is exactly one set of checks across Linux, macOS, and Windows.

    python3 scripts/doctor.py [--json] [--strict] [--comsol-path ...]
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

if sys.version_info < (3, 10):
    sys.exit(
        f"comsol-support needs Python 3.10+; this is "
        f"{sys.version_info.major}.{sys.version_info.minor}. "
        "Install uv (https://docs.astral.sh/uv/) and re-run the installer, "
        "which provisions its own Python."
    )

from comsol_support.doctor import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
