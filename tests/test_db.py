"""Tests for comsol_support.db — SQLite schema, CRUD, and the orchestrator self-heal."""

import sqlite3

import pytest

from comsol_support.db import (
    ORCHESTRATOR_OBJECTS,
    _drop_orchestrator_objects,
    get_fragment,
    init_db,
    search_fragments,
    search_knowledge,
    store_fragment,
    store_knowledge_row,
    store_telemetry_events,
    get_telemetry_events,
)


@pytest.fixture
def db_conn(tmp_path):
    """Create a fresh database for each test."""
    db_path = tmp_path / "test_comsol.db"
    conn = init_db(db_path)
    yield conn
    conn.close()


class TestInitDb:
    """Verify schema creation."""

    KEEP_TABLES = {
        "knowledge", "fragments", "telemetry_events", "slot_expected_units",
        "variable_declared_units", "native_interfaces", "domain_synonyms",
    }

    def test_creates_all_tables(self, db_conn):
        """init_db creates exactly the seven deterministic-tool tables."""
        tables = {row[0] for row in db_conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%' AND name NOT LIKE '%_fts%'"
        ).fetchall()}
        assert tables == self.KEEP_TABLES

    def test_creates_fts_tables(self, db_conn):
        """init_db creates FTS5 virtual tables for knowledge and telemetry only."""
        tables = {row[0] for row in db_conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
        assert "knowledge_fts" in tables
        assert "telemetry_fts" in tables
        assert "builds_fts" not in tables

    def test_no_orchestrator_objects_on_fresh_db(self, db_conn):
        """A fresh file never carries builds / checkpoints / builds_fts."""
        names = {row[0] for row in db_conn.execute(
            "SELECT name FROM sqlite_master"
        ).fetchall()}
        assert not names & {name for _k, name in ORCHESTRATOR_OBJECTS}

    def test_idempotent(self, tmp_path):
        """Calling init_db twice doesn't error."""
        db_path = tmp_path / "test_idem.db"
        conn1 = init_db(db_path)
        conn1.close()
        conn2 = init_db(db_path)
        conn2.close()

    def test_row_factory(self, db_conn):
        """Connection uses sqlite3.Row factory."""
        assert db_conn.row_factory is sqlite3.Row


class TestKnowledge:
    """Knowledge table CRUD and FTS."""

    def test_store_and_search(self, db_conn):
        """Store a knowledge row and find it via FTS."""
        store_knowledge_row(
            db_conn, "GeomFeature",
            method="create",
            signature="create(String tag, String type)",
            stage="geometry",
            source="javadoc",
            description="Creates a geometry feature",
        )
        results = search_knowledge(db_conn, "GeomFeature")
        assert len(results) >= 1
        assert results[0]["class"] == "GeomFeature"

    def test_search_by_stage(self, db_conn):
        """Stage-filtered search returns only matching stage."""
        store_knowledge_row(db_conn, "HeatTransfer", stage="physics", source="javadoc")
        store_knowledge_row(db_conn, "Block", stage="geometry", source="javadoc")

        physics = search_knowledge(db_conn, "HeatTransfer OR Block", stage="physics")
        # Should only find HeatTransfer
        classes = [r["class"] for r in physics]
        assert "HeatTransfer" in classes


class TestFragments:
    """Fragment CRUD."""

    def test_store_and_get(self, db_conn):
        """Round-trip store/get for a fragment."""
        fid = store_fragment(
            db_conn, "geometry", "create_block",
            'model.component("comp1").geom("geom1").create("blk1", "Block");',
            description="Create a block geometry primitive",
            corpus_freq=42,
        )
        frag = get_fragment(db_conn, fid)
        assert frag is not None
        assert frag["pattern_name"] == "create_block"
        assert frag["corpus_freq"] == 42

    def test_search_by_stage(self, db_conn):
        """search_fragments filters by stage."""
        store_fragment(db_conn, "geometry", "create_block", "java code 1")
        store_fragment(db_conn, "physics", "add_heat", "java code 2")

        results = search_fragments(db_conn, "geometry")
        assert all(r["stage"] == "geometry" for r in results)

    def test_search_by_pattern(self, db_conn):
        """search_fragments filters by pattern name."""
        store_fragment(db_conn, "geometry", "create_block", "code1")
        store_fragment(db_conn, "geometry", "create_cylinder", "code2")

        results = search_fragments(db_conn, "geometry", pattern_name="cylinder")
        assert len(results) == 1
        assert results[0]["pattern_name"] == "create_cylinder"


# ---------------------------------------------------------------------------
# Self-heal of files created before the 2026-08 orchestrator removal
# ---------------------------------------------------------------------------

LEGACY_ORCHESTRATOR_DDL = """
    CREATE TABLE builds (
        id TEXT PRIMARY KEY, parent_id TEXT, depth INTEGER DEFAULT 0,
        created_at TEXT NOT NULL, intent TEXT NOT NULL, current_stage TEXT,
        summary_xml TEXT, outcome TEXT, status TEXT DEFAULT 'active',
        physics_modules TEXT, feature_classes TEXT, module_tags TEXT,
        keywords TEXT, session_data TEXT, record_type TEXT DEFAULT 'build'
    );
    CREATE INDEX idx_builds_status ON builds(status);
    CREATE TABLE checkpoints (
        id INTEGER PRIMARY KEY AUTOINCREMENT, build_id TEXT NOT NULL,
        stage TEXT NOT NULL, tier TEXT, status TEXT NOT NULL,
        artifact_path TEXT, error_text TEXT,
        timestamp TEXT NOT NULL DEFAULT (datetime('now')),
        FOREIGN KEY (build_id) REFERENCES builds(id)
    );
    CREATE INDEX idx_checkpoints_build ON checkpoints(build_id);
    CREATE INDEX idx_checkpoints_stage ON checkpoints(build_id, stage);
    CREATE VIRTUAL TABLE builds_fts USING fts5(
        id UNINDEXED, intent, summary_xml, keywords, content='builds', content_rowid='rowid'
    );
    CREATE TRIGGER builds_ai AFTER INSERT ON builds BEGIN
        INSERT INTO builds_fts(rowid, id, intent, summary_xml, keywords)
        VALUES (new.rowid, new.id, new.intent, new.summary_xml, new.keywords);
    END;
    CREATE TRIGGER builds_ad AFTER DELETE ON builds BEGIN
        INSERT INTO builds_fts(builds_fts, rowid, id, intent, summary_xml, keywords)
        VALUES ('delete', old.rowid, old.id, old.intent, old.summary_xml, old.keywords);
    END;
    CREATE TRIGGER builds_au AFTER UPDATE ON builds BEGIN
        INSERT INTO builds_fts(builds_fts, rowid, id, intent, summary_xml, keywords)
        VALUES ('delete', old.rowid, old.id, old.intent, old.summary_xml, old.keywords);
        INSERT INTO builds_fts(rowid, id, intent, summary_xml, keywords)
        VALUES (new.rowid, new.id, new.intent, new.summary_xml, new.keywords);
    END;
"""


def _object_names(conn):
    return {row[0] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"
    ).fetchall()}


