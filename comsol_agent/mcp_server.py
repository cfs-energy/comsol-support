#!/usr/bin/env python3
"""Compatibility entry point: pre-1.0 MCP configs launched
``comsol_agent/mcp_server.py``. Delegates to ``comsol_support.mcp_server``."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from comsol_support.mcp_server import main  # noqa: E402

if __name__ == "__main__":
    main()
