"""SQLite persistence for the deterministic tools: knowledge base, fragments, telemetry, catalogs.

Adopted from auto-compact/db.py — same idioms: sqlite3.Row, FTS5 with sync
triggers, idempotent schema creation, safe column migration.
"""

import json
import sqlite3
from pathlib import Path
from typing import Iterable


def init_db(db_path: Path) -> sqlite3.Connection:
    """Initialize the database, creating all tables if needed.

    Table families (all deterministic-tool data):
      - knowledge (+ knowledge_fts): Javadoc ontology + Reference Manual data
      - fragments: corpus-mined composable code fragments
      - telemetry_events (+ telemetry_fts): solver/run events keyed by a
        caller-chosen run id
      - slot_expected_units, variable_declared_units: unit-slot catalogs
      - native_interfaces, domain_synonyms: native-interface catalog

    Files created before 2026-08 may also carry the retired build
    orchestrator's ``builds`` / ``checkpoints`` objects; every open drops
    them (``_drop_orchestrator_objects``). They never held rows on any
    known deployment and no kept table references them.
    """
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")

    conn.executescript("""
        -- Knowledge table: Javadoc ontology + Reference Manual properties
        CREATE TABLE IF NOT EXISTS knowledge (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            class           TEXT NOT NULL,
            method          TEXT,
            signature       TEXT,
            property_key    TEXT,
            value_type      TEXT,
            stage           TEXT,
            module          TEXT,
            source          TEXT,
            description     TEXT,
            created_at      TEXT NOT NULL DEFAULT (datetime('now'))
        );

        CREATE INDEX IF NOT EXISTS idx_knowledge_class ON knowledge(class);
        CREATE INDEX IF NOT EXISTS idx_knowledge_stage ON knowledge(stage);
        CREATE INDEX IF NOT EXISTS idx_knowledge_property ON knowledge(property_key);

        -- Fragments table: corpus-mined composable Java code blocks
        CREATE TABLE IF NOT EXISTS fragments (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            stage           TEXT NOT NULL,
            tier            TEXT NOT NULL DEFAULT 'A',
            pattern_name    TEXT NOT NULL,
            java_code       TEXT NOT NULL,
            description     TEXT,
            corpus_freq     INTEGER DEFAULT 0,
            co_occurrence_json TEXT,
            embedding_blob  BLOB,
            created_at      TEXT NOT NULL DEFAULT (datetime('now'))
        );

        CREATE INDEX IF NOT EXISTS idx_fragments_stage ON fragments(stage);
        CREATE INDEX IF NOT EXISTS idx_fragments_pattern ON fragments(pattern_name);

        -- Telemetry events: model-agnostic solver/run events emitted by
        -- the Java facade (SolverTelemetry / ModelExporter). One row
        -- per event. build_id is a free-text run key chosen by the caller
        -- (no foreign key); it may be NULL when a telemetry stream is
        -- ingested outside any run context (e.g. standalone mphgen runs).
        CREATE TABLE IF NOT EXISTS telemetry_events (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            build_id        TEXT,
            output_path     TEXT,
            event_type      TEXT NOT NULL,
            wall_ms         INTEGER,
            payload_json    TEXT,
            created_at      TEXT NOT NULL DEFAULT (datetime('now'))
        );

        CREATE INDEX IF NOT EXISTS idx_telemetry_build
            ON telemetry_events(build_id);
        CREATE INDEX IF NOT EXISTS idx_telemetry_type
            ON telemetry_events(event_type);
        CREATE INDEX IF NOT EXISTS idx_telemetry_build_type
            ON telemetry_events(build_id, event_type);

        -- Slot expected-unit catalog (Phase 2 of the slot catalog pipeline).
        -- One row per (five-axis key, comsol_version).
        CREATE TABLE IF NOT EXISTS slot_expected_units (
            physics_type    TEXT NOT NULL,
            sdim            TEXT NOT NULL,
            feature_type    TEXT NOT NULL,
            feature_scope   TEXT NOT NULL,
            slot_property   TEXT NOT NULL,
            comsol_version  TEXT NOT NULL,

            n_attempts      INTEGER NOT NULL,
            n_resolved      INTEGER NOT NULL,
            coverage_frac   REAL NOT NULL,

            modal_unit      TEXT,
            modal_count     INTEGER,
            modal_frac      REAL,

            distribution    TEXT NOT NULL,
            confidence      TEXT NOT NULL,

            type_source     TEXT NOT NULL,

            created_at      TEXT NOT NULL DEFAULT (datetime('now')),

            PRIMARY KEY (physics_type, sdim, feature_type, feature_scope,
                         slot_property, comsol_version)
        );

        CREATE INDEX IF NOT EXISTS idx_slot_expected_lookup
            ON slot_expected_units(physics_type, feature_type);
        CREATE INDEX IF NOT EXISTS idx_slot_expected_confidence
            ON slot_expected_units(confidence);

        -- Source-B variable unit declarations (Phase 2, indirect role).
        -- Feeds Layer C's symbol resolver rather than the slot catalog.
        CREATE TABLE IF NOT EXISTS variable_declared_units (
            var_name        TEXT NOT NULL,
            physics_type    TEXT NOT NULL,
            sdim            TEXT NOT NULL,
            declaration_key TEXT NOT NULL,
            declared_unit   TEXT NOT NULL,
            n_occurrences   INTEGER NOT NULL,
            comsol_version  TEXT NOT NULL,

            PRIMARY KEY (var_name, physics_type, sdim, declaration_key,
                         declared_unit, comsol_version)
        );

        CREATE INDEX IF NOT EXISTS idx_var_declared_units_lookup
            ON variable_declared_units(var_name, physics_type);

        -- Native-interface catalog (Phase-1 of the native-preference plan).
        -- Flat table; rendered nested by the preamble assembler.
        -- Seeded hand-curated for common domains; corpus_freq is extended
        -- by the offline catalog-promotion job over time.
        CREATE TABLE IF NOT EXISTS native_interfaces (
            domain_keyword   TEXT NOT NULL,   -- "heat", "flow", "stress", ...
            stage            TEXT NOT NULL,   -- "physics" | "study" | "result"
            tag_prefix       TEXT NOT NULL,   -- "ht", "spf", "stat", "surf", ...
            class_name       TEXT NOT NULL,   -- "HeatTransfer", "Stationary", ...
            setup_cost_rank  INTEGER NOT NULL,-- 1 = lowest-cost native option
            corpus_freq      INTEGER NOT NULL DEFAULT 0,
            default_studies  TEXT,            -- JSON array of study tag_prefixes
            default_plots    TEXT,            -- JSON array of plot tag_prefixes
            auto_features    TEXT,            -- JSON array of auto-created tags
            notes            TEXT,            -- one-line prose for preamble rendering
            created_at       TEXT NOT NULL DEFAULT (datetime('now')),
            PRIMARY KEY (domain_keyword, stage, tag_prefix)
        );

        CREATE INDEX IF NOT EXISTS idx_ni_domain_stage
            ON native_interfaces(domain_keyword, stage);
        CREATE INDEX IF NOT EXISTS idx_ni_stage_rank
            ON native_interfaces(stage, setup_cost_rank);

        -- Small intent-keyword → canonical domain map. Hand-seeded.
        -- Unmatched intent keywords resolve to no catalog candidates, in
        -- which case the preamble injects nothing for that stage.
        CREATE TABLE IF NOT EXISTS domain_synonyms (
            intent_keyword   TEXT PRIMARY KEY,
            domain_keyword   TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_dom_synonyms_domain
            ON domain_synonyms(domain_keyword);
    """)

    # Migrate: add source column to fragments if missing
    try:
        conn.execute("ALTER TABLE fragments ADD COLUMN source TEXT")
    except sqlite3.OperationalError:
        pass  # Column already exists

    # Self-heal: drop the retired build-orchestrator objects if this file
    # predates their removal. Idempotent.
    _drop_orchestrator_objects(conn)

    # FTS5 over knowledge for retrieval
    _ensure_knowledge_fts(conn)

    # FTS5 over telemetry payloads for cross-build event search
    _ensure_telemetry_fts(conn)

    # Seed the native-interface catalog on first init. Idempotent: the seed
    # uses INSERT OR IGNORE so re-running never overwrites customizations
    # or bumped corpus_freq values from the promotion job.
    from comsol_support.native_catalog import seed_default_catalog
    seed_default_catalog(conn)

    return conn