@pytest.fixture
def legacy_db(tmp_path):
    """A DB file exactly as the pre-2026-08 schema left it: kept tables with
    a row each, plus all nine orchestrator objects (empty, as on every
    deployment on record)."""
    path = tmp_path / "legacy.db"
    conn = init_db(path)
    store_knowledge_row(conn, "GeomSequence", method="create",
                        stage="geometry", description="kept row")
    store_telemetry_events(conn, build_id="run-1", output_path=None, events=[
        {"event_type": "halt", "wall_ms": 1, "payload": {"halt_reason": "success"}},
    ])
    conn.commit()
    conn.executescript(LEGACY_ORCHESTRATOR_DDL)
    assert {n for _k, n in ORCHESTRATOR_OBJECTS} <= _object_names(conn)
    conn.close()
    return path


class TestOrchestratorSelfHeal:
    """init_db drops the retired build-orchestrator objects on open."""

    def test_drops_all_nine_objects(self, legacy_db):
        conn = init_db(legacy_db)
        names = _object_names(conn)
        assert not names & {n for _k, n in ORCHESTRATOR_OBJECTS}
        conn.close()

    def test_kept_data_survives(self, legacy_db):
        conn = init_db(legacy_db)
        assert search_knowledge(conn, "GeomSequence")[0]["description"] == "kept row"
        assert len(get_telemetry_events(conn, "run-1")) == 1
        assert TestInitDb.KEEP_TABLES <= _object_names(conn)
        conn.close()

    def test_reports_what_it_dropped_then_noop(self, legacy_db):
        raw = sqlite3.connect(str(legacy_db))
        dropped = _drop_orchestrator_objects(raw)
        assert set(dropped) == {n for _k, n in ORCHESTRATOR_OBJECTS}
        assert _drop_orchestrator_objects(raw) == []
        raw.close()

    def test_partial_leftovers_are_handled(self, tmp_path):
        """A file where only some objects remain (e.g. an earlier manual
        cleanup) heals too — every DROP is IF EXISTS."""
        path = tmp_path / "partial.db"
        conn = init_db(path)
        conn.executescript(
            "CREATE TABLE builds (id TEXT PRIMARY KEY, intent TEXT);"
            "CREATE INDEX idx_builds_status ON builds(intent);"
        )
        conn.close()
        conn = init_db(path)
        assert "builds" not in _object_names(conn)
        conn.close()

    def test_healed_file_reopens_cleanly(self, legacy_db):
        for _ in range(3):
            conn = init_db(legacy_db)
            conn.close()
        raw = sqlite3.connect(str(legacy_db))
        assert raw.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        raw.close()

