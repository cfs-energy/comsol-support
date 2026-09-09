"""comsol-support: Agentic COMSOL Multiphysics interface."""

import os
import platform
import shutil
from pathlib import Path

__version__ = "1.0.0"

# Default COMSOL 6.4 install roots probed (in order) when the COMSOL_PATH
# environment variable is unset. The first existing directory wins; then
# the install that owns a `comsol` launcher found on PATH (any platform);
# when neither exists, the platform's first candidate is kept as the
# default so error messages still point at the conventional install
# location. v6.4 is the ceiling (COMSOL_VERSIONS.md) — only 6.4 roots here.
#: The installers (scripts/install.sh, scripts/install.ps1) and `doctor`
#: read COMSOL_PATH from this module, so this is the single place COMSOL
#: is discovered. Only vendor-default locations belong here — never a
#: site's own mount point (export COMSOL_PATH, or put `comsol` on PATH).
_DEFAULT_CANDIDATES = {
    "Linux": [
        "/usr/local/comsol64/multiphysics/",         # COMSOL default
        "/opt/comsol64/multiphysics/",
    ],
    "Windows": [
        r"C:\Program Files\COMSOL\COMSOL64\Multiphysics",
    ],
    "Darwin": [
        "/Applications/COMSOL64/Multiphysics",
    ],
}


def _root_from_launcher() -> str | None:
    """The install root that owns the `comsol` launcher on PATH, if any.

    Resolves symlinks (a `/usr/local/bin/comsol` link is common), then
    walks up to the nearest `bin` directory — the launcher lives at
    `<root>/bin/comsol` on Linux/macOS and `<root>/bin/win64/comsol.exe`
    on Windows — and accepts that root only if it carries `plugins/`,
    so an unrelated `comsol` script on PATH cannot masquerade as an
    install.
    """
    exe = shutil.which("comsol") or shutil.which("comsol.exe")
    if not exe:
        return None
    try:
        resolved = Path(exe).resolve()
    except OSError:
        return None
    for ancestor in resolved.parents:
        if ancestor.name == "bin":
            root = ancestor.parent
            if (root / "plugins").is_dir():
                return str(root)
            return None
    return None


def _default_comsol_path() -> str:
    candidates = _DEFAULT_CANDIDATES.get(
        platform.system(), _DEFAULT_CANDIDATES["Linux"]
    )
    for cand in candidates:
        if Path(cand).is_dir():
            return cand
    launcher_root = _root_from_launcher()
    if launcher_root:
        return launcher_root
    return candidates[0]


# COMSOL installation path. Overridable via the COMSOL_PATH environment
# variable so ad-hoc scripts and CI don't have to re-hardcode it; falls
# back to per-platform default probing (then the `comsol` launcher on
# PATH) when unset (or set but empty).
COMSOL_PATH = os.environ.get("COMSOL_PATH") or _default_comsol_path()
