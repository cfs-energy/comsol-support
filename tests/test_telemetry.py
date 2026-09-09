"""Tests for the telemetry loop: parse, sidecar, digest, health, DB,
MCP dispatch, and mphgen integration."""

from __future__ import annotations

import json
import os
import subprocess
import zipfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from comsol_support.db import (
    clear_telemetry_for_build,
    get_telemetry_events,
    init_db,
    search_telemetry,
    store_telemetry_events,
)
from comsol_support.mphgen import REQUIRED_MPH_MEMBERS, generate_mph
from comsol_support.telemetry import (
    TELEMETRY_PREFIX,
    check_health,
    cmd_ingest_telemetry,
    digest,
    ingest_from_path,
    ingest_sidecar_to_db,
    parse_telemetry_lines,
    partial_path_for,
    read_telemetry_sidecar,
    sidecar_path_for,
    write_telemetry_sidecar,
)
from tests.conftest import FakePopen


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

def test_parse_extracts_events_in_order():
    stdout = (
        "banner line\n"
        f"{TELEMETRY_PREFIX}"
        '{"event_type":"run_start","wall_ms":0,"payload":{"x":1}}\n'
        "unrelated line\n"
        f"{TELEMETRY_PREFIX}"
        '{"event_type":"halt","wall_ms":100,"payload":{"halt_reason":"success"}}\n'
    )
    events = parse_telemetry_lines(stdout)
    assert len(events) == 2
    assert events[0]["event_type"] == "run_start"
    assert events[1]["event_type"] == "halt"
    assert events[1]["payload"]["halt_reason"] == "success"


def test_parse_skips_malformed_lines():
    stdout = (
        f"{TELEMETRY_PREFIX}not-json\n"
        f"{TELEMETRY_PREFIX}{{\"event_type\":\"ok\",\"wall_ms\":1,\"payload\":{{}}}}\n"
        f"{TELEMETRY_PREFIX}[1,2,3]\n"  # JSON but not an object
    )
    events = parse_telemetry_lines(stdout)
    assert len(events) == 1
    assert events[0]["event_type"] == "ok"


def test_parse_empty_and_none_safe():
    assert parse_telemetry_lines("") == []
    assert parse_telemetry_lines(None) == []  # type: ignore[arg-type]


def test_parse_ignores_non_prefixed_lines():
    stdout = "just a regular line\n{\"not\":\"telemetry\"}\n"
    assert parse_telemetry_lines(stdout) == []


# ---------------------------------------------------------------------------
# Sidecar I/O
# ---------------------------------------------------------------------------

def test_sidecar_path_convention(tmp_path):
    mph = tmp_path / "build.mph"
    assert sidecar_path_for(mph) == tmp_path / "build.mph.telemetry.jsonl"


def test_sidecar_roundtrip(tmp_path):
    mph = tmp_path / "x.mph"
    events = [
        {"event_type": "run_start", "wall_ms": 0, "payload": {}},
        {"event_type": "halt", "wall_ms": 5,
         "payload": {"halt_reason": "success"}},
    ]
    path = write_telemetry_sidecar(mph, events)
    assert path.exists()
    recovered = read_telemetry_sidecar(mph)
    assert recovered == events


def test_sidecar_handles_missing_file(tmp_path):
    assert read_telemetry_sidecar(tmp_path / "missing.mph") == []


def test_sidecar_tolerates_malformed_lines(tmp_path):
    mph = tmp_path / "x.mph"
    path = sidecar_path_for(mph)
    path.write_text(
        '{"event_type":"ok","wall_ms":1,"payload":{}}\n'
        "not json\n"
        '{"event_type":"ok2","wall_ms":2,"payload":{}}\n'
    )
    events = read_telemetry_sidecar(mph)
    assert [e["event_type"] for e in events] == ["ok", "ok2"]


# ---------------------------------------------------------------------------
# Digest
# ---------------------------------------------------------------------------

def test_digest_summarizes_events():
    events = [
        {"event_type": "run_start", "wall_ms": 0, "payload": {}},
        {"event_type": "solve_start", "wall_ms": 10,
         "payload": {"study_tag": "std1"}},
        {"event_type": "solve_done", "wall_ms": 50,
         "payload": {"study_tag": "std1", "status": "success"}},
        {"event_type": "save_done", "wall_ms": 60,
         "payload": {"path": "/out.mph"}},
        {"event_type": "halt", "wall_ms": 65,
         "payload": {"halt_reason": "success", "message": ""}},
    ]
    d = digest(events)
    assert d.event_count == 5
    assert d.event_type_counts["solve_done"] == 1
    assert d.total_wall_ms == 65
    assert d.halt_reason == "success"
    assert d.solved is True
    assert d.solve_study == "std1"
    assert d.save_done_path == "/out.mph"
    assert d.partial_save_path is None


def test_digest_captures_partial_save():
    events = [
        {"event_type": "solve_start", "wall_ms": 1, "payload": {}},
        {"event_type": "partial_save", "wall_ms": 10,
         "payload": {"path": "/out.partial.mph", "reason": "solver_error"}},
        {"event_type": "halt", "wall_ms": 11,
         "payload": {"halt_reason": "solver_error", "message": "boom"}},
    ]
    d = digest(events)
    assert d.halt_reason == "solver_error"
    assert d.partial_save_path == "/out.partial.mph"
    assert d.solved is False


def test_digest_captures_error_detail_from_halt():
    events = [
        {"event_type": "solve_start", "wall_ms": 1,
         "payload": {"study_tag": "std1"}},
        {"event_type": "solve_done", "wall_ms": 50,
         "payload": {"study_tag": "std1", "status": "error",
                     "error": "Study 'std1' failed",
                     "error_detail": ["Feature: Stationary Solver 1",
                                      "Undefined variable: t"]}},
        {"event_type": "halt", "wall_ms": 51,
         "payload": {"halt_reason": "solver_error", "message": "boom",
                     "error_detail": ["Feature: Stationary Solver 1",
                                      "Undefined variable: t",
                                      "1 DOF NaN"]}},
    ]
    d = digest(events)
    assert d.halt_reason == "solver_error"
    # halt's richer detail wins over the earlier solve_done detail.
    assert d.error_detail == [
        "Feature: Stationary Solver 1", "Undefined variable: t", "1 DOF NaN"]
    assert "error_detail" in d.as_dict()


def test_digest_error_detail_falls_back_to_solve_done():
    # halt carries no detail (e.g. an older/edge path) — solve_done's
    # detail is still surfaced.
    events = [
        {"event_type": "solve_done", "wall_ms": 50,
         "payload": {"study_tag": "s", "status": "error",
                     "error_detail": ["aveop1 over an empty selection"]}},
        {"event_type": "halt", "wall_ms": 51,
         "payload": {"halt_reason": "solver_error", "message": "boom"}},
    ]
    d = digest(events)
    assert d.error_detail == ["aveop1 over an empty selection"]


def test_digest_no_error_detail_on_success():
    events = [
        {"event_type": "solve_done", "wall_ms": 50,
         "payload": {"study_tag": "s", "status": "success"}},
        {"event_type": "halt", "wall_ms": 51,
         "payload": {"halt_reason": "success", "message": ""}},
    ]
    d = digest(events)
    assert d.error_detail == []


def test_digest_accumulates_mutator_error_events():
    """A mutator that swallows a mesh exception and reports it via
    SolverTelemetry.emitError("mesh_error", e) ends the run with
    halt_reason=success — the digest must still count the event and
    accumulate its detail (in one campaign, every failing build
    read `halt_reason: success` / `error_detail: []`)."""
    events = [
        {"event_type": "mesh_error", "wall_ms": 200_000,
         "payload": {"message": "FlException: Source face must be specified",
                     "error_detail": ["Failed to create swept mesh.",
                                      "- Domain: 2"]}},
        {"event_type": "mesh_error", "wall_ms": 210_000,
         "payload": {"message": "FlException: Source face must be specified",
                     "error_detail": ["Failed to create swept mesh.",
                                      "- Domain: 7"]}},
        {"event_type": "save_done", "wall_ms": 250_000,
         "payload": {"path": "out.mph"}},
        {"event_type": "halt", "wall_ms": 251_000,
         "payload": {"halt_reason": "success", "message": ""}},
    ]
    d = digest(events)
    assert d.error_events == 2
    # De-duplicated, order-preserving accumulation across both events.
    assert d.error_event_detail == [
        "Failed to create swept mesh.", "- Domain: 2",
        "FlException: Source face must be specified", "- Domain: 7"]
    # Terminal error_detail semantics unchanged: success halt → empty.
    assert d.error_detail == []
    dd = d.as_dict()
    assert dd["error_events"] == 2
    assert dd["error_event_detail"][0] == "Failed to create swept mesh."


