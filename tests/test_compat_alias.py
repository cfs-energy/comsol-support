"""1.0.0 rename contract: the pre-1.0 names keep working.

`comsol_agent` (import name) and `comsol-agent` (console script) must
resolve to the same code as `comsol_support` / `comsol-support` — same
module objects, not copies — so existing scripts, campaign workspaces,
`patch("comsol_agent.…")` targets and pre-1.0 MCP configs are
unaffected by the rename.
"""

import importlib
import json
import os
import subprocess
import sys
from importlib import metadata
from pathlib import Path
from unittest.mock import patch

import pytest

import comsol_agent
import comsol_support

ROOT = Path(__file__).resolve().parent.parent


def test_versions_agree():
    assert comsol_agent.__version__ == comsol_support.__version__
    assert comsol_agent.COMSOL_PATH == comsol_support.COMSOL_PATH
    assert metadata.version("comsol-support") == comsol_support.__version__


@pytest.mark.parametrize("name", [
    "db", "cli", "mphgen", "edit_mph", "query_mph", "java_facade",
    "telemetry", "linting", "dimensional", "mcp_server", "doctor",
    "gotcha_search", "config", "_source_checkout",
])
def test_submodules_are_the_same_objects(name):
    old = importlib.import_module(f"comsol_agent.{name}")
    new = importlib.import_module(f"comsol_support.{name}")
    assert old is new
    assert sys.modules[f"comsol_agent.{name}"] is new
    assert getattr(comsol_agent, name) is new


def test_from_import_and_attribute_access():
    from comsol_agent import db as old_db
    from comsol_support import db as new_db
    assert old_db is new_db
    assert comsol_agent.db.init_db is new_db.init_db


def test_patch_target_under_old_name_hits_the_real_module():
    from comsol_support import mphgen
    with patch("comsol_agent.mphgen.subprocess.Popen") as fake:
        assert mphgen.subprocess.Popen is fake


def test_both_console_scripts_are_registered():
    eps = {ep.name: ep.value for ep in metadata.entry_points(group="console_scripts")
           if ep.value.startswith("comsol_support.")}
    assert eps.get("comsol-support") == "comsol_support.cli:main"
    assert eps.get("comsol-agent") == "comsol_support.cli:main"


def test_old_mcp_script_path_still_serves():
    """Pre-1.0 --mcp-config files point at comsol_agent/mcp_server.py."""
    msg = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                      "params": {}}).encode()
    framed = b"Content-Length: %d\r\n\r\n%s" % (len(msg), msg)
    env = {**os.environ, "COMSOL_DB": str(ROOT / "data" / "comsol.db")}
    out = subprocess.run(
        [sys.executable, str(ROOT / "comsol_agent" / "mcp_server.py")],
        input=framed, capture_output=True, env=env, timeout=60,
    ).stdout
    assert b'"serverInfo"' in out
    assert b"comsol-support-tools" in out