# Objects created by the retired LLM build orchestrator (L5), in drop
# order: triggers first (they reference builds_fts), then the FTS table,
# then the FK child (checkpoints) before its parent (builds).
ORCHESTRATOR_OBJECTS: tuple[tuple[str, str], ...] = (
    ("TRIGGER", "builds_ai"),
    ("TRIGGER", "builds_ad"),
    ("TRIGGER", "builds_au"),
    ("TABLE", "builds_fts"),
    ("INDEX", "idx_checkpoints_stage"),
    ("INDEX", "idx_checkpoints_build"),
    ("TABLE", "checkpoints"),
    ("INDEX", "idx_builds_status"),
    ("TABLE", "builds"),
)


def _drop_orchestrator_objects(conn: sqlite3.Connection) -> list[str]:
    """Drop any leftover build-orchestrator objects; return what was dropped.

    Safe on every open: ``DROP … IF EXISTS`` is a no-op on fresh files, and
    on old files the objects are empty (0 rows anywhere on record) and
    unreferenced by any kept table. Runs in one transaction so a partial
    failure leaves the file as it was.
    """
    present = {
        row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE name IN (%s)"
            % ",".join("?" * len(ORCHESTRATOR_OBJECTS)),
            [name for _kind, name in ORCHESTRATOR_OBJECTS],
        ).fetchall()
    }
    if not present:
        return []
    script = "BEGIN;\n" + "".join(
        f"DROP {kind} IF EXISTS {name};\n" for kind, name in ORCHESTRATOR_OBJECTS
    ) + "COMMIT;\n"
    conn.executescript(script)
    return [name for _kind, name in ORCHESTRATOR_OBJECTS if name in present]