def test_digest_counts_result_error_as_error_event():
    events = [
        {"event_type": "result_error", "wall_ms": 10,
         "payload": {"message": "probe extraction failed"}},
        {"event_type": "halt", "wall_ms": 11,
         "payload": {"halt_reason": "success", "message": ""}},
    ]
    d = digest(events)
    assert d.error_events == 1
    assert d.error_event_detail == ["probe extraction failed"]


def test_digest_counts_mesh_heartbeats_and_captures_census():
    """mesh_heartbeat is a first-class heartbeat (--mesh verb) and the
    post-run mesh_census payload is surfaced on the digest — the build's
    real result even when ms.run() threw."""
    events = [
        {"event_type": "mesh_start", "wall_ms": 1,
         "payload": {"mesh_tag": "mesh1", "component": "comp1"}},
        {"event_type": "mesh_heartbeat", "wall_ms": 2000,
         "payload": {"mesh_tag": "mesh1", "rss_bytes": 1234}},
        {"event_type": "mesh_heartbeat", "wall_ms": 4000,
         "payload": {"mesh_tag": "mesh1", "rss_bytes": 2345}},
        {"event_type": "mesh_census", "wall_ms": 5000,
         "payload": {"mesh_tag": "mesh1",
                     "element_counts_by_type": {"tet": 100, "hex": 50,
                                                "prism": 0, "pyr": 0},
                     "total_volumetric_elements": 150,
                     "meshed_domain_count": 7,
                     "n_domains": 9,
                     "unmeshed_domain_count": 2}},
        {"event_type": "halt", "wall_ms": 6000,
         "payload": {"halt_reason": "success", "message": ""}},
    ]
    d = digest(events)
    assert d.heartbeat_count == 2
    assert d.mesh_census is not None
    assert d.mesh_census["unmeshed_domain_count"] == 2
    assert d.as_dict()["mesh_census"]["meshed_domain_count"] == 7


def test_digest_last_event_type_shows_abrupt_death():
    """A stream ending on a heartbeat (no halt) is the signature of an
    abrupt process death — last_event_type makes it stateable."""
    events = [
        {"event_type": "run_start", "wall_ms": 0, "payload": {}},
        {"event_type": "mesh_heartbeat", "wall_ms": 5000, "payload": {}},
        {"event_type": "mesh_heartbeat", "wall_ms": 7000, "payload": {}},
    ]
    d = digest(events)
    assert d.last_event_type == "mesh_heartbeat"
    assert d.halt_reason is None
    assert d.as_dict()["last_event_type"] == "mesh_heartbeat"


def test_describe_abnormal_exit_decodes_signal_and_finds_hs_err(tmp_path):
    from comsol_support.java_facade import describe_abnormal_exit
    # SIGABRT decode + dump discovery
    dump = tmp_path / "hs_err_pid1234.log"
    dump.write_text("# A fatal error has been detected")
    desc = describe_abnormal_exit(-6, 1234, [tmp_path])
    assert "SIGABRT" in desc and "exit=-6" in desc
    assert str(dump) in desc
    # SIGSEGV, no dump anywhere → says absence is not evidence
    desc2 = describe_abnormal_exit(-11, 9999, [tmp_path])
    assert "SIGSEGV" in desc2
    assert "absence of a dump is not absence of a native crash" in desc2
    # Normal exit codes produce no noise
    assert describe_abnormal_exit(0, 1234, [tmp_path]) == ""
    assert describe_abnormal_exit(1, None) == ""


def test_stream_telemetry_raw_log_tee(tmp_path):
    """raw_log_path tees EVERY raw line (telemetry included) to disk —
    the crash-safe evidence channel a killed JVM leaves behind."""
    from comsol_support.telemetry import stream_telemetry_lines
    lines = [
        "COMSOL mesh log line 1\n",
        'TELEMETRY: {"event_type":"mesh_heartbeat","wall_ms":5,'
        '"payload":{}}\n',
        '{"success": true}\n',
    ]
    sidecar = tmp_path / "t.telemetry.jsonl"
    sidecar.touch()
    raw = tmp_path / "t.jvm.log"
    txt, evs = stream_telemetry_lines(
        iter(lines), sidecar, raw_log_path=raw)
    assert raw.read_text() == "".join(lines)
    assert txt == "".join(lines)
    assert len(evs) == 1 and evs[0]["event_type"] == "mesh_heartbeat"


def test_stream_telemetry_raw_log_created_even_when_stream_empty(tmp_path):
    """An empty stream still creates the (0-byte) log file: 'process
    emitted nothing' must be distinguishable from 'tee never ran'
    (six 0-byte logs once fed a fabricated narrative)."""
    from comsol_support.telemetry import stream_telemetry_lines
    sidecar = tmp_path / "t.telemetry.jsonl"
    sidecar.touch()
    raw = tmp_path / "t.jvm.log"
    txt, evs = stream_telemetry_lines(iter([]), sidecar, raw_log_path=raw)
    assert raw.exists() and raw.stat().st_size == 0
    assert txt == "" and evs == []


def test_stream_telemetry_raw_log_tee_failure_does_not_abort(tmp_path):
    """A tee that cannot open (path is a directory) is logged and
    skipped — the drain and its return value are unaffected."""
    from comsol_support.telemetry import stream_telemetry_lines
    lines = ["line one\n", "line two\n"]
    sidecar = tmp_path / "t.telemetry.jsonl"
    sidecar.touch()
    txt, evs = stream_telemetry_lines(
        iter(lines), sidecar, raw_log_path=tmp_path)  # dir → open fails
    assert txt == "".join(lines)
    assert evs == []


def test_check_health_error_events_budget():
    """max_error_events is opt-in: default None keeps historical
    behavior (error events informational); 0 requires a clean run."""
    events = [
        {"event_type": "mesh_error", "wall_ms": 5,
         "payload": {"message": "boom", "error_detail": ["detail line"]}},
        {"event_type": "halt", "wall_ms": 9,
         "payload": {"halt_reason": "success", "message": ""}},
    ]
    default = check_health(events)
    assert default.passed  # unchanged default behavior
    strict = check_health(events, max_error_events=0)
    assert not strict.passed
    assert any("error_events=1 exceeds max=0" in r for r in strict.reasons)
    assert any("detail line" in r for r in strict.reasons)


def test_digest_tolerates_malformed_input():
    events = [
        {"event_type": "run_start", "wall_ms": "abc", "payload": {}},
        {"event_type": None},
        {},  # type: ignore[typeddict-item]
    ]
    d = digest(events)
    assert d.event_count == 3
    assert d.total_wall_ms == 0


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------

def test_health_pass_on_success_halt():
    events = [
        {"event_type": "run_start", "wall_ms": 0, "payload": {}},
        {"event_type": "halt", "wall_ms": 10,
         "payload": {"halt_reason": "success"}},
    ]
    r = check_health(events)
    assert r.passed is True
    assert r.halt_reason == "success"


def test_health_fail_on_missing_halt():
    events = [{"event_type": "run_start", "wall_ms": 0, "payload": {}}]
    r = check_health(events)
    assert r.passed is False
    assert "no halt event" in r.reasons[0]


def test_health_fail_on_error_halt():
    events = [
        {"event_type": "halt", "wall_ms": 1,
         "payload": {"halt_reason": "solver_error"}},
    ]
    r = check_health(events)
    assert r.passed is False
    assert r.halt_reason == "solver_error"


def test_health_allows_informational_halts_by_default():
    events = [
        {"event_type": "halt", "wall_ms": 1,
         "payload": {"halt_reason": "tlist_truncation"}},
    ]
    r = check_health(events)
    assert r.passed is True


