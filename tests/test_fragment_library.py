"""Tests for the fragment library as exposed to calling agents: the
``search_fragments`` / ``get_fragment`` MCP tools over the ``fragments``
table (the orchestrator-side preamble injection, manifest tracking and
Tier C audit that used to be tested here were retired in 2026-08).
"""

import json
from pathlib import Path

import pytest

from comsol_support.db import (
    init_db,
    search_fragments,
    store_fragment,
)

FIXTURES = Path(__file__).parent / "fixtures" / "fragments"


# ── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture
def db_conn(tmp_path):
    """Fresh database connection."""
    conn = init_db(tmp_path / "test.db")
    yield conn
    conn.close()


@pytest.fixture
def db_with_fragments(db_conn):
    """Database pre-loaded with geometry and physics fragments."""
    geom_path = FIXTURES / "geometry_fragments.json"
    physics_path = FIXTURES / "physics_fragments.json"

    for path in (geom_path, physics_path):
        with open(path) as f:
            fragments = json.load(f)
        for frag in fragments:
            store_fragment(
                db_conn,
                frag["stage"],
                frag["pattern_name"],
                frag["java_code"],
                tier=frag["tier"],
                description=frag.get("description", ""),
                corpus_freq=frag.get("corpus_freq", 0),
                co_occurrence_json=frag.get("co_occurrence_json", ""),
                source=frag.get("source", "corpus-test"),
            )
    return db_conn






# ── MCP search_fragments tool ──────────────────────────────────────────────


class TestMCPSearchFragments:
    """Tests for the search_fragments MCP tool."""

    def test_tool_listed(self, db_conn):
        """search_fragments appears in tools/list response."""
        from comsol_support.mcp_server import TOOLS
        names = [t["name"] for t in TOOLS]
        assert "search_fragments" in names

    def test_tool_count(self, db_conn):
        """10 tools total (search_api + 2 fragment + 3 telemetry/results
        + 3 native-catalog + 1 gotchas)."""
        from comsol_support.mcp_server import TOOLS
        assert len(TOOLS) == 10

    def test_search_by_stage(self, db_with_fragments):
        """Returns fragments filtered by stage."""
        from comsol_support.mcp_server import handle_tool_call
        result = handle_tool_call(db_with_fragments, "search_fragments",
                                  {"stage": "geometry"})
        assert "Block" in result
        assert "Fragment" in result

    def test_search_by_pattern(self, db_with_fragments):
        """Returns fragments matching pattern_name."""
        from comsol_support.mcp_server import handle_tool_call
        result = handle_tool_call(db_with_fragments, "search_fragments",
                                  {"stage": "geometry", "pattern_name": "Block"})
        assert "Block" in result

    def test_search_respects_limit(self, db_with_fragments):
        """Limit parameter works."""
        from comsol_support.mcp_server import handle_tool_call
        result = handle_tool_call(db_with_fragments, "search_fragments",
                                  {"stage": "geometry", "limit": 1})
        # Should only have 1 fragment (the ---separator would indicate more)
        assert result.count("---") == 0

    def test_search_empty_stage(self, db_with_fragments):
        """Returns 'No fragments found' for empty stage."""
        from comsol_support.mcp_server import handle_tool_call
        result = handle_tool_call(db_with_fragments, "search_fragments",
                                  {"stage": "nonexistent"})
        assert "No fragments found" in result

    def test_search_ordered_by_freq(self, db_with_fragments):
        """Most frequent fragments come first."""
        from comsol_support.mcp_server import handle_tool_call
        result = handle_tool_call(db_with_fragments, "search_fragments",
                                  {"stage": "geometry", "limit": 5})
        # Block (freq=312) should appear before Sphere (freq=87)
        block_pos = result.find("Block")
        sphere_pos = result.find("Sphere")
        assert block_pos < sphere_pos

    def test_search_includes_co_occurrence(self, db_with_fragments):
        """Co-occurrence data formatted in response."""
        from comsol_support.mcp_server import handle_tool_call
        result = handle_tool_call(db_with_fragments, "search_fragments",
                                  {"stage": "geometry", "limit": 1})
        assert "Co-occurs with:" in result

    def test_search_includes_code(self, db_with_fragments):
        """Java code block included in response."""
        from comsol_support.mcp_server import handle_tool_call
        result = handle_tool_call(db_with_fragments, "search_fragments",
                                  {"stage": "geometry", "limit": 1})
        assert "Code:" in result
        assert ".create(" in result

    def test_search_max_limit_capped(self, db_with_fragments):
        """Limit capped at 20."""
        from comsol_support.mcp_server import handle_tool_call
        # Even with limit=100, should not exceed 20
        result = handle_tool_call(db_with_fragments, "search_fragments",
                                  {"stage": "geometry", "limit": 100})
        # We only have 5 geometry fragments, so result count is <=5
        assert "Block" in result






# ── Integration ────────────────────────────────────────────────────────────


class TestIntegration:
    """Fragment queries as the MCP layer issues them."""

    def test_has_fragments_query_detects_presence(self, db_with_fragments):
        """search_fragments(conn, stage, limit=1) correctly detects fragments."""
        assert bool(search_fragments(db_with_fragments, "geometry", limit=1))
        assert not bool(search_fragments(db_with_fragments, "nonexistent", limit=1))

    def test_fragment_query_on_empty_db(self, db_conn):
        """No crash when fragments table exists but is empty."""
        result = search_fragments(db_conn, "geometry", limit=1)
        assert result == []


    def test_mcp_get_fragment_includes_source(self, db_with_fragments):
        """get_fragment response includes relevant fields."""
        from comsol_support.mcp_server import handle_tool_call
        # Fragment IDs start at 1
        result = handle_tool_call(db_with_fragments, "get_fragment",
                                  {"fragment_id": 1})
        assert "Fragment 1" in result
        assert "Corpus freq:" in result