def _ensure_knowledge_fts(conn: sqlite3.Connection) -> None:
    """Create FTS5 over knowledge table for API retrieval queries."""
    try:
        conn.execute("SELECT class FROM knowledge_fts LIMIT 0")
        return
    except sqlite3.OperationalError:
        pass

    conn.executescript("""
        DROP TRIGGER IF EXISTS knowledge_ai;
        DROP TRIGGER IF EXISTS knowledge_ad;
        DROP TRIGGER IF EXISTS knowledge_au;
        DROP TABLE IF EXISTS knowledge_fts;

        CREATE VIRTUAL TABLE knowledge_fts USING fts5(
            class, method, signature, property_key, description, stage, module,
            content=knowledge, content_rowid=rowid
        );

        INSERT INTO knowledge_fts(rowid, class, method, signature,
            property_key, description, stage, module)
        SELECT rowid, class, COALESCE(method, ''), COALESCE(signature, ''),
               COALESCE(property_key, ''), COALESCE(description, ''),
               COALESCE(stage, ''), COALESCE(module, '')
        FROM knowledge;

        CREATE TRIGGER knowledge_ai AFTER INSERT ON knowledge BEGIN
            INSERT INTO knowledge_fts(rowid, class, method, signature,
                property_key, description, stage, module)
            VALUES (new.rowid, new.class, COALESCE(new.method, ''),
                    COALESCE(new.signature, ''), COALESCE(new.property_key, ''),
                    COALESCE(new.description, ''), COALESCE(new.stage, ''),
                    COALESCE(new.module, ''));
        END;

        CREATE TRIGGER knowledge_ad AFTER DELETE ON knowledge BEGIN
            INSERT INTO knowledge_fts(knowledge_fts, rowid, class, method,
                signature, property_key, description, stage, module)
            VALUES ('delete', old.rowid, old.class, COALESCE(old.method, ''),
                    COALESCE(old.signature, ''), COALESCE(old.property_key, ''),
                    COALESCE(old.description, ''), COALESCE(old.stage, ''),
                    COALESCE(old.module, ''));
        END;

        CREATE TRIGGER knowledge_au AFTER UPDATE ON knowledge BEGIN
            INSERT INTO knowledge_fts(knowledge_fts, rowid, class, method,
                signature, property_key, description, stage, module)
            VALUES ('delete', old.rowid, old.class, COALESCE(old.method, ''),
                    COALESCE(old.signature, ''), COALESCE(old.property_key, ''),
                    COALESCE(old.description, ''), COALESCE(old.stage, ''),
                    COALESCE(old.module, ''));
            INSERT INTO knowledge_fts(rowid, class, method, signature,
                property_key, description, stage, module)
            VALUES (new.rowid, new.class, COALESCE(new.method, ''),
                    COALESCE(new.signature, ''), COALESCE(new.property_key, ''),
                    COALESCE(new.description, ''), COALESCE(new.stage, ''),
                    COALESCE(new.module, ''));
        END;
    """)