def test_health_strict_allowed_halts():
    events = [
        {"event_type": "halt", "wall_ms": 1,
         "payload": {"halt_reason": "tlist_truncation"}},
    ]
    r = check_health(events, allowed_halts={"success"})
    assert r.passed is False


def test_health_fail_on_wall_budget_exceeded():
    events = [
        {"event_type": "halt", "wall_ms": 5000,
         "payload": {"halt_reason": "success"}},
    ]
    r = check_health(events, wall_budget_ms=1000)
    assert r.passed is False
    assert "wall_ms=5000" in r.reasons[0]


def test_health_fail_on_partial_save_failed():
    events = [
        {"event_type": "partial_save_failed", "wall_ms": 1,
         "payload": {"error": "disk full"}},
        {"event_type": "halt", "wall_ms": 2,
         "payload": {"halt_reason": "solver_error"}},
    ]
    r = check_health(events, allowed_halts={"success", "solver_error"})
    assert r.passed is False
    # Both the halt allow-list check and the partial-save failure can
    # contribute reasons; we only need to confirm the partial_save_failed
    # contribution is present.
    assert any("partial_save_failed" in reason for reason in r.reasons)


# ---------------------------------------------------------------------------
# DB
# ---------------------------------------------------------------------------

@pytest.fixture
def db_conn(tmp_path):
    conn = init_db(tmp_path / "t.db")
    yield conn
    conn.close()


def test_db_store_and_get_roundtrip(db_conn):
    events = [
        {"event_type": "run_start", "wall_ms": 0, "payload": {"x": 1}},
        {"event_type": "halt", "wall_ms": 5,
         "payload": {"halt_reason": "success"}},
    ]
    n = store_telemetry_events(
        db_conn, build_id="b1", output_path="/out.mph", events=events,
    )
    assert n == 2
    rows = get_telemetry_events(db_conn, "b1")
    assert [r["event_type"] for r in rows] == ["run_start", "halt"]
    assert json.loads(rows[0]["payload_json"]) == {"x": 1}


def test_db_filter_by_event_type(db_conn):
    store_telemetry_events(db_conn, build_id="b1", output_path="/o.mph",
                           events=[
                               {"event_type": "halt", "wall_ms": 1,
                                "payload": {"halt_reason": "success"}},
                               {"event_type": "run_start", "wall_ms": 0,
                                "payload": {}},
                           ])
    rows = get_telemetry_events(db_conn, "b1", event_type="halt")
    assert len(rows) == 1
    assert rows[0]["event_type"] == "halt"


def test_db_allows_null_build_id(db_conn):
    n = store_telemetry_events(
        db_conn, build_id=None, output_path="/o.mph",
        events=[{"event_type": "run_start", "wall_ms": 0, "payload": {}}],
    )
    assert n == 1


def test_db_skips_non_dict_events(db_conn):
    n = store_telemetry_events(
        db_conn, build_id="b1", output_path="/o.mph",
        events=[
            {"event_type": "run_start", "wall_ms": 0, "payload": {}},
            "not a dict",  # type: ignore[list-item]
            123,  # type: ignore[list-item]
        ],
    )
    assert n == 1


def test_db_fts_search(db_conn):
    store_telemetry_events(
        db_conn, build_id="b1", output_path="/o1.mph",
        events=[{"event_type": "halt", "wall_ms": 1,
                 "payload": {"halt_reason": "solver_error"}}],
    )
    store_telemetry_events(
        db_conn, build_id="b2", output_path="/o2.mph",
        events=[{"event_type": "halt", "wall_ms": 1,
                 "payload": {"halt_reason": "success"}}],
    )
    hits = search_telemetry(db_conn, "solver_error")
    assert any(r["build_id"] == "b1" for r in hits)
    assert not any(r["build_id"] == "b2" for r in hits)


def test_db_clear_for_build(db_conn):
    store_telemetry_events(
        db_conn, build_id="b1", output_path="/o.mph",
        events=[{"event_type": "run_start", "wall_ms": 0, "payload": {}}],
    )
    assert clear_telemetry_for_build(db_conn, "b1") == 1
    assert get_telemetry_events(db_conn, "b1") == []


def test_db_wall_ms_coerced_to_null_when_invalid(db_conn):
    store_telemetry_events(
        db_conn, build_id="b1", output_path="/o.mph",
        events=[{"event_type": "x", "wall_ms": "bad", "payload": {}}],
    )
    rows = get_telemetry_events(db_conn, "b1")
    assert rows[0]["wall_ms"] is None


# ---------------------------------------------------------------------------
# Sidecar → DB ingest
# ---------------------------------------------------------------------------

def test_ingest_sidecar_to_db(tmp_path, db_conn):
    mph = tmp_path / "x.mph"
    events = [
        {"event_type": "run_start", "wall_ms": 0, "payload": {}},
        {"event_type": "halt", "wall_ms": 1,
         "payload": {"halt_reason": "success"}},
    ]
    write_telemetry_sidecar(mph, events)
    n = ingest_sidecar_to_db(db_conn, "bx", mph)
    assert n == 2
    assert len(get_telemetry_events(db_conn, "bx")) == 2


def test_ingest_empty_sidecar_no_error(tmp_path, db_conn):
    mph = tmp_path / "x.mph"
    # No sidecar present
    assert ingest_sidecar_to_db(db_conn, "bx", mph) == 0


# ---------------------------------------------------------------------------
# MCP dispatch
# ---------------------------------------------------------------------------

def test_mcp_get_solver_telemetry(db_conn):
    from comsol_support.mcp_server import handle_tool_call
    store_telemetry_events(
        db_conn, build_id="b1", output_path="/o.mph",
        events=[
            {"event_type": "run_start", "wall_ms": 0, "payload": {}},
            {"event_type": "halt", "wall_ms": 5,
             "payload": {"halt_reason": "success"}},
        ],
    )
    out = handle_tool_call(db_conn, "get_solver_telemetry",
                           {"build_id": "b1"})
    assert "run_start" in out
    assert "halt" in out


def test_mcp_get_solver_telemetry_filter(db_conn):
    from comsol_support.mcp_server import handle_tool_call
    store_telemetry_events(
        db_conn, build_id="b1", output_path="/o.mph",
        events=[
            {"event_type": "run_start", "wall_ms": 0, "payload": {}},
            {"event_type": "halt", "wall_ms": 5,
             "payload": {"halt_reason": "success"}},
        ],
    )
    out = handle_tool_call(db_conn, "get_solver_telemetry",
                           {"build_id": "b1", "event_type": "halt"})
    assert "halt" in out
    assert "run_start" not in out


def test_mcp_get_solver_telemetry_no_data(db_conn):
    from comsol_support.mcp_server import handle_tool_call
    out = handle_tool_call(db_conn, "get_solver_telemetry",
                           {"build_id": "missing"})
    assert "No telemetry" in out


def test_mcp_search_telemetry(db_conn):
    from comsol_support.mcp_server import handle_tool_call
    store_telemetry_events(
        db_conn, build_id="b1", output_path="/o.mph",
        events=[{"event_type": "halt", "wall_ms": 1,
                 "payload": {"halt_reason": "solver_error"}}],
    )
    out = handle_tool_call(db_conn, "search_telemetry",
                           {"query": "solver_error"})
    assert "b1" in out
    assert "solver_error" in out


def test_mcp_tools_listed():
    from comsol_support.mcp_server import TOOLS
    names = {t["name"] for t in TOOLS}
    assert "get_solver_telemetry" in names
    assert "search_telemetry" in names


# ---------------------------------------------------------------------------
# mphgen integration
# ---------------------------------------------------------------------------

def _make_fake_mph(path: Path) -> None:
    import os as _os
    with zipfile.ZipFile(path, "w", zipfile.ZIP_STORED) as zf:
        for member in REQUIRED_MPH_MEMBERS:
            zf.writestr(member, _os.urandom(1024))


_SIMPLE_BUILDER = (
    "import com.comsol.model.*;\n"
    "import com.comsol.model.util.*;\n"
    "import java.util.Map;\n"
    "public class ExampleBuilder {\n"
    "    public static Model buildModel(Map<String,String> args) {\n"
    "        return null;\n"
    "    }\n"
    "}\n"
)


