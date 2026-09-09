"""Tests for the native-interface catalog.

Covers:
- db.py schema creation (native_interfaces + domain_synonyms)
- native_catalog: upsert, lookup_domain, list_interfaces, list_studies_for_physics,
  list_plots_for_physics, seed_default_catalog idempotency, bump_corpus_freq
- mcp_server: the three catalog tools, dispatch + empty-match messaging
  (the retrieval side of the native-first ladders in docs/modeling-practice.md)
"""

from __future__ import annotations


import pytest

from comsol_support.db import init_db
from comsol_support.native_catalog import (
    bump_corpus_freq,
    get_interface,
    list_interfaces,
    list_plots_for_physics,
    list_studies_for_physics,
    lookup_domain,
    lookup_domains,
    seed_default_catalog,
    update_catalog_corpus_freq,
    upsert_interface,
    upsert_synonym,
)


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------

@pytest.fixture
def db_conn(tmp_path):
    conn = init_db(tmp_path / "catalog.db")
    yield conn
    conn.close()


# ---------------------------------------------------------------------------
# Schema + seed
# ---------------------------------------------------------------------------

def test_schema_creates_tables(db_conn):
    tables = {
        r["name"] for r in db_conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    assert "native_interfaces" in tables
    assert "domain_synonyms" in tables


def test_seed_populates_expected_domains(db_conn):
    rows = db_conn.execute(
        "SELECT DISTINCT domain_keyword FROM native_interfaces "
        "WHERE stage = 'physics'"
    ).fetchall()
    domains = {r["domain_keyword"] for r in rows}
    # Every domain the validator + preamble rely on must be present.
    for expected in (
        "heat", "flow", "stress", "em_low", "em_high",
        "acoustics", "transport", "chem", "custom",
    ):
        assert expected in domains


def test_seed_is_idempotent(db_conn):
    # Second call must not duplicate rows nor overwrite corpus_freq bumps.
    bump_corpus_freq(
        db_conn, domain_keyword="heat", stage="physics", tag_prefix="ht",
        delta=50,
    )
    db_conn.commit()
    before = db_conn.execute(
        "SELECT corpus_freq FROM native_interfaces "
        "WHERE domain_keyword='heat' AND stage='physics' AND tag_prefix='ht'"
    ).fetchone()["corpus_freq"]
    seed_default_catalog(db_conn)
    after = db_conn.execute(
        "SELECT corpus_freq FROM native_interfaces "
        "WHERE domain_keyword='heat' AND stage='physics' AND tag_prefix='ht'"
    ).fetchone()["corpus_freq"]
    assert after == before  # bump preserved across re-seed


# ---------------------------------------------------------------------------
# Catalog queries
# ---------------------------------------------------------------------------

def test_lookup_domain_resolves_synonyms(db_conn):
    assert lookup_domain(db_conn, "thermal") == "heat"
    assert lookup_domain(db_conn, "Temperature") == "heat"  # case-insensitive
    assert lookup_domain(db_conn, "pipe") == "flow"
    assert lookup_domain(db_conn, "antenna") == "em_high"
    assert lookup_domain(db_conn, "xyz_unknown") is None
    assert lookup_domain(db_conn, "") is None


def test_lookup_domains_dedupes_and_preserves_order(db_conn):
    doms = lookup_domains(db_conn, ["thermal", "temperature", "stress"])
    # "thermal" and "temperature" both map to "heat" — dedup.
    assert doms == ["heat", "stress"]


def test_list_interfaces_ranks_native_before_pde(db_conn):
    rows = list_interfaces(
        db_conn, domain_keyword="heat", stage="physics", limit=10,
    )
    ranks = [r["setup_cost_rank"] for r in rows]
    assert ranks == sorted(ranks)
    # ht must come before the generic PDE in the heat domain.
    tags = [r["tag_prefix"] for r in rows]
    assert tags.index("ht") < tags.index("pde")


def test_list_interfaces_unknown_domain_returns_empty(db_conn):
    rows = list_interfaces(
        db_conn, domain_keyword="quantum_xyz", stage="physics",
    )
    assert rows == []


def test_list_studies_for_physics_follows_physics_defaults(db_conn):
    rows = list_studies_for_physics(db_conn, physics_tag="ht")
    tags = [r["tag_prefix"] for r in rows]
    # ht's default_studies = ["stat", "time"]
    assert set(tags) == {"stat", "time"}


def test_list_studies_for_physics_unknown_falls_back(db_conn):
    # Unknown physics tag: fall back to corpus-ranked study catalog.
    rows = list_studies_for_physics(db_conn, physics_tag="xyz_unknown")
    assert len(rows) > 0
    # 'stat' should still be the rank-1 study in the fallback ordering.
    assert rows[0]["tag_prefix"] == "stat"


def test_list_plots_for_physics_follows_physics_defaults(db_conn):
    rows = list_plots_for_physics(db_conn, physics_tag="ht")
    tags = {r["tag_prefix"] for r in rows}
    # ht's default_plots = ["surf", "line", "probe_dom"]
    assert {"surf", "line", "probe_dom"}.issubset(tags)


def test_upsert_interface_refreshes_metadata_preserves_corpus_state(db_conn):
    """Upserting an existing PK refreshes curated metadata (class_name,
    notes) but preserves state carried by the promote job
    (corpus_freq, setup_cost_rank). This lets seed re-runs propagate
    curated updates while not clobbering promote-bumped values."""
    before = get_interface(db_conn, tag_prefix="ht", stage="physics")
    assert before["class_name"] == "HeatTransfer"
    # Simulate the promote-catalog job having bumped corpus_freq.
    bump_corpus_freq(
        db_conn, domain_keyword="heat", stage="physics", tag_prefix="ht",
        delta=999,
    )
    db_conn.commit()
    bumped_freq = before["corpus_freq"] + 999
    # Now re-upsert with new curated metadata.
    upsert_interface(
        db_conn, domain_keyword="heat", stage="physics", tag_prefix="ht",
        class_name="HeatTransferUpdated", setup_cost_rank=42,
        corpus_freq=0, notes="refreshed",
    )
    db_conn.commit()
    after = get_interface(db_conn, tag_prefix="ht", stage="physics")
    # Metadata refreshed:
    assert after["class_name"] == "HeatTransferUpdated"
    assert after["notes"] == "refreshed"
    # State preserved:
    assert after["setup_cost_rank"] == before["setup_cost_rank"]
    assert after["corpus_freq"] == bumped_freq


def test_upsert_synonym_normalizes_case(db_conn):
    upsert_synonym(db_conn, intent_keyword="  MixedCase  ",
                   domain_keyword="heat")
    db_conn.commit()
    assert lookup_domain(db_conn, "mixedcase") == "heat"


# ---------------------------------------------------------------------------
# D1 — corpus-derived corpus_freq updates
# ---------------------------------------------------------------------------

def _synthetic_stats(**kw):
    """Duck-typed CorpusStatistics stand-in for unit tests."""
    from types import SimpleNamespace
    defaults = {
        "physics_freq": {},
        "study_freq": {},
        "result_freq": {},
        "total_models": 0,
        "parsed": 0,
        "failed": 0,
    }
    defaults.update(kw)
    return SimpleNamespace(**defaults)


def test_update_corpus_freq_overwrites_matched_rows(db_conn):
    stats = _synthetic_stats(
        physics_freq={"HeatTransfer": 77, "SolidMechanics": 123},
        study_freq={"Stationary": 50},
        result_freq={"Surface": 88},
    )
    summary = update_catalog_corpus_freq(db_conn, stats)
    assert summary["matched"] == 4
    assert summary["unmatched"] == []
    assert get_interface(
        db_conn, tag_prefix="ht", stage="physics",
    )["corpus_freq"] == 77
    assert get_interface(
        db_conn, tag_prefix="solid", stage="physics",
    )["corpus_freq"] == 123
    assert get_interface(
        db_conn, tag_prefix="stat", stage="study",
    )["corpus_freq"] == 50
    assert get_interface(
        db_conn, tag_prefix="surf", stage="result",
    )["corpus_freq"] == 88


def test_update_corpus_freq_leaves_unseen_rows_alone(db_conn):
    before = get_interface(
        db_conn, tag_prefix="spf", stage="physics",
    )["corpus_freq"]
    stats = _synthetic_stats(physics_freq={"HeatTransfer": 10})
    update_catalog_corpus_freq(db_conn, stats)
    after = get_interface(
        db_conn, tag_prefix="spf", stage="physics",
    )["corpus_freq"]
    assert after == before  # untouched


def test_update_corpus_freq_logs_unmatched_class_names(db_conn, caplog):
    stats = _synthetic_stats(
        physics_freq={"MadeUpInterfaceV99": 17},
    )
    with caplog.at_level("INFO", logger="comsol_support.native_catalog"):
        summary = update_catalog_corpus_freq(db_conn, stats)
    assert summary["matched"] == 0
    assert ("physics", "MadeUpInterfaceV99", 17) in summary["unmatched"]
    # Row was NOT auto-inserted.
    r = db_conn.execute(
        "SELECT 1 FROM native_interfaces WHERE class_name = ?",
        ("MadeUpInterfaceV99",),
    ).fetchone()
    assert r is None
    # Logged at INFO level.
    assert any(
        "MadeUpInterfaceV99" in rec.message for rec in caplog.records
    )


def test_update_corpus_freq_idempotent(db_conn):
    stats = _synthetic_stats(physics_freq={"HeatTransfer": 42})
    update_catalog_corpus_freq(db_conn, stats)
    first = get_interface(
        db_conn, tag_prefix="ht", stage="physics",
    )["corpus_freq"]
    update_catalog_corpus_freq(db_conn, stats)
    second = get_interface(
        db_conn, tag_prefix="ht", stage="physics",
    )["corpus_freq"]
    assert first == 42
    assert second == 42


def test_update_corpus_freq_empty_stats_is_noop(db_conn):
    before = get_interface(
        db_conn, tag_prefix="ht", stage="physics",
    )["corpus_freq"]
    summary = update_catalog_corpus_freq(db_conn, _synthetic_stats())
    assert summary == {"matched": 0, "unmatched": []}
    after = get_interface(
        db_conn, tag_prefix="ht", stage="physics",
    )["corpus_freq"]
    assert after == before


def test_update_corpus_freq_preserves_setup_cost_rank(db_conn):
    """Only corpus_freq is touched — rank stays at the seeded value
    even when the new freq is very different."""
    rank_before = get_interface(
        db_conn, tag_prefix="pde", stage="physics",
    )["setup_cost_rank"]
    update_catalog_corpus_freq(
        db_conn, _synthetic_stats(physics_freq={"GeneralFormPDE": 9999}),
    )
    rank_after = get_interface(
        db_conn, tag_prefix="pde", stage="physics",
    )["setup_cost_rank"]
    assert rank_after == rank_before


def test_update_corpus_freq_does_not_clobber_curated_metadata(db_conn):
    """class_name / notes / default_studies etc. are untouched by a
    corpus-freq update — promote-catalog is strictly about freq."""
    before = get_interface(db_conn, tag_prefix="ht", stage="physics")
    update_catalog_corpus_freq(
        db_conn, _synthetic_stats(physics_freq={"HeatTransfer": 1}),
    )
    after = get_interface(db_conn, tag_prefix="ht", stage="physics")
    assert after["class_name"] == before["class_name"]
    assert after["notes"] == before["notes"]
    assert after["default_studies"] == before["default_studies"]


# --- End-to-end: real corpus_miner → update ------------------------------

def _write_toy_corpus(root):
    """Three toy .java files covering physics/study/result extraction."""
    (root / "m1.java").write_text(
        'public class M1 {\n'
        '  void run(Model m) {\n'
        '    m.physics().create("ht", "HeatTransfer");\n'
        '    m.study("std1").create("stat", "Stationary");\n'
        '    m.result().create("pg1", "PlotGroup3D");\n'
        '    m.result("pg1").feature().create("s1", "Surface");\n'
        '  }\n}\n'
    )
    (root / "m2.java").write_text(
        'public class M2 {\n'
        '  void run(Model m) {\n'
        '    m.physics().create("ht", "HeatTransfer");\n'
        '    m.study("std1").create("time", "TimeDependent");\n'
        '    m.result().create("pg1", "PlotGroup3D");\n'
        '    m.result("pg1").feature().create("s1", "Surface");\n'
        '    m.result("pg1").feature().create("a1", "Arrow");\n'
        '  }\n}\n'
    )
    (root / "m3.java").write_text(
        'public class M3 {\n'
        '  void run(Model m) {\n'
        '    m.physics().create("spf", "LaminarFlow");\n'
        '    m.study("std1").create("stat", "Stationary");\n'
        '    m.result().create("pg1", "PlotGroup3D");\n'
        '    m.result("pg1").feature().create("st1", "Streamline");\n'
        '  }\n}\n'
    )


def test_analyze_corpus_then_update_end_to_end(db_conn, tmp_path):
    from comsol_support.corpus_miner import analyze_corpus
    corpus_dir = tmp_path / "corpus"
    corpus_dir.mkdir()
    _write_toy_corpus(corpus_dir)
    stats = analyze_corpus(corpus_dir)
    # The toy corpus gives known counts:
    assert stats.physics_freq == {"HeatTransfer": 2, "LaminarFlow": 1}
    assert stats.study_freq == {"Stationary": 2, "TimeDependent": 1}
    assert stats.result_freq["Surface"] == 2
    assert stats.result_freq["Arrow"] == 1
    assert stats.result_freq["Streamline"] == 1
    summary = update_catalog_corpus_freq(db_conn, stats)
    # HeatTransfer, LaminarFlow, Stationary, TimeDependent, Surface,
    # Arrow, Streamline = 7 matched catalog rows.
    # (PlotGroup3D is not in the catalog — it's a container, not a
    # plot feature type — and lands in unmatched.)
    assert summary["matched"] >= 7
    assert any(
        cls == "PlotGroup3D" for _s, cls, _c in summary["unmatched"]
    )
    # Spot-check the post-update values.
    assert get_interface(
        db_conn, tag_prefix="ht", stage="physics",
    )["corpus_freq"] == 2
    assert get_interface(
        db_conn, tag_prefix="surf", stage="result",
    )["corpus_freq"] == 2


def test_result_types_extracted_from_postprocessing_only(tmp_path):
    """Class names appearing outside the postprocessing block must not
    be counted as result types."""
    from comsol_support.corpus_miner import parse_java_file
    src = tmp_path / "scoped.java"
    src.write_text(
        'public class Scoped {\n'
        '  void run(Model m) {\n'
        # Surface appearing in geom stage (not postprocessing) — must
        # not become a result_type.
        '    m.component("c1").geom("g1").create("s1", "Surface");\n'
        '    m.study("std1").create("stat", "Stationary");\n'
        '  }\n}\n'
    )
    a = parse_java_file(src)
    # No postprocessing block → no result_types extracted.
    assert a.result_types == []


# ---------------------------------------------------------------------------
# promote-catalog CLI
# ---------------------------------------------------------------------------

def test_promote_catalog_cli_updates_freq(tmp_path, capsys):
    from comsol_support.cli import cmd_promote_catalog
    corpus_dir = tmp_path / "corpus"
    corpus_dir.mkdir()
    _write_toy_corpus(corpus_dir)
    db_path = tmp_path / "promote.db"
    # Seed the DB first via init_db (indirectly via cmd_promote_catalog,
    # which calls init_db() internally).
    from unittest.mock import MagicMock
    ns = MagicMock()
    ns.java_dir = str(corpus_dir)
    ns.db = str(db_path)
    rc = cmd_promote_catalog(ns)
    assert rc == 0
    out = capsys.readouterr().out
    assert "analyzed 3 models" in out
    assert "updated" in out
    # Verify persistence.
    from comsol_support.db import init_db
    conn = init_db(db_path)
    try:
        assert get_interface(
            conn, tag_prefix="ht", stage="physics",
        )["corpus_freq"] == 2
    finally:
        conn.close()


def test_promote_catalog_cli_missing_dir(tmp_path, capsys):
    from comsol_support.cli import cmd_promote_catalog
    from unittest.mock import MagicMock
    ns = MagicMock()
    ns.java_dir = str(tmp_path / "nope")
    ns.db = str(tmp_path / "x.db")
    rc = cmd_promote_catalog(ns)
    assert rc == 2
    assert "does not exist" in capsys.readouterr().err


def test_promote_catalog_cli_empty_corpus(tmp_path, capsys):
    from comsol_support.cli import cmd_promote_catalog
    from unittest.mock import MagicMock
    empty = tmp_path / "empty"
    empty.mkdir()
    ns = MagicMock()
    ns.java_dir = str(empty)
    ns.db = str(tmp_path / "x.db")
    rc = cmd_promote_catalog(ns)
    assert rc == 2
    assert "no .java files" in capsys.readouterr().err


def test_promote_catalog_cli_in_parser():
    from comsol_support.cli import build_parser
    parser = build_parser()
    ns = parser.parse_args([
        "promote-catalog", "--java-dir", "/tmp/x", "--db", "/tmp/t.db",
    ])
    assert ns.command == "promote-catalog"
    assert ns.java_dir == "/tmp/x"
    assert ns.db == "/tmp/t.db"


# ---------------------------------------------------------------------------
# MCP tools
# ---------------------------------------------------------------------------

def test_mcp_tool_count_updated():
    from comsol_support.mcp_server import TOOLS
    names = {t["name"] for t in TOOLS}
    assert "list_physics_options" in names
    assert "list_studies_for_physics" in names
    assert "list_default_plots_for_physics" in names


def test_mcp_list_physics_options_ranked(db_conn):
    from comsol_support.mcp_server import handle_tool_call
    # limit=20 so GeneralFormPDE (rank 9) is included for the ordering
    # assertion; the real MCP callers default to 5 which excludes it.
    out = handle_tool_call(
        db_conn, "list_physics_options", {"domain": "heat", "limit": 20},
    )
    assert "ht (HeatTransfer)" in out
    assert "rank=1" in out
    # The rank-1 native must appear before the generic PDE in the output.
    assert out.index("ht (HeatTransfer)") < out.index("GeneralFormPDE")


def test_mcp_list_physics_options_accepts_synonym(db_conn):
    from comsol_support.mcp_server import handle_tool_call
    out = handle_tool_call(
        db_conn, "list_physics_options",
        {"domain": "thermal"},  # synonym → heat
    )
    assert "HeatTransfer" in out


def test_mcp_list_physics_options_empty_domain(db_conn):
    from comsol_support.mcp_server import handle_tool_call
    out = handle_tool_call(
        db_conn, "list_physics_options", {"domain": "quantum_unknown"},
    )
    assert "No native physics interfaces" in out


def test_mcp_list_physics_options_requires_domain(db_conn):
    from comsol_support.mcp_server import handle_tool_call
    out = handle_tool_call(db_conn, "list_physics_options", {})
    assert "Provide a domain keyword" in out


def test_mcp_list_studies_for_physics_ht(db_conn):
    from comsol_support.mcp_server import handle_tool_call
    out = handle_tool_call(
        db_conn, "list_studies_for_physics", {"physics_tag": "ht"},
    )
    assert "Stationary" in out
    assert "TimeDependent" in out


def test_mcp_list_default_plots_for_physics_ht(db_conn):
    from comsol_support.mcp_server import handle_tool_call
    out = handle_tool_call(
        db_conn, "list_default_plots_for_physics", {"physics_tag": "ht"},
    )
    # Real COMSOL class name (matches what .result().create(tag, class)
    # emits in the corpus Java). tag_prefix is "surf".
    assert "Surface" in out