# ── Session data persistence ────────────────────────────────────────────────



# ── Knowledge CRUD ──────────────────────────────────────────────────────────

def store_knowledge_row(
    conn: sqlite3.Connection,
    cls: str,
    *,
    method: str | None = None,
    signature: str | None = None,
    property_key: str | None = None,
    value_type: str | None = None,
    stage: str | None = None,
    module: str | None = None,
    source: str | None = None,
    description: str | None = None,
) -> int:
    """Store a knowledge row. Returns the row id."""
    cursor = conn.execute(
        "INSERT INTO knowledge (class, method, signature, property_key, "
        "value_type, stage, module, source, description) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (cls, method, signature, property_key, value_type, stage, module,
         source, description),
    )
    conn.commit()
    return cursor.lastrowid


def clear_knowledge_by_source(conn: sqlite3.Connection, source: str) -> int:
    """Delete all knowledge rows matching a source tag. Returns count deleted."""
    cursor = conn.execute("DELETE FROM knowledge WHERE source = ?", (source,))
    conn.commit()
    return cursor.rowcount


def store_knowledge_batch(
    conn: sqlite3.Connection,
    rows: list[tuple],
) -> int:
    """Batch-insert knowledge rows. Each tuple:
    (class, method, signature, property_key, value_type, stage, module, source, description).
    Returns count inserted.
    """
    conn.executemany(
        "INSERT INTO knowledge (class, method, signature, property_key, "
        "value_type, stage, module, source, description) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        rows,
    )
    conn.commit()
    return len(rows)


def search_knowledge(
    conn: sqlite3.Connection,
    query: str,
    *,
    stage: str | None = None,
    limit: int = 10,
) -> list[dict]:
    """Search knowledge using FTS5, optionally filtered by stage."""
    if stage:
        rows = conn.execute(
            "SELECT k.* FROM knowledge_fts f "
            "JOIN knowledge k ON k.rowid = f.rowid "
            "WHERE knowledge_fts MATCH ? AND k.stage = ? "
            "ORDER BY rank LIMIT ?",
            (query, stage, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT k.* FROM knowledge_fts f "
            "JOIN knowledge k ON k.rowid = f.rowid "
            "WHERE knowledge_fts MATCH ? "
            "ORDER BY rank LIMIT ?",
            (query, limit),
        ).fetchall()
    return [dict(r) for r in rows]


# ── Fragment CRUD ───────────────────────────────────────────────────────────

def store_fragment(
    conn: sqlite3.Connection,
    stage: str,
    pattern_name: str,
    java_code: str,
    *,
    tier: str = "A",
    description: str | None = None,
    corpus_freq: int = 0,
    co_occurrence_json: str | None = None,
    embedding_blob: bytes | None = None,
    source: str | None = None,
) -> int:
    """Store a code fragment. Returns the fragment id."""
    cursor = conn.execute(
        "INSERT INTO fragments (stage, tier, pattern_name, java_code, description, "
        "corpus_freq, co_occurrence_json, embedding_blob, source) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (stage, tier, pattern_name, java_code, description, corpus_freq,
         co_occurrence_json, embedding_blob, source),
    )
    conn.commit()
    return cursor.lastrowid


def get_fragment(conn: sqlite3.Connection, fragment_id: int) -> dict | None:
    """Get a fragment by ID."""
    row = conn.execute(
        "SELECT * FROM fragments WHERE id = ?", (fragment_id,)
    ).fetchone()
    return dict(row) if row else None