def _mock_mphgen_deps():
    """Return context managers for mocking JavaFacade methods used by mphgen."""
    jf = __import__("comsol_support.java_facade",
                    fromlist=["JavaFacade"]).JavaFacade
    return (
        patch.object(jf, "find_java_executable", return_value="/fake/java"),
        patch.object(jf, "get_full_classpath", return_value="/fake/cp"),
        patch.object(jf, "get_comsol_env", return_value=os.environ.copy()),
        patch.object(jf, "compile_stage_code",
                     return_value=MagicMock(success=True, stderr="")),
    )


def test_mphgen_writes_telemetry_sidecar_on_success(tmp_path):
    builder = tmp_path / "ExampleBuilder.java"
    builder.write_text(_SIMPLE_BUILDER)
    output = tmp_path / "out.mph"

    def fake_run(argv, **kwargs):
        if "-Djava.awt.headless=true" in argv:
            _make_fake_mph(output)
            stdout = (
                f"{TELEMETRY_PREFIX}"
                '{"event_type":"run_start","wall_ms":0,"payload":{}}\n'
                f"{TELEMETRY_PREFIX}"
                '{"event_type":"save_done","wall_ms":40,'
                # json.dumps escapes Windows path backslashes
                '"payload":{"path":' + json.dumps(str(output)) + '}}\n'
                f"{TELEMETRY_PREFIX}"
                '{"event_type":"halt","wall_ms":41,'
                '"payload":{"halt_reason":"success","message":""}}\n'
                + json.dumps({
                    "success": True, "output": str(output),
                    "builder": "ExampleBuilder",
                    "elapsed_ms": 42,
                }) + "\n"
            )
            return FakePopen(stdout_text=stdout, returncode=0)
        return FakePopen(stdout_text="", returncode=0)

    m1, m2, m3, m4 = _mock_mphgen_deps()
    with patch("comsol_support.mphgen.subprocess.Popen",
               side_effect=fake_run), m1, m2, m3, m4:
        compiled_dir = tmp_path / "java_compiled"
        compiled_dir.mkdir()
        (compiled_dir / "ModelExporter.class").write_bytes(b"\xca\xfe\xba\xbe")
        (compiled_dir / "SolverTelemetry.class").write_bytes(b"\xca\xfe\xba\xbe")
        result = generate_mph(
            builder_java=builder, output_mph=output,
            workspace_dir=tmp_path, run_linting=False,
        )

    assert result.success is True
    sidecar = sidecar_path_for(output)
    assert sidecar.exists()
    events = read_telemetry_sidecar(output)
    assert [e["event_type"] for e in events] == [
        "run_start", "save_done", "halt",
    ]
    assert result.telemetry_sidecar_path == str(sidecar)
    assert result.telemetry_event_count == 3
    assert result.telemetry_halt_reason == "success"
    assert result.telemetry_digest["solved"] is False


def test_mphgen_writes_telemetry_sidecar_on_solver_failure(tmp_path):
    """A halted solve still produces a full telemetry sidecar, and the
    raised error surfaces the halt reason and partial .mph path."""
    from comsol_support.mphgen import BuildFailure

    builder = tmp_path / "ExampleBuilder.java"
    builder.write_text(_SIMPLE_BUILDER)
    output = tmp_path / "out.mph"
    partial = tmp_path / "out.partial.mph"

    def fake_run(argv, **kwargs):
        if "-Djava.awt.headless=true" in argv:
            # Java side does NOT write output.mph — it writes partial.mph.
            _make_fake_mph(partial)
            stdout = (
                f"{TELEMETRY_PREFIX}"
                '{"event_type":"run_start","wall_ms":0,"payload":{}}\n'
                f"{TELEMETRY_PREFIX}"
                '{"event_type":"solve_done","wall_ms":30,'
                '"payload":{"study_tag":"std1","status":"error",'
                '"error":"boom"}}\n'
                f"{TELEMETRY_PREFIX}"
                '{"event_type":"partial_save","wall_ms":31,'
                # json.dumps escapes Windows path backslashes
                '"payload":{"path":' + json.dumps(str(partial))
                + ',"reason":"solver_error"}}\n'
                f"{TELEMETRY_PREFIX}"
                '{"event_type":"halt","wall_ms":32,'
                '"payload":{"halt_reason":"solver_error","message":"boom"}}\n'
                + json.dumps({
                    "success": False,
                    "error": "Study 'std1' failed: boom",
                }) + "\n"
            )
            return FakePopen(stdout_text=stdout, returncode=1)
        return FakePopen(stdout_text="", returncode=0)

    m1, m2, m3, m4 = _mock_mphgen_deps()
    with patch("comsol_support.mphgen.subprocess.Popen",
               side_effect=fake_run), m1, m2, m3, m4:
        compiled_dir = tmp_path / "java_compiled"
        compiled_dir.mkdir()
        (compiled_dir / "ModelExporter.class").write_bytes(b"\xca\xfe\xba\xbe")
        (compiled_dir / "SolverTelemetry.class").write_bytes(b"\xca\xfe\xba\xbe")
        with pytest.raises(BuildFailure) as exc_info:
            generate_mph(
                builder_java=builder, output_mph=output,
                solve_study="std1",
                workspace_dir=tmp_path, run_linting=False,
            )

    # Telemetry sidecar was written regardless of the exception.
    sidecar = sidecar_path_for(output)
    assert sidecar.exists()
    events = read_telemetry_sidecar(output)
    types = [e["event_type"] for e in events]
    assert "halt" in types
    assert "partial_save" in types
    # Error message includes diagnostic feed-forward.
    err_str = str(exc_info.value)
    assert "halt_reason: solver_error" in err_str
    assert "partial .mph" in err_str
    assert str(partial) in err_str


# ---------------------------------------------------------------------------
# partial_path_for + stale-partial cleanup
# ---------------------------------------------------------------------------

def test_partial_path_for_mph_extension(tmp_path):
    assert partial_path_for(tmp_path / "foo.mph") == tmp_path / "foo.partial.mph"


def test_partial_path_for_no_mph_extension(tmp_path):
    p = tmp_path / "foo"
    assert partial_path_for(p) == tmp_path / "foo.partial.mph"


def test_mphgen_cleans_up_stale_partial_on_success(tmp_path):
    """A stale .partial.mph from a prior failed run is removed when a
    later run at the same output path succeeds."""
    builder = tmp_path / "ExampleBuilder.java"
    builder.write_text(_SIMPLE_BUILDER)
    output = tmp_path / "out.mph"
    stale = partial_path_for(output)
    stale.write_bytes(b"stale-from-prior-failed-run")

    def fake_run(argv, **kwargs):
        if "-Djava.awt.headless=true" in argv:
            _make_fake_mph(output)
            stdout = (
                f"{TELEMETRY_PREFIX}"
                '{"event_type":"halt","wall_ms":1,'
                '"payload":{"halt_reason":"success","message":""}}\n'
                + json.dumps({
                    "success": True, "output": str(output),
                    "builder": "ExampleBuilder", "elapsed_ms": 1,
                }) + "\n"
            )
            return FakePopen(stdout_text=stdout, returncode=0)
        return FakePopen(stdout_text="", returncode=0)

    m1, m2, m3, m4 = _mock_mphgen_deps()
    with patch("comsol_support.mphgen.subprocess.Popen",
               side_effect=fake_run), m1, m2, m3, m4:
        compiled_dir = tmp_path / "java_compiled"
        compiled_dir.mkdir()
        (compiled_dir / "ModelExporter.class").write_bytes(b"\xca\xfe\xba\xbe")
        (compiled_dir / "SolverTelemetry.class").write_bytes(b"\xca\xfe\xba\xbe")
        generate_mph(
            builder_java=builder, output_mph=output,
            workspace_dir=tmp_path, run_linting=False,
        )

    assert output.exists()
    assert not stale.exists()


# ---------------------------------------------------------------------------
# partial_save_timeout health counting
# ---------------------------------------------------------------------------

def test_health_fails_on_partial_save_timeout():
    events = [
        {"event_type": "partial_save_timeout", "wall_ms": 30_000,
         "payload": {"timeout_ms": "30000"}},
        {"event_type": "halt", "wall_ms": 31_000,
         "payload": {"halt_reason": "solver_error"}},
    ]
    r = check_health(events, allowed_halts={"success", "solver_error"})
    assert r.passed is False
    assert any("partial_save_failed" in reason for reason in r.reasons)


