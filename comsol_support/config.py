"""Configuration for the COMSOL agent."""

import os
from dataclasses import dataclass, field
from pathlib import Path

from comsol_support import COMSOL_PATH

#: Knowledge base used when nothing overrides it: ``data/comsol.db``
#: inside the source checkout (comsol-support is source-checkout-only,
#: so the checkout is always known), NOT relative to the current
#: directory. Until 1.0 the default was cwd-relative, which made every
#: new working directory silently address a fresh, empty database.
DEFAULT_DB_RELPATH = Path("data/comsol.db")


def default_db_path() -> Path:
    """Resolve the knowledge-base path.

    Precedence: the ``COMSOL_DB`` environment variable (the same
    variable the MCP server speaks), else ``data/comsol.db`` under the
    source checkout (``_source_checkout.SOURCE_ROOT``) — the same file
    no matter which directory the command is run from.
    """
    env = os.environ.get("COMSOL_DB")
    if env:
        return Path(env)
    from comsol_support._source_checkout import SOURCE_ROOT
    return SOURCE_ROOT / DEFAULT_DB_RELPATH


@dataclass
class Config:
    """Tool configuration with sensible defaults (paths only — the LLM
    knobs of the retired build orchestrator were removed in 2026-08)."""

    comsol_path: str = COMSOL_PATH
    workspace_dir: Path = Path("workspace")
    db_path: Path = field(default_factory=default_db_path)

    @classmethod
    def from_yaml(cls, path: Path) -> "Config":
        """Load config from YAML file, falling back to defaults for missing keys."""
        if not path.exists():
            return cls()

        text = path.read_text(encoding="utf-8")
        # Support simple key: value YAML subset via line parsing
        data = {}
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if ":" in line:
                key, _, val = line.partition(":")
                val = val.strip().strip('"').strip("'")
                data[key.strip()] = val

        kwargs = {}
        if "comsol_path" in data:
            kwargs["comsol_path"] = data["comsol_path"]
        if "workspace_dir" in data:
            kwargs["workspace_dir"] = Path(data["workspace_dir"])
        if "db_path" in data:
            kwargs["db_path"] = Path(data["db_path"])

        return cls(**kwargs)