def search_fragments(
    conn: sqlite3.Connection,
    stage: str,
    *,
    pattern_name: str | None = None,
    limit: int = 10,
) -> list[dict]:
    """Search fragments by stage and optional pattern name."""
    if pattern_name:
        rows = conn.execute(
            "SELECT * FROM fragments WHERE stage = ? AND pattern_name LIKE ? "
            "ORDER BY corpus_freq DESC LIMIT ?",
            (stage, f"%{pattern_name}%", limit),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM fragments WHERE stage = ? "
            "ORDER BY corpus_freq DESC LIMIT ?",
            (stage, limit),
        ).fetchall()
    return [dict(r) for r in rows]


def clear_fragments_by_source(conn: sqlite3.Connection, source: str) -> int:
    """Delete all fragments matching a source tag. Returns count deleted."""
    cursor = conn.execute("DELETE FROM fragments WHERE source = ?", (source,))
    conn.commit()
    return cursor.rowcount


# ── Telemetry CRUD ──────────────────────────────────────────────────────────

def _ensure_telemetry_fts(conn: sqlite3.Connection) -> None:
    """Create FTS5 over telemetry_events.payload_json for cross-build search."""
    try:
        conn.execute("SELECT event_type FROM telemetry_fts LIMIT 0")
        return
    except sqlite3.OperationalError:
        pass

    conn.executescript("""
        DROP TRIGGER IF EXISTS telemetry_ai;
        DROP TRIGGER IF EXISTS telemetry_ad;
        DROP TRIGGER IF EXISTS telemetry_au;
        DROP TABLE IF EXISTS telemetry_fts;

        CREATE VIRTUAL TABLE telemetry_fts USING fts5(
            event_type, payload_json, build_id, output_path,
            content=telemetry_events, content_rowid=rowid
        );

        INSERT INTO telemetry_fts(rowid, event_type, payload_json,
            build_id, output_path)
        SELECT rowid, event_type, COALESCE(payload_json, ''),
               COALESCE(build_id, ''), COALESCE(output_path, '')
        FROM telemetry_events;

        CREATE TRIGGER telemetry_ai AFTER INSERT ON telemetry_events BEGIN
            INSERT INTO telemetry_fts(rowid, event_type, payload_json,
                build_id, output_path)
            VALUES (new.rowid, new.event_type,
                    COALESCE(new.payload_json, ''),
                    COALESCE(new.build_id, ''),
                    COALESCE(new.output_path, ''));
        END;

        CREATE TRIGGER telemetry_ad AFTER DELETE ON telemetry_events BEGIN
            INSERT INTO telemetry_fts(telemetry_fts, rowid, event_type,
                payload_json, build_id, output_path)
            VALUES ('delete', old.rowid, old.event_type,
                    COALESCE(old.payload_json, ''),
                    COALESCE(old.build_id, ''),
                    COALESCE(old.output_path, ''));
        END;

        CREATE TRIGGER telemetry_au AFTER UPDATE ON telemetry_events BEGIN
            INSERT INTO telemetry_fts(telemetry_fts, rowid, event_type,
                payload_json, build_id, output_path)
            VALUES ('delete', old.rowid, old.event_type,
                    COALESCE(old.payload_json, ''),
                    COALESCE(old.build_id, ''),
                    COALESCE(old.output_path, ''));
            INSERT INTO telemetry_fts(rowid, event_type, payload_json,
                build_id, output_path)
            VALUES (new.rowid, new.event_type,
                    COALESCE(new.payload_json, ''),
                    COALESCE(new.build_id, ''),
                    COALESCE(new.output_path, ''));
        END;
    """)


def store_telemetry_events(
    conn: sqlite3.Connection,
    *,
    build_id: str | None,
    output_path: str | None,
    events: list[dict],
) -> int:
    """Batch-insert telemetry events. Returns count inserted.

    Each event is an emitted JSON object with keys event_type, wall_ms,
    and payload. Missing fields are tolerated — event_type defaults to
    "unknown", wall_ms to NULL, payload serialized as "{}".
    """
    rows = []
    for ev in events:
        if not isinstance(ev, dict):
            continue
        event_type = str(ev.get("event_type") or "unknown")
        wall_ms = ev.get("wall_ms")
        try:
            wall_ms = int(wall_ms) if wall_ms is not None else None
        except (TypeError, ValueError):
            wall_ms = None
        payload = ev.get("payload")
        if payload is None:
            payload_json = "{}"
        elif isinstance(payload, str):
            payload_json = payload
        else:
            payload_json = json.dumps(payload, separators=(",", ":"))
        rows.append((build_id, output_path, event_type, wall_ms, payload_json))

    if not rows:
        return 0

    conn.executemany(
        "INSERT INTO telemetry_events "
        "(build_id, output_path, event_type, wall_ms, payload_json) "
        "VALUES (?, ?, ?, ?, ?)",
        rows,
    )
    conn.commit()
    return len(rows)