def test_health_counts_timeout_and_failed_together():
    events = [
        {"event_type": "partial_save_timeout", "wall_ms": 1, "payload": {}},
        {"event_type": "partial_save_failed", "wall_ms": 2, "payload": {}},
        {"event_type": "halt", "wall_ms": 3,
         "payload": {"halt_reason": "success"}},
    ]
    r = check_health(events, max_partial_save_failures=1)
    # Combined count (2) exceeds budget (1) → fail.
    assert r.passed is False


# ---------------------------------------------------------------------------
# mphgen opt-in DB ingest
# ---------------------------------------------------------------------------

def test_generate_mph_ingests_to_db_when_conn_and_build_id_given(
    tmp_path, db_conn,
):
    builder = tmp_path / "ExampleBuilder.java"
    builder.write_text(_SIMPLE_BUILDER)
    output = tmp_path / "out.mph"

    def fake_run(argv, **kwargs):
        if "-Djava.awt.headless=true" in argv:
            _make_fake_mph(output)
            stdout = (
                f"{TELEMETRY_PREFIX}"
                '{"event_type":"run_start","wall_ms":0,"payload":{}}\n'
                f"{TELEMETRY_PREFIX}"
                '{"event_type":"halt","wall_ms":2,'
                '"payload":{"halt_reason":"success","message":""}}\n'
                + json.dumps({
                    "success": True, "output": str(output),
                    "builder": "ExampleBuilder", "elapsed_ms": 2,
                }) + "\n"
            )
            return FakePopen(stdout_text=stdout, returncode=0)
        return FakePopen(stdout_text="", returncode=0)

    m1, m2, m3, m4 = _mock_mphgen_deps()
    with patch("comsol_support.mphgen.subprocess.Popen",
               side_effect=fake_run), m1, m2, m3, m4:
        compiled_dir = tmp_path / "java_compiled"
        compiled_dir.mkdir()
        (compiled_dir / "ModelExporter.class").write_bytes(b"\xca\xfe\xba\xbe")
        (compiled_dir / "SolverTelemetry.class").write_bytes(b"\xca\xfe\xba\xbe")
        generate_mph(
            builder_java=builder, output_mph=output,
            workspace_dir=tmp_path, run_linting=False,
            db_conn=db_conn, build_id="b-ingest",
        )

    rows = get_telemetry_events(db_conn, "b-ingest")
    assert [r["event_type"] for r in rows] == ["run_start", "halt"]


def test_generate_mph_db_ingest_failure_does_not_fail_build(
    tmp_path, db_conn,
):
    """A DB error during ingest is logged, not raised."""
    builder = tmp_path / "ExampleBuilder.java"
    builder.write_text(_SIMPLE_BUILDER)
    output = tmp_path / "out.mph"

    def fake_run(argv, **kwargs):
        if "-Djava.awt.headless=true" in argv:
            _make_fake_mph(output)
            stdout = (
                f"{TELEMETRY_PREFIX}"
                '{"event_type":"halt","wall_ms":1,'
                '"payload":{"halt_reason":"success"}}\n'
                + json.dumps({
                    "success": True, "output": str(output),
                    "builder": "ExampleBuilder", "elapsed_ms": 1,
                }) + "\n"
            )
            return FakePopen(stdout_text=stdout, returncode=0)
        return FakePopen(stdout_text="", returncode=0)

    # Close the connection before the call so any DB write raises.
    db_conn.close()

    m1, m2, m3, m4 = _mock_mphgen_deps()
    with patch("comsol_support.mphgen.subprocess.Popen",
               side_effect=fake_run), m1, m2, m3, m4:
        compiled_dir = tmp_path / "java_compiled"
        compiled_dir.mkdir()
        (compiled_dir / "ModelExporter.class").write_bytes(b"\xca\xfe\xba\xbe")
        (compiled_dir / "SolverTelemetry.class").write_bytes(b"\xca\xfe\xba\xbe")
        # Should not raise.
        result = generate_mph(
            builder_java=builder, output_mph=output,
            workspace_dir=tmp_path, run_linting=False,
            db_conn=db_conn, build_id="b-broken",
        )
    assert result.success is True


# ---------------------------------------------------------------------------
# ingest_from_path + CLI
# ---------------------------------------------------------------------------

def test_ingest_from_path_single_mph(tmp_path, db_conn):
    mph = tmp_path / "x.mph"
    write_telemetry_sidecar(mph, [
        {"event_type": "halt", "wall_ms": 1,
         "payload": {"halt_reason": "success"}},
    ])
    summary = ingest_from_path(db_conn, mph, build_id="bfile")
    assert summary == {"mph_count": 1, "event_count": 1}
    assert len(get_telemetry_events(db_conn, "bfile")) == 1


def test_ingest_from_path_sidecar_file_arg(tmp_path, db_conn):
    mph = tmp_path / "x.mph"
    sidecar = write_telemetry_sidecar(mph, [
        {"event_type": "halt", "wall_ms": 1,
         "payload": {"halt_reason": "success"}},
    ])
    summary = ingest_from_path(db_conn, sidecar, build_id="bsidecar")
    assert summary["mph_count"] == 1
    assert summary["event_count"] == 1


def test_ingest_from_path_directory_walk(tmp_path, db_conn):
    sub = tmp_path / "nested"
    sub.mkdir()
    for name in ("a.mph", "b.mph"):
        write_telemetry_sidecar(sub / name, [
            {"event_type": "halt", "wall_ms": 1,
             "payload": {"halt_reason": "success"}},
        ])
    # Stray file that should not be ingested.
    (sub / "random.txt").write_text("noise")
    summary = ingest_from_path(db_conn, tmp_path, build_id="bdir")
    assert summary["mph_count"] == 2
    assert summary["event_count"] == 2


def test_ingest_from_path_idempotent_with_build_id(tmp_path, db_conn):
    mph = tmp_path / "x.mph"
    write_telemetry_sidecar(mph, [
        {"event_type": "halt", "wall_ms": 1,
         "payload": {"halt_reason": "success"}},
    ])
    ingest_from_path(db_conn, mph, build_id="bidem")
    ingest_from_path(db_conn, mph, build_id="bidem")
    # Still only 1 row for that build_id — re-ingest cleared first.
    assert len(get_telemetry_events(db_conn, "bidem")) == 1


def test_ingest_from_path_mph_without_sidecar_is_ignored(tmp_path, db_conn):
    mph = tmp_path / "x.mph"
    mph.write_bytes(b"no sidecar")
    summary = ingest_from_path(db_conn, mph, build_id="bmiss")
    assert summary == {"mph_count": 0, "event_count": 0}


def test_cli_ingest_subparser_present():
    from comsol_support.cli import build_parser
    parser = build_parser()
    ns = parser.parse_args([
        "ingest-telemetry", "/tmp/foo.mph",
        "--db", "/tmp/test.db", "--build-id", "b1",
    ])
    assert ns.command == "ingest-telemetry"
    assert ns.path == "/tmp/foo.mph"
    assert ns.db == "/tmp/test.db"
    assert ns.build_id == "b1"


def test_cli_ingest_runs_end_to_end(tmp_path, capsys):
    db_path = tmp_path / "t.db"
    mph = tmp_path / "x.mph"
    write_telemetry_sidecar(mph, [
        {"event_type": "halt", "wall_ms": 1,
         "payload": {"halt_reason": "success"}},
    ])
    ns = MagicMock()
    ns.path = str(mph)
    ns.db = str(db_path)
    ns.build_id = "bcli"
    rc = cmd_ingest_telemetry(ns)
    assert rc == 0
    # Verify DB got populated.
    conn = init_db(db_path)
    try:
        rows = get_telemetry_events(conn, "bcli")
        assert len(rows) == 1
    finally:
        conn.close()
    captured = capsys.readouterr()
    assert "ingested 1 event" in captured.out


def test_cli_ingest_missing_path_returns_error(tmp_path, capsys):
    ns = MagicMock()
    ns.path = str(tmp_path / "missing")
    ns.db = str(tmp_path / "t.db")
    ns.build_id = None
    rc = cmd_ingest_telemetry(ns)
    assert rc == 2


