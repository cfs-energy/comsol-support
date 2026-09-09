"""CLI tests — the parser surface and the ``search`` verb.

The other verbs (mphgen, edit-mph, query-mph, run-harness, check,
gotcha-search, ingest-telemetry, license-status, scrape, slot-stats,
promote-catalog) are exercised by their own test modules; this file pins
the parser as a whole (13 verbs) and the knowledge-base ``search`` verb, which since
2026-08 is the CLI twin of the MCP ``search_api`` tool (before that it
searched the retired orchestrator's never-written build catalog).
"""

import argparse
from unittest.mock import MagicMock, patch

import pytest

from comsol_support.cli import build_parser, cmd_search, main
from comsol_support.db import init_db, store_knowledge_row

# The complete verb surface after the orchestrator removal. Keep in sync
# with README.md / MANIFEST.md.
EXPECTED_VERBS = {
    "search", "scrape", "slot-stats", "mphgen", "edit-mph", "run-harness",
    "query-mph", "license-status", "check", "gotcha-search",
    "ingest-telemetry", "promote-catalog", "doctor",
}
RETIRED_VERBS = {"build", "resume", "status", "list"}


# ── Fixtures ────────────────────────────────────────────────────────────────

@pytest.fixture
def parser():
    return build_parser()


@pytest.fixture
def kb_path(tmp_path):
    """A knowledge base with three rows across two stages."""
    p = tmp_path / "kb.db"
    conn = init_db(p)
    store_knowledge_row(conn, "MeshFeature", method="create",
                        signature="String tag, String type", stage="mesh",
                        module="mesh",
                        description="Create a Swept mesh feature")
    store_knowledge_row(conn, "MeshFeature", source="refmanual",
                        property_key="sourceface", stage="mesh",
                        description="Swept source face selection")
    store_knowledge_row(conn, "PhysicsFeature", method="set",
                        signature="String prop, Object val", stage="physics",
                        description="Set a physics property")
    conn.close()
    return p


@pytest.fixture
def empty_kb_path(tmp_path):
    p = tmp_path / "empty.db"
    init_db(p).close()
    return p


def _search_args(db, query, stage=None, limit=10):
    return argparse.Namespace(query=query, stage=stage, limit=limit, db=str(db))


# ── Parser surface ─────────────────────────────────────────────────────────

class TestParserSurface:

    def _verbs(self, parser):
        subs = next(a for a in parser._actions
                    if isinstance(a, argparse._SubParsersAction))
        return set(subs.choices)

    def test_exactly_the_kept_verbs(self, parser):
        assert self._verbs(parser) == EXPECTED_VERBS

    def test_retired_verbs_are_gone(self, parser):
        assert not self._verbs(parser) & RETIRED_VERBS

    @pytest.mark.parametrize("verb", sorted(RETIRED_VERBS))
    def test_retired_verb_is_a_parse_error(self, parser, verb):
        with pytest.raises(SystemExit):
            parser.parse_args([verb])

    def test_search_parses_query(self, parser):
        args = parser.parse_args(["search", "heat transfer"])
        assert args.command == "search"
        assert args.query == "heat transfer"
        assert args.stage is None

    def test_search_parses_stage_and_limit(self, parser):
        args = parser.parse_args(["search", "Swept", "--stage", "mesh", "-n", "3"])
        assert args.stage == "mesh"
        assert args.limit == 3

    def test_search_default_limit(self, parser):
        assert parser.parse_args(["search", "q"]).limit == 10

    def test_version_flag_prints_package_version(self, parser, capsys):
        from comsol_support import __version__
        with pytest.raises(SystemExit) as exc:
            parser.parse_args(["--version"])
        assert exc.value.code == 0
        assert capsys.readouterr().out.strip() == f"comsol-support {__version__}"

    def test_verbose_flag(self, parser):
        assert parser.parse_args(["-v", "search", "q"]).verbose is True

    def test_main_dispatches_every_verb(self):
        """Every parser verb has a handler in main()'s dispatch table and
        nothing retired lingers there."""
        import inspect
        from comsol_support import cli
        src = inspect.getsource(cli.main)
        for verb in EXPECTED_VERBS:
            assert f'"{verb}":' in src, verb
        for verb in RETIRED_VERBS:
            assert f'"{verb}":' not in src, verb

    def test_main_without_command_prints_help(self, capsys):
        with patch("sys.argv", ["comsol-support"]), pytest.raises(SystemExit) as e:
            main()
        assert e.value.code == 0
        assert "search" in capsys.readouterr().out


