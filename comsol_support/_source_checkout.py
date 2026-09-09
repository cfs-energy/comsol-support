"""comsol-support is a source-checkout project — detect when it is not one.

Several entry points need files that live in the repository but not in
the importable package: the Java sources under ``comsol_support/java/``
(compiled at run time against the COMSOL classpath), the probe library
under ``comsol_support/java/probes/``, and ``docs/known-gotchas.md`` (the
catalog behind ``gotcha-search`` and the MCP ``search_gotchas`` tool).
A wheel built from ``pyproject.toml`` carries none of them, so an
installed-from-wheel ``comsol-support`` fails in confusing ways (a bare
``javac`` usage error from ``license-status``, "gotchas doc not found
at .../site-packages/docs" from ``gotcha-search``).

This module makes that failure explicit and early. ``doctor`` reports
it as a blocking problem; ``JavaFacade`` and ``gotcha-search`` raise
``SourceCheckoutRequired`` with the same message instead of failing
downstream. Supported installation is an editable install of a source
checkout (``uv sync`` / ``pip install -e .``) — see INSTALL.md.
"""

from __future__ import annotations

from pathlib import Path

#: Package directory (``comsol_support/``) and the repository root above it.
PACKAGE_DIR = Path(__file__).resolve().parent
SOURCE_ROOT = PACKAGE_DIR.parent

#: Repository-relative paths every full install must have. Kept short and
#: representative: one file per asset family, so a partial copy is
#: reported rather than half-working.
REQUIRED_ASSETS: tuple[str, ...] = (
    "comsol_support/java/ModelExporter.java",
    "comsol_support/java/TagRegistry.java",
    "comsol_support/java/probes/MeshStatsProbe.java",
    "docs/known-gotchas.md",
)

INSTALL_HINT = (
    "comsol-support must run from a source checkout (git clone + `uv sync`, "
    "or `pip install -e .`); wheels do not carry the Java sources, probes, "
    "or docs/known-gotchas.md. See INSTALL.md."
)


class SourceCheckoutRequired(RuntimeError):
    """Raised when a needed repository asset is absent from the install."""


def missing_source_assets(root: Path | None = None) -> list[str]:
    """Return the REQUIRED_ASSETS (repo-relative) that are absent under root."""
    base = Path(root) if root is not None else SOURCE_ROOT
    return [rel for rel in REQUIRED_ASSETS if not (base / rel).is_file()]


def is_source_checkout(root: Path | None = None) -> bool:
    return not missing_source_assets(root)


def require_source_checkout(feature: str, root: Path | None = None) -> None:
    """Raise SourceCheckoutRequired naming `feature` if assets are missing."""
    missing = missing_source_assets(root)
    if missing:
        base = Path(root) if root is not None else SOURCE_ROOT
        raise SourceCheckoutRequired(
            f"{feature} needs repository files that are not installed "
            f"(missing under {base}: {', '.join(missing)}). {INSTALL_HINT}"
        )