# ---------------------------------------------------------------------------
# mphgen CLI: --ingest-db / --ingest-build-id
# ---------------------------------------------------------------------------

def test_mphgen_cli_accepts_ingest_flags():
    from comsol_support.cli import build_parser
    parser = build_parser()
    ns = parser.parse_args([
        "mphgen", "--builder", "foo.java",
        "--ingest-db", "/tmp/t.db", "--ingest-build-id", "stg5",
    ])
    assert ns.ingest_db == "/tmp/t.db"
    assert ns.ingest_build_id == "stg5"


# ---------------------------------------------------------------------------
# Streaming sidecar writes (Delta 1)
# ---------------------------------------------------------------------------

def test_streaming_appends_events_on_arrival(tmp_path):
    """stream_telemetry_lines writes each event to the sidecar before the
    stdout iterator has been fully consumed — the key real-time property."""
    from comsol_support.telemetry import (
        read_telemetry_sidecar,
        stream_telemetry_lines,
        truncate_telemetry_sidecar,
    )
    mph = tmp_path / "live.mph"
    sidecar = truncate_telemetry_sidecar(mph)

    # Snapshots after each line is parsed by the stream loop.
    snapshots: list[list[str]] = []

    class _Gen:
        def __init__(self):
            self._lines = iter([
                f"{TELEMETRY_PREFIX}"
                '{"event_type":"run_start","wall_ms":0,"payload":{}}\n',
                f"{TELEMETRY_PREFIX}"
                '{"event_type":"solve_start","wall_ms":10,'
                '"payload":{"study_tag":"std1"}}\n',
                f"{TELEMETRY_PREFIX}"
                '{"event_type":"halt","wall_ms":20,'
                '"payload":{"halt_reason":"success"}}\n',
            ])

        def __iter__(self):
            return self

        def __next__(self):
            ln = next(self._lines)  # raises StopIteration at end
            # Snapshot the sidecar as-of just BEFORE this line is consumed,
            # then the stream loop will append the parsed event.
            snapshots.append([
                e["event_type"] for e in read_telemetry_sidecar(mph)
            ])
            return ln

    stream_telemetry_lines(_Gen(), sidecar)

    # Between line N and line N+1, the sidecar must have N-1 events —
    # proving appends happen synchronously as lines arrive, not at the end.
    assert snapshots == [[], ["run_start"], ["run_start", "solve_start"]]
    # Final sidecar has all three.
    final = [e["event_type"] for e in read_telemetry_sidecar(mph)]
    assert final == ["run_start", "solve_start", "halt"]


def test_streaming_invokes_on_event_callback(tmp_path):
    from comsol_support.telemetry import (
        stream_telemetry_lines, truncate_telemetry_sidecar,
    )
    mph = tmp_path / "cb.mph"
    sidecar = truncate_telemetry_sidecar(mph)
    seen: list[str] = []

    lines = [
        f"{TELEMETRY_PREFIX}"
        '{"event_type":"solver_heartbeat","wall_ms":2000,"payload":{}}\n',
        f"{TELEMETRY_PREFIX}"
        '{"event_type":"halt","wall_ms":3000,'
        '"payload":{"halt_reason":"success"}}\n',
    ]
    stream_telemetry_lines(
        iter(lines), sidecar, on_event=lambda ev: seen.append(ev["event_type"]),
    )
    assert seen == ["solver_heartbeat", "halt"]


def test_streaming_callback_exception_is_logged_not_raised(tmp_path, caplog):
    from comsol_support.telemetry import (
        stream_telemetry_lines, truncate_telemetry_sidecar,
    )
    mph = tmp_path / "cb.mph"
    sidecar = truncate_telemetry_sidecar(mph)

    def _boom(_ev):
        raise RuntimeError("callback broke")

    # Must not raise.
    lines = [
        f"{TELEMETRY_PREFIX}"
        '{"event_type":"halt","wall_ms":1,"payload":{"halt_reason":"success"}}\n'
    ]
    with caplog.at_level("WARNING"):
        stream_telemetry_lines(iter(lines), sidecar, on_event=_boom)
    assert any("on_event callback raised" in r.message for r in caplog.records)


def test_streaming_mirror_receives_raw_lines(tmp_path):
    import io as _io
    from comsol_support.telemetry import (
        stream_telemetry_lines, truncate_telemetry_sidecar,
    )
    mph = tmp_path / "m.mph"
    sidecar = truncate_telemetry_sidecar(mph)
    mirror = _io.StringIO()
    lines = [
        "banner from JVM\n",
        f"{TELEMETRY_PREFIX}"
        '{"event_type":"halt","wall_ms":1,"payload":{"halt_reason":"success"}}\n',
    ]
    stream_telemetry_lines(iter(lines), sidecar, mirror=mirror)
    mirror_content = mirror.getvalue()
    assert "banner from JVM" in mirror_content
    assert TELEMETRY_PREFIX in mirror_content


def test_streaming_tolerates_malformed_lines(tmp_path):
    from comsol_support.telemetry import (
        read_telemetry_sidecar,
        stream_telemetry_lines,
        truncate_telemetry_sidecar,
    )
    mph = tmp_path / "mal.mph"
    sidecar = truncate_telemetry_sidecar(mph)
    lines = [
        f"{TELEMETRY_PREFIX}not-json\n",
        f"{TELEMETRY_PREFIX}"
        '{"event_type":"ok","wall_ms":1,"payload":{}}\n',
        f"{TELEMETRY_PREFIX}[1,2]\n",
    ]
    _, events = stream_telemetry_lines(iter(lines), sidecar)
    assert [e["event_type"] for e in events] == ["ok"]
    assert [e["event_type"] for e in read_telemetry_sidecar(mph)] == ["ok"]


# ---------------------------------------------------------------------------
# Heartbeat + result aggregation in digest (Delta 2/5)
# ---------------------------------------------------------------------------

def test_digest_counts_heartbeats_and_records_results():
    events = [
        {"event_type": "run_start", "wall_ms": 0, "payload": {}},
        {"event_type": "solve_start", "wall_ms": 10,
         "payload": {"study_tag": "std1"}},
        {"event_type": "solver_heartbeat", "wall_ms": 2010,
         "payload": {"study_tag": "std1", "elapsed_solve_ms": 2000}},
        {"event_type": "solver_heartbeat", "wall_ms": 4010,
         "payload": {"study_tag": "std1", "elapsed_solve_ms": 4000}},
        {"event_type": "solve_done", "wall_ms": 5000,
         "payload": {"study_tag": "std1", "status": "success"}},
        {"event_type": "result_global", "wall_ms": 5010,
         "payload": {"tag": "gev1", "name": "total_Q",
                     "shape": [1, 1], "series": [[1234.5]]}},
        {"event_type": "result_probe", "wall_ms": 5020,
         "payload": {"tag": "pt1", "name": "T_max",
                     "shape": [3, 2], "series": [[0, 293], [1, 310], [2, 342]]}},
        {"event_type": "halt", "wall_ms": 5100,
         "payload": {"halt_reason": "success"}},
    ]
    d = digest(events)
    assert d.heartbeat_count == 2
    assert d.solved is True
    assert "global:total_Q" in d.results
    assert "probe:T_max" in d.results
    assert d.results["global:total_Q"]["series"] == [[1234.5]]


def test_digest_result_events_without_name_are_skipped():
    events = [
        {"event_type": "result_probe", "wall_ms": 1,
         "payload": {"tag": "", "name": "", "series": []}},
    ]
    d = digest(events)
    assert d.results == {}


# ---------------------------------------------------------------------------
# DB: since_id + event_types filtering (Delta 4)
# ---------------------------------------------------------------------------

def test_db_since_id_returns_only_newer_events(db_conn):
    store_telemetry_events(db_conn, build_id="b", output_path="/o.mph",
                           events=[
                               {"event_type": "run_start", "wall_ms": 0,
                                "payload": {}},
                               {"event_type": "solve_start", "wall_ms": 10,
                                "payload": {}},
                               {"event_type": "halt", "wall_ms": 20,
                                "payload": {"halt_reason": "success"}},
                           ])
    rows = get_telemetry_events(db_conn, "b")
    assert [r["event_type"] for r in rows] == [
        "run_start", "solve_start", "halt",
    ]
    mid_id = rows[0]["id"]
    tailed = get_telemetry_events(db_conn, "b", since_id=mid_id)
    assert [r["event_type"] for r in tailed] == ["solve_start", "halt"]


