"""Compatibility alias: ``comsol_agent`` is the pre-1.0 name of ``comsol_support``.

Every ``comsol_support.<module>`` is registered in ``sys.modules`` under
``comsol_agent.<module>`` as the *same* module object, so
``import comsol_agent.db``, ``from comsol_agent import mphgen`` and
``patch("comsol_agent.mphgen.subprocess.Popen")`` all keep working
against the one real module (no second copy, no custom import finder —
importlib clobbers ``__spec__`` on aliased modules, so a finder is the
wrong tool here). New code should import ``comsol_support``; this shim
may be removed in a future major version.
"""

from __future__ import annotations

import importlib
import pkgutil
import sys

import comsol_support as _real

COMSOL_PATH = _real.COMSOL_PATH
__version__ = _real.__version__
__path__: list[str] = []  # no submodules of our own; everything is aliased below


def _alias_all() -> None:
    for info in pkgutil.iter_modules(_real.__path__):
        name = info.name
        if name.startswith("_") and name != "_source_checkout":
            continue
        module = importlib.import_module(f"{_real.__name__}.{name}")
        sys.modules[f"{__name__}.{name}"] = module
        globals()[name] = module


_alias_all()