def get_telemetry_events(
    conn: sqlite3.Connection,
    build_id: str,
    *,
    event_type: str | None = None,
    event_types: Iterable[str] | None = None,
    since_id: int | None = None,
    limit: int = 1000,
) -> list[dict]:
    """Return telemetry events for a build, optionally filtered.

    Args:
      build_id: required build id filter.
      event_type: optional single event_type filter (kept for back-compat
        with the existing MCP tool).
      event_types: optional iterable of event types. If set, overrides
        event_type. Used by result views that aggregate multiple types
        (e.g. result_probe + result_global + result_error).
      since_id: optional exclusive lower bound on row id; returns only
        events strictly newer than this. Lets polling callers tail an
        active build by remembering the last id they saw.
      limit: maximum rows.

    Ordered by ascending id (= emission order).
    """
    clauses = ["build_id = ?"]
    params: list = [build_id]
    if event_types is not None:
        types = [str(t) for t in event_types]
        if types:
            placeholders = ",".join(["?"] * len(types))
            clauses.append(f"event_type IN ({placeholders})")
            params.extend(types)
    elif event_type:
        clauses.append("event_type = ?")
        params.append(event_type)
    if since_id is not None:
        clauses.append("id > ?")
        params.append(int(since_id))
    sql = (
        "SELECT * FROM telemetry_events WHERE "
        + " AND ".join(clauses)
        + " ORDER BY id LIMIT ?"
    )
    params.append(int(limit))
    rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def search_telemetry(
    conn: sqlite3.Connection,
    query: str,
    *,
    limit: int = 20,
) -> list[dict]:
    """FTS search over telemetry payloads. Matches event_type and payload."""
    rows = conn.execute(
        "SELECT t.* FROM telemetry_fts f "
        "JOIN telemetry_events t ON t.rowid = f.rowid "
        "WHERE telemetry_fts MATCH ? "
        "ORDER BY rank LIMIT ?",
        (query, limit),
    ).fetchall()
    return [dict(r) for r in rows]


def clear_telemetry_for_build(
    conn: sqlite3.Connection, build_id: str,
) -> int:
    """Delete all telemetry for a build. Returns count deleted."""
    cursor = conn.execute(
        "DELETE FROM telemetry_events WHERE build_id = ?", (build_id,)
    )
    conn.commit()
    return cursor.rowcount


# ---- Slot expected-unit catalog ---------------------------------------