def test_db_event_types_filter_multiple(db_conn):
    store_telemetry_events(db_conn, build_id="b", output_path="/o.mph",
                           events=[
                               {"event_type": "run_start", "wall_ms": 0,
                                "payload": {}},
                               {"event_type": "result_probe", "wall_ms": 1,
                                "payload": {"tag": "p1", "name": "T"}},
                               {"event_type": "result_global", "wall_ms": 2,
                                "payload": {"tag": "g1", "name": "Q"}},
                               {"event_type": "halt", "wall_ms": 3,
                                "payload": {"halt_reason": "success"}},
                           ])
    rows = get_telemetry_events(
        db_conn, "b",
        event_types=("result_probe", "result_global"),
    )
    assert {r["event_type"] for r in rows} == {"result_probe", "result_global"}


# ---------------------------------------------------------------------------
# MCP: since_id + get_solver_results (Delta 4)
# ---------------------------------------------------------------------------

def test_mcp_get_solver_telemetry_since_id(db_conn):
    from comsol_support.mcp_server import handle_tool_call
    store_telemetry_events(
        db_conn, build_id="b1", output_path="/o.mph",
        events=[
            {"event_type": "run_start", "wall_ms": 0, "payload": {}},
            {"event_type": "solver_heartbeat", "wall_ms": 2000,
             "payload": {"elapsed_solve_ms": 2000}},
            {"event_type": "halt", "wall_ms": 3000,
             "payload": {"halt_reason": "success"}},
        ],
    )
    first_row = get_telemetry_events(db_conn, "b1")[0]
    out = handle_tool_call(
        db_conn, "get_solver_telemetry",
        {"build_id": "b1", "since_id": first_row["id"]},
    )
    # Only events after the first were returned.
    assert "run_start" not in out
    assert "solver_heartbeat" in out
    assert "halt" in out
    assert "last_id=" in out


def test_mcp_get_solver_results_aggregates(db_conn):
    from comsol_support.mcp_server import handle_tool_call
    store_telemetry_events(
        db_conn, build_id="b1", output_path="/o.mph",
        events=[
            {"event_type": "result_probe", "wall_ms": 1,
             "payload": {"tag": "p1", "name": "T_max",
                         "shape": [3, 2],
                         "series": [[0, 293], [1, 310], [2, 342]]}},
            {"event_type": "result_global", "wall_ms": 2,
             "payload": {"tag": "g1", "name": "total_Q",
                         "shape": [1, 1], "series": [[1234.5]]}},
            {"event_type": "result_error", "wall_ms": 3,
             "payload": {"kind": "probe", "tag": "p2",
                         "error": "NullPointerException"}},
            {"event_type": "halt", "wall_ms": 4,
             "payload": {"halt_reason": "success"}},
        ],
    )
    out = handle_tool_call(db_conn, "get_solver_results", {"build_id": "b1"})
    assert "Globals (1)" in out
    assert "total_Q" in out
    assert "1234.5" in out
    assert "Probes (1)" in out
    assert "T_max" in out
    assert "Extraction errors (1)" in out
    assert "NullPointerException" in out


def test_mcp_get_solver_results_empty(db_conn):
    from comsol_support.mcp_server import handle_tool_call
    out = handle_tool_call(db_conn, "get_solver_results", {"build_id": "x"})
    assert "No solver results" in out


def test_mcp_tools_listed_include_get_solver_results():
    from comsol_support.mcp_server import TOOLS
    names = {t["name"] for t in TOOLS}
    assert "get_solver_results" in names


# ---------------------------------------------------------------------------
# solver_info capture (Delta #1)
# ---------------------------------------------------------------------------

def test_solver_info_events_flow_through_stream(tmp_path):
    """A mix of solver_info and TELEMETRY lines in the subprocess stream
    is parsed correctly: both event types land in the sidecar, nothing
    is dropped, order is preserved."""
    from comsol_support.telemetry import (
        read_telemetry_sidecar,
        stream_telemetry_lines,
        truncate_telemetry_sidecar,
    )
    mph = tmp_path / "si.mph"
    sidecar = truncate_telemetry_sidecar(mph)

    lines = [
        # Raw COMSOL banner — not TELEMETRY, not captured by the sidecar.
        "COMSOL 6.4 starting up\n",
        f"{TELEMETRY_PREFIX}"
        '{"event_type":"solve_start","wall_ms":0,'
        '"payload":{"study_tag":"std1"}}\n',
        # Java-side solver_info events, as the tee would emit them.
        f"{TELEMETRY_PREFIX}"
        '{"event_type":"solver_info","wall_ms":100,'
        '"payload":{"message":"Time: 0.0010 s"}}\n',
        f"{TELEMETRY_PREFIX}"
        '{"event_type":"solver_info","wall_ms":200,'
        '"payload":{"message":"Nonlinear iteration 3"}}\n',
        f"{TELEMETRY_PREFIX}"
        '{"event_type":"halt","wall_ms":500,'
        '"payload":{"halt_reason":"success"}}\n',
    ]
    _, events = stream_telemetry_lines(iter(lines), sidecar)

    types = [e["event_type"] for e in events]
    assert types == ["solve_start", "solver_info", "solver_info", "halt"]

    stored = read_telemetry_sidecar(mph)
    assert [e["event_type"] for e in stored] == types
    assert stored[1]["payload"]["message"] == "Time: 0.0010 s"
    assert stored[2]["payload"]["message"] == "Nonlinear iteration 3"


def test_solver_info_counted_by_digest():
    events = [
        {"event_type": "solve_start", "wall_ms": 0, "payload": {}},
        {"event_type": "solver_info", "wall_ms": 10,
         "payload": {"message": "Time: 0.001 s"}},
        {"event_type": "solver_info", "wall_ms": 20,
         "payload": {"message": "Time: 0.002 s"}},
        {"event_type": "halt", "wall_ms": 30,
         "payload": {"halt_reason": "success"}},
    ]
    d = digest(events)
    assert d.event_type_counts.get("solver_info") == 2
    assert d.halt_reason == "success"


def test_mcp_get_solver_telemetry_returns_solver_info(db_conn):
    from comsol_support.mcp_server import handle_tool_call
    store_telemetry_events(
        db_conn, build_id="b1", output_path="/o.mph",
        events=[
            {"event_type": "solver_info", "wall_ms": 10,
             "payload": {"message": "Time: 0.001 s"}},
            {"event_type": "solver_info", "wall_ms": 20,
             "payload": {"message": "Nonlinear iteration 3"}},
            {"event_type": "halt", "wall_ms": 30,
             "payload": {"halt_reason": "success"}},
        ],
    )
    # With filter.
    out = handle_tool_call(
        db_conn, "get_solver_telemetry",
        {"build_id": "b1", "event_type": "solver_info"},
    )
    assert "Time: 0.001 s" in out
    assert "Nonlinear iteration 3" in out
    assert "halt" not in out
    # Without filter, solver_info events still appear alongside halt.
    all_out = handle_tool_call(
        db_conn, "get_solver_telemetry", {"build_id": "b1"},
    )
    assert "solver_info" in all_out
    assert "halt" in all_out


# ---------------------------------------------------------------------------
# Units on result events (Delta #3)
# ---------------------------------------------------------------------------

def test_digest_preserves_unit_on_result_payload():
    events = [
        {"event_type": "result_global", "wall_ms": 1,
         "payload": {"tag": "gev1", "name": "total_Q", "unit": "W",
                     "shape": [1, 1], "series": [[1234.5]]}},
        {"event_type": "result_probe", "wall_ms": 2,
         "payload": {"tag": "pt1", "name": "T_max", "unit": "K",
                     "shape": [3, 2], "series": [[0, 293], [1, 310],
                                                   [2, 342]]}},
    ]
    d = digest(events)
    assert d.results["global:total_Q"]["unit"] == "W"
    assert d.results["probe:T_max"]["unit"] == "K"