# ── search verb ────────────────────────────────────────────────────────────

class TestCmdSearch:

    def test_finds_knowledge_rows(self, kb_path, capsys):
        rc = cmd_search(_search_args(kb_path, "Swept"))
        out = capsys.readouterr().out
        assert rc == 0
        assert "MeshFeature.create(String tag, String type)" in out
        assert "[prop: sourceface]" in out
        assert "stage=mesh" in out
        assert "PhysicsFeature" not in out

    def test_stage_filter(self, kb_path, capsys):
        rc = cmd_search(_search_args(kb_path, "set", stage="physics"))
        out = capsys.readouterr().out
        assert rc == 0
        assert "PhysicsFeature.set" in out
        assert "MeshFeature" not in out

    def test_limit(self, kb_path, capsys):
        cmd_search(_search_args(kb_path, "Swept", limit=1))
        out = capsys.readouterr().out.strip()
        assert out.count("| stage=") == 1

    def test_no_match_reports_index_size(self, kb_path, capsys):
        rc = cmd_search(_search_args(kb_path, "nonexistent_xyz", stage="mesh"))
        out = capsys.readouterr().out
        assert rc == 0
        assert "No API entries matching 'nonexistent_xyz' (stage=mesh)" in out
        assert "3 entries indexed" in out

    def test_empty_kb_points_to_scrapers(self, empty_kb_path, capsys):
        """The campaign gap 'search has no API-knowledge backend' was an
        empty knowledge table — say so and name the verbs that fill it."""
        rc = cmd_search(_search_args(empty_kb_path, "Swept"))
        out = capsys.readouterr().out
        assert rc == 0
        assert "knowledge base" in out and "is empty" in out
        assert "scrape javadoc" in out and "scrape refmanual" in out

    def test_bad_fts_query_returns_1(self, kb_path, capsys):
        rc = cmd_search(_search_args(kb_path, 'sourceface AND "'))
        assert rc == 1
        assert "Search error" in capsys.readouterr().out

    def test_matches_mcp_search_api_rendering(self, kb_path, capsys):
        """CLI and MCP answer identically for the same query."""
        from comsol_support.mcp_server import handle_tool_call
        conn = init_db(kb_path)
        mcp = handle_tool_call(conn, "search_api", {"query": "Swept", "limit": 10})
        conn.close()
        cmd_search(_search_args(kb_path, "Swept"))
        assert capsys.readouterr().out.strip() == mcp.strip()

    def test_comsol_db_env_var_selects_default_db(self, kb_path, capsys, monkeypatch):
        """Without --db, the CLI reads COMSOL_DB (as the MCP server does) so a
        campaign workspace can point at one populated knowledge base instead
        of auto-creating an empty cwd-relative data/comsol.db."""
        monkeypatch.setenv("COMSOL_DB", str(kb_path))
        rc = cmd_search(argparse.Namespace(query="Swept", stage=None, limit=10, db=None))
        assert rc == 0
        assert "MeshFeature" in capsys.readouterr().out

    def test_db_override_wins_over_config(self, kb_path, capsys):
        with patch("comsol_support.cli.Config") as MockConfig:
            MockConfig.from_yaml.return_value = MagicMock(db_path="/nonexistent")
            rc = cmd_search(_search_args(kb_path, "Swept"))
        assert rc == 0
        assert "MeshFeature" in capsys.readouterr().out