def upsert_slot_expected_unit(
    conn: sqlite3.Connection,
    *,
    physics_type: str,
    sdim: str,
    feature_type: str,
    feature_scope: str,
    slot_property: str,
    comsol_version: str,
    n_attempts: int,
    n_resolved: int,
    coverage_frac: float,
    modal_unit: str | None,
    modal_count: int | None,
    modal_frac: float | None,
    distribution: dict,
    confidence: str,
    type_source: str,
) -> None:
    """Insert or replace one catalog row."""
    conn.execute(
        """
        INSERT OR REPLACE INTO slot_expected_units (
            physics_type, sdim, feature_type, feature_scope, slot_property,
            comsol_version,
            n_attempts, n_resolved, coverage_frac,
            modal_unit, modal_count, modal_frac,
            distribution, confidence, type_source
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            physics_type, sdim, feature_type, feature_scope, slot_property,
            comsol_version,
            n_attempts, n_resolved, coverage_frac,
            modal_unit, modal_count, modal_frac,
            json.dumps(distribution, sort_keys=True),
            confidence, type_source,
        ),
    )


def lookup_expected_unit(
    conn: sqlite3.Connection,
    *,
    physics_type: str,
    sdim: str,
    feature_type: str,
    feature_scope: str,
    slot_property: str,
    comsol_version: str,
    min_confidence: str = "high",
) -> dict | None:
    """Look up the expected unit for a five-axis key.

    Version-fallback policy:
      1. Exact `comsol_version` match first.
      2. If none, fall back to the highest-version row for the same
         five-axis key and mark `version_mismatch=True` on the result.
      3. Never silently cross versions without annotation.

    Confidence filter: rows with `confidence` lower than `min_confidence`
    are excluded. Default is 'high' — callers that want medium-confidence
    hits must pass `min_confidence='medium'`.

    Returns a dict with catalog row fields plus a `version_mismatch`
    boolean. Returns None if no row matches.
    """
    confidence_order = {"low": 0, "medium": 1, "high": 2}
    floor = confidence_order.get(min_confidence, 2)
    allowed = [
        c for c, v in confidence_order.items() if v >= floor
    ]
    placeholders = ",".join("?" * len(allowed))

    exact = conn.execute(
        f"""
        SELECT * FROM slot_expected_units
        WHERE physics_type = ? AND sdim = ? AND feature_type = ?
              AND feature_scope = ? AND slot_property = ?
              AND comsol_version = ?
              AND confidence IN ({placeholders})
        """,
        (physics_type, sdim, feature_type, feature_scope, slot_property,
         comsol_version, *allowed),
    ).fetchone()
    if exact is not None:
        row = dict(exact)
        row["version_mismatch"] = False
        row["distribution"] = json.loads(row.get("distribution") or "{}")
        return row

    fallback = conn.execute(
        f"""
        SELECT * FROM slot_expected_units
        WHERE physics_type = ? AND sdim = ? AND feature_type = ?
              AND feature_scope = ? AND slot_property = ?
              AND confidence IN ({placeholders})
        ORDER BY comsol_version DESC LIMIT 1
        """,
        (physics_type, sdim, feature_type, feature_scope, slot_property,
         *allowed),
    ).fetchone()
    if fallback is not None:
        row = dict(fallback)
        row["version_mismatch"] = True
        row["distribution"] = json.loads(row.get("distribution") or "{}")
        return row
    return None


def upsert_variable_declared_unit(
    conn: sqlite3.Connection,
    *,
    var_name: str,
    physics_type: str,
    sdim: str,
    declaration_key: str,
    declared_unit: str,
    n_occurrences: int,
    comsol_version: str,
) -> None:
    """Insert or replace one variable_declared_units row."""
    conn.execute(
        """
        INSERT OR REPLACE INTO variable_declared_units (
            var_name, physics_type, sdim, declaration_key, declared_unit,
            n_occurrences, comsol_version
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (var_name, physics_type, sdim, declaration_key, declared_unit,
         n_occurrences, comsol_version),
    )


def lookup_variable_declared_units(
    conn: sqlite3.Connection,
    var_name: str,
    *,
    physics_type: str | None = None,
    sdim: str | None = None,
) -> list[dict]:
    """Return all declared-unit rows for a variable name.

    Optionally narrow by physics_type and/or sdim. Multiple rows are
    expected (a variable may carry different declared units across
    models / contexts).
    """
    clauses = ["var_name = ?"]
    params: list = [var_name]
    if physics_type is not None:
        clauses.append("physics_type = ?")
        params.append(physics_type)
    if sdim is not None:
        clauses.append("sdim = ?")
        params.append(sdim)
    q = (
        "SELECT * FROM variable_declared_units WHERE "
        + " AND ".join(clauses)
        + " ORDER BY n_occurrences DESC"
    )
    rows = conn.execute(q, params).fetchall()
    return [dict(r) for r in rows]


def count_slot_catalog(conn: sqlite3.Connection) -> dict:
    """Return {'total', 'high', 'medium', 'low'} for the slot catalog."""
    total = conn.execute(
        "SELECT COUNT(*) FROM slot_expected_units"
    ).fetchone()[0]
    by_conf = dict(conn.execute(
        "SELECT confidence, COUNT(*) FROM slot_expected_units "
        "GROUP BY confidence"
    ).fetchall())
    return {
        "total": total,
        "high": by_conf.get("high", 0),
        "medium": by_conf.get("medium", 0),
        "low": by_conf.get("low", 0),
    }