def test_mcp_get_solver_results_renders_units(db_conn):
    from comsol_support.mcp_server import handle_tool_call
    store_telemetry_events(
        db_conn, build_id="b1", output_path="/o.mph",
        events=[
            {"event_type": "result_global", "wall_ms": 1,
             "payload": {"tag": "gev1", "name": "total_Q", "unit": "W",
                         "shape": [1, 1], "series": [[1234.5]]}},
            {"event_type": "result_probe", "wall_ms": 2,
             "payload": {"tag": "pt1", "name": "T_max", "unit": "K",
                         "shape": [3, 2], "series": [[0, 293], [2, 342]]}},
            {"event_type": "halt", "wall_ms": 3,
             "payload": {"halt_reason": "success"}},
        ],
    )
    out = handle_tool_call(db_conn, "get_solver_results", {"build_id": "b1"})
    # Global value printed with its unit suffix.
    assert "total_Q: 1234.5 W" in out
    # Probe name with [unit] tag.
    assert "T_max [K]" in out


def test_mcp_get_solver_results_omits_unit_when_empty(db_conn):
    """A probe/global with no unit renders cleanly without a stray ' '
    or '[]' artifact."""
    from comsol_support.mcp_server import handle_tool_call
    store_telemetry_events(
        db_conn, build_id="b1", output_path="/o.mph",
        events=[
            {"event_type": "result_global", "wall_ms": 1,
             "payload": {"tag": "g1", "name": "scalar", "unit": "",
                         "shape": [1, 1], "series": [[42.0]]}},
            {"event_type": "result_probe", "wall_ms": 2,
             "payload": {"tag": "p1", "name": "unitless",
                         "shape": [2, 1], "series": [[0], [1]]}},
        ],
    )
    out = handle_tool_call(db_conn, "get_solver_results", {"build_id": "b1"})
    # No trailing unit marker.
    assert "scalar: 42.0 [shape=" in out
    # No [unit] tag.
    assert "unitless: shape=" in out
    assert "[]:" not in out


# ---------------------------------------------------------------------------
# mphgen: streaming path creates sidecar before completion (Delta 2)
# ---------------------------------------------------------------------------

def test_mphgen_truncates_sidecar_at_start(tmp_path):
    """The sidecar must exist (empty) the instant Popen starts, so an
    external tail -f consumer has a file to follow even before the first
    TELEMETRY line. Uses a side_effect that asserts the file existence
    during the Popen call."""
    builder = tmp_path / "ExampleBuilder.java"
    builder.write_text(_SIMPLE_BUILDER)
    output = tmp_path / "out.mph"
    sidecar = sidecar_path_for(output)
    saw_sidecar_early = {"ok": False}

    def fake_run(argv, **kwargs):
        if "-Djava.awt.headless=true" in argv:
            # Sidecar must exist BEFORE any telemetry has been written.
            if sidecar.exists() and sidecar.read_text() == "":
                saw_sidecar_early["ok"] = True
            _make_fake_mph(output)
            stdout = (
                f"{TELEMETRY_PREFIX}"
                '{"event_type":"halt","wall_ms":1,'
                '"payload":{"halt_reason":"success"}}\n'
                + json.dumps({
                    "success": True, "output": str(output),
                    "builder": "ExampleBuilder", "elapsed_ms": 1,
                }) + "\n"
            )
            return FakePopen(stdout_text=stdout, returncode=0)
        return FakePopen(stdout_text="", returncode=0)

    m1, m2, m3, m4 = _mock_mphgen_deps()
    with patch("comsol_support.mphgen.subprocess.Popen",
               side_effect=fake_run), m1, m2, m3, m4:
        compiled_dir = tmp_path / "java_compiled"
        compiled_dir.mkdir()
        (compiled_dir / "ModelExporter.class").write_bytes(b"\xca\xfe\xba\xbe")
        (compiled_dir / "SolverTelemetry.class").write_bytes(b"\xca\xfe\xba\xbe")
        generate_mph(
            builder_java=builder, output_mph=output,
            workspace_dir=tmp_path, run_linting=False,
        )

    assert saw_sidecar_early["ok"] is True


def test_mphgen_on_event_callback_receives_live_events(tmp_path):
    builder = tmp_path / "ExampleBuilder.java"
    builder.write_text(_SIMPLE_BUILDER)
    output = tmp_path / "out.mph"
    seen: list[str] = []

    def fake_run(argv, **kwargs):
        if "-Djava.awt.headless=true" in argv:
            _make_fake_mph(output)
            stdout = (
                f"{TELEMETRY_PREFIX}"
                '{"event_type":"run_start","wall_ms":0,"payload":{}}\n'
                f"{TELEMETRY_PREFIX}"
                '{"event_type":"solver_heartbeat","wall_ms":2000,'
                '"payload":{"elapsed_solve_ms":2000}}\n'
                f"{TELEMETRY_PREFIX}"
                '{"event_type":"halt","wall_ms":3000,'
                '"payload":{"halt_reason":"success"}}\n'
                + json.dumps({
                    "success": True, "output": str(output),
                    "builder": "ExampleBuilder", "elapsed_ms": 3000,
                }) + "\n"
            )
            return FakePopen(stdout_text=stdout, returncode=0)
        return FakePopen(stdout_text="", returncode=0)

    m1, m2, m3, m4 = _mock_mphgen_deps()
    with patch("comsol_support.mphgen.subprocess.Popen",
               side_effect=fake_run), m1, m2, m3, m4:
        compiled_dir = tmp_path / "java_compiled"
        compiled_dir.mkdir()
        (compiled_dir / "ModelExporter.class").write_bytes(b"\xca\xfe\xba\xbe")
        (compiled_dir / "SolverTelemetry.class").write_bytes(b"\xca\xfe\xba\xbe")
        generate_mph(
            builder_java=builder, output_mph=output,
            workspace_dir=tmp_path, run_linting=False,
            on_event=lambda ev: seen.append(ev["event_type"]),
        )

    assert seen == ["run_start", "solver_heartbeat", "halt"]


def test_mphgen_timeout_kills_process_and_raises(tmp_path):
    """If the child never exits, proc.wait(timeout) fires and we kill,
    raising BuildFailure — drainer joins cleanly on the closed pipe."""
    from comsol_support.mphgen import BuildFailure

    builder = tmp_path / "ExampleBuilder.java"
    builder.write_text(_SIMPLE_BUILDER)
    output = tmp_path / "out.mph"

    class _HangingPopen(FakePopen):
        def __init__(self, argv, **kwargs):
            super().__init__(stdout_text="", returncode=0)

        def wait(self, timeout=None):
            # Simulate a hung child: timeout never satisfied.
            raise subprocess.TimeoutExpired(cmd="fake", timeout=timeout)

    def fake_run(argv, **kwargs):
        if "-Djava.awt.headless=true" in argv:
            return _HangingPopen(argv, **kwargs)
        return FakePopen(stdout_text="", returncode=0)

    m1, m2, m3, m4 = _mock_mphgen_deps()
    with patch("comsol_support.mphgen.subprocess.Popen",
               side_effect=fake_run), m1, m2, m3, m4:
        compiled_dir = tmp_path / "java_compiled"
        compiled_dir.mkdir()
        (compiled_dir / "ModelExporter.class").write_bytes(b"\xca\xfe\xba\xbe")
        (compiled_dir / "SolverTelemetry.class").write_bytes(b"\xca\xfe\xba\xbe")
        with pytest.raises(BuildFailure, match="timed out"):
            generate_mph(
                builder_java=builder, output_mph=output,
                workspace_dir=tmp_path, run_linting=False,
                timeout_s=1,
            )


def test_mphgen_cli_rejects_ingest_db_without_build_id(tmp_path, capsys):
    from comsol_support.mphgen import cmd_mphgen
    ns = MagicMock()
    ns.builder = str(tmp_path / "foo.java")
    ns.output = str(tmp_path / "foo.mph")
    ns.arg = []
    ns.solve = None
    ns.comsol_path = None
    ns.workspace = None
    ns.timeout = 600
    ns.force = False
    ns.no_sidecar = False
    ns.no_lint = True
    ns.skip_layer_a = True
    ns.skip_layer_b = True
    ns.skip_layer_c = True
    ns.ingest_db = str(tmp_path / "t.db")
    ns.ingest_build_id = None
    rc = cmd_mphgen(ns)
    assert rc == 2
    assert "ingest-db requires" in capsys.readouterr().err
