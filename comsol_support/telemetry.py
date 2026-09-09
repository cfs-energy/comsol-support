"""Telemetry loop — the Python side of the model-agnostic solver event stream
emitted by the Java facade (SolverTelemetry / ModelExporter).

Responsibilities:
  - Parse TELEMETRY: lines out of subprocess stdout.
  - Persist events to a JSONL sidecar next to the .mph.
  - Build a compact digest (counts, wall budget, halt reason).
  - Health-check the digest against caller-supplied budgets.
  - Ingest the sidecar into SQLite (via db.store_telemetry_events) when
    the caller has a build_id and a conn — kept optional so mphgen itself
    remains DB-free.

Deliberately small: no runtime dependencies, no COMSOL imports, safe to
use anywhere in the package.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

logger = logging.getLogger("comsol_support.telemetry")

TELEMETRY_PREFIX = "TELEMETRY: "

# Halt reasons the Java facade may emit. Not a closed enum — callers may
# define their own. These are the ones this module treats as "success-ish".
SUCCESS_HALTS = frozenset({"success"})

# Halt reasons that indicate something to pay attention to but may still
# be the correct terminal state for a given caller (e.g. tlist truncation
# when that is the brief's explicit predicate).
INFORMATIONAL_HALTS = frozenset({
    "tlist_truncation",
    "wall_ceiling",
    "user_interrupt",
})

# Halt reasons that are unambiguously errors.
ERROR_HALTS = frozenset({
    "solver_error",
    "save_error",
    "build_error",
    "contract_error",
    "exception",
})


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

def parse_telemetry_lines(stdout_text: str) -> list[dict]:
    """Extract telemetry events from arbitrary subprocess stdout.

    Lines that do not start with TELEMETRY_PREFIX are ignored. Lines that
    start with the prefix but fail to JSON-decode are logged and skipped —
    the loop is best-effort and must never crash on a malformed line.

    Returns events in emission order.
    """
    events: list[dict] = []
    if not stdout_text:
        return events
    for raw in stdout_text.splitlines():
        line = raw.rstrip()
        if not line.startswith(TELEMETRY_PREFIX):
            continue
        body = line[len(TELEMETRY_PREFIX):].strip()
        if not body:
            continue
        try:
            ev = json.loads(body)
        except json.JSONDecodeError as e:
            logger.warning("telemetry: skipping malformed line (%s): %s",
                           e, body[:200])
            continue
        if not isinstance(ev, dict):
            logger.warning("telemetry: skipping non-object event: %s",
                           body[:200])
            continue
        events.append(ev)
    return events


# ---------------------------------------------------------------------------
# Sidecar I/O
# ---------------------------------------------------------------------------

def sidecar_path_for(mph_path: Path) -> Path:
    """Return the conventional sidecar path for a given .mph output."""
    mph_path = Path(mph_path)
    return mph_path.with_suffix(mph_path.suffix + ".telemetry.jsonl")


def partial_path_for(mph_path: Path) -> Path:
    """Return the conventional partial-save path for a given .mph output.

    Mirrors ModelExporter.partialPathFor: if the output ends in ".mph",
    insert ".partial" before the extension; otherwise append
    ".partial.mph". Kept in sync with the Java side so Python can find
    or clean up partials without guessing.
    """
    mph_path = Path(mph_path)
    s = str(mph_path)
    if s.endswith(".mph"):
        return Path(s[:-4] + ".partial.mph")
    return Path(s + ".partial.mph")


def write_telemetry_sidecar(
    mph_path: Path,
    events: Iterable[dict],
) -> Path:
    """Write events as JSON-lines to a sidecar next to mph_path.

    Overwrites existing content. Creates parent directories if needed.
    Returns the sidecar path (whether or not any events were written).
    """
    path = sidecar_path_for(mph_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for ev in events:
            f.write(json.dumps(ev, separators=(",", ":")) + "\n")
    return path


def truncate_telemetry_sidecar(mph_path: Path) -> Path:
    """Create (or empty) the sidecar file next to mph_path.

    Used at the start of a streaming run so external watchers can tail
    the file even before the first event is emitted. Returns the path.
    """
    path = sidecar_path_for(mph_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("")
    return path


def append_telemetry_event(sidecar_path: Path, event: dict) -> None:
    """Append one event as a single JSONL line and flush.

    Crash-safe granularity: each call opens, writes one line, flushes,
    and closes — so a `tail -f` consumer sees the event immediately and
    a killed parent loses at most the in-flight line. (No fsync: solver
    telemetry is progress signal, not durability-critical data.)
    Ordering is preserved by serial callers; there is no concurrency
    control beyond what the OS provides on append writes.
    """
    line = json.dumps(event, separators=(",", ":")) + "\n"
    with sidecar_path.open("a", encoding="utf-8") as f:
        f.write(line)
        f.flush()


def stream_telemetry_lines(
    stdout,
    sidecar_path: Path,
    *,
    on_event=None,
    mirror=None,
    raw_log_path: Path | None = None,
) -> tuple[str, list[dict]]:
    """Drain stdout line-by-line, writing telemetry events live.

    Consumes an iterable/readable of text lines (e.g. subprocess.Popen
    stdout in text mode). TELEMETRY: lines are parsed, appended to the
    sidecar as soon as they arrive, and optionally forwarded to an
    on_event callback. Non-TELEMETRY lines are collected verbatim (for
    the final JSON envelope parser in mphgen).

    Args:
        stdout: line-iterable of the subprocess' stdout stream.
        sidecar_path: sidecar JSONL path. Caller must pre-create (via
            truncate_telemetry_sidecar) so a tail -f consumer has a file.
        on_event: optional callable(dict) invoked for each parsed event.
            Exceptions from the callback are logged, never raised.
        mirror: optional writeable stream that receives every raw line
            (e.g. sys.stdout) for live-print use cases.
        raw_log_path: optional path; when set, EVERY raw line is teed to
            this file with per-line flush, so the full unfiltered JVM
            stream (COMSOL solver/mesh log, SIGQUIT thread dumps, the
            envelope) survives on disk even when the process is killed
            mid-run. Before this existed, the raw stream lived only in
            memory until process exit — a killed build left no raw log
            anywhere, and a post-campaign audit traced a
            fabricated-evidence narrative to exactly that hole. The file
            is created (truncated) immediately, so an empty file proves
            "process emitted nothing", distinguishable from "never ran".
            Tee I/O errors are logged once and never abort the drain.

    Returns:
        (full_stdout_text, parsed_events_in_order)
    """
    buf: list[str] = []
    events: list[dict] = []
    raw_fh = None
    raw_fh_error_logged = False
    if raw_log_path is not None:
        try:
            raw_fh = open(raw_log_path, "w", encoding="utf-8",
                          errors="replace")
        except OSError as e:
            logger.warning("telemetry: raw-log tee unavailable (%s): %s",
                           raw_log_path, e)
    try:
        for raw in stdout:
            if not raw:
                continue
            # subprocess text-mode yields lines with trailing newline
            buf.append(raw)
            if raw_fh is not None:
                try:
                    raw_fh.write(raw)
                    raw_fh.flush()
                except OSError as e:
                    if not raw_fh_error_logged:
                        raw_fh_error_logged = True
                        logger.warning(
                            "telemetry: raw-log tee write failed (%s): %s",
                            raw_log_path, e)
            if mirror is not None:
                try:
                    mirror.write(raw)
                    mirror.flush()
                except Exception:
                    pass
            line = raw.rstrip("\r\n")
            if not line.startswith(TELEMETRY_PREFIX):
                continue
            body = line[len(TELEMETRY_PREFIX):].strip()
            if not body:
                continue
            try:
                ev = json.loads(body)
            except json.JSONDecodeError as e:
                logger.warning(
                    "telemetry: skipping malformed streamed line (%s): %s",
                    e, body[:200],
                )
                continue
            if not isinstance(ev, dict):
                logger.warning(
                    "telemetry: skipping non-object streamed event: %s",
                    body[:200],
                )
                continue
            events.append(ev)
            try:
                append_telemetry_event(sidecar_path, ev)
            except OSError as e:
                logger.warning("telemetry: sidecar append failed: %s", e)
            if on_event is not None:
                try:
                    on_event(ev)
                except Exception as e:
                    logger.warning("telemetry on_event callback raised: %s", e)
    finally:
        if raw_fh is not None:
            try:
                raw_fh.close()
            except Exception:
                pass
    return "".join(buf), events


def read_telemetry_sidecar(mph_path: Path) -> list[dict]:
    """Read events back from a sidecar. Missing files return []."""
    path = sidecar_path_for(mph_path)
    if not path.exists():
        return []
    events: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError as e:
            logger.warning("telemetry sidecar: skipping malformed line (%s)", e)
    return events


# ---------------------------------------------------------------------------
# Digest
# ---------------------------------------------------------------------------

@dataclass
class TelemetryDigest:
    """Compact summary of a telemetry stream. Suitable for logging,
    sidecar metadata, and feed-forward into a calling agent's context."""
    event_count: int = 0
    event_type_counts: dict[str, int] = field(default_factory=dict)
    total_wall_ms: int = 0
    halt_reason: str | None = None
    halt_message: str = ""
    # COMSOL's detailed diagnostic lines harvested from the failing
    # exception chain (getTranslatableMessageArray / getMessages). Empty
    # on success or when no detail was available. Model-agnostic.
    error_detail: list[str] = field(default_factory=list)
    partial_save_path: str | None = None
    save_done_path: str | None = None
    solved: bool = False
    solve_study: str | None = None
    heartbeat_count: int = 0
    # Mid-run error events (any event_type ending in "_error", e.g. a
    # mutator-emitted "mesh_error" via SolverTelemetry.emitError, or a
    # "result_error"). These are distinct from the terminal
    # halt/solve_done error_detail above: a mutator that swallows a mesh
    # exception to preserve a partial mesh still ends with
    # halt_reason=success — without this accumulator the failure is
    # invisible. error_event_detail
    # accumulates each such event's payload error_detail lines and
    # message, de-duplicated, in stream order.
    error_events: int = 0
    error_event_detail: list[str] = field(default_factory=list)
    # Forensics: the type of the LAST event in the stream.
    # A stream whose last event is a heartbeat (no halt, no
    # jvm_shutdown) died abruptly mid-run — a real meshing campaign
    # could not even state this fact about its lost builds.
    last_event_type: str | None = None
    # First-class mesh build (--mesh verb): the post-run
    # element census payload — per-type counts, meshed/unmeshed domain
    # counts. "Score the artifact, not the exception": this is the
    # build's real result even when ms.run() threw.
    mesh_census: dict | None = None
    # Compact view of solver result payloads, keyed by (kind, name).
    # kind ∈ {"probe", "global"}; value is the last payload emitted.
    results: dict[str, dict] = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "event_count": self.event_count,
            "event_type_counts": dict(self.event_type_counts),
            "total_wall_ms": self.total_wall_ms,
            "halt_reason": self.halt_reason,
            "halt_message": self.halt_message,
            "error_detail": list(self.error_detail),
            "partial_save_path": self.partial_save_path,
            "save_done_path": self.save_done_path,
            "solved": self.solved,
            "solve_study": self.solve_study,
            "heartbeat_count": self.heartbeat_count,
            "error_events": self.error_events,
            "error_event_detail": list(self.error_event_detail),
            "last_event_type": self.last_event_type,
            "mesh_census": (dict(self.mesh_census)
                            if self.mesh_census is not None else None),
            "results": dict(self.results),
        }


def digest(events: Iterable[dict]) -> TelemetryDigest:
    """Summarize a telemetry event stream into a TelemetryDigest.

    Tolerant of missing fields and unknown event types — never raises
    on malformed input.
    """
    d = TelemetryDigest()
    for ev in events:
        d.event_count += 1
        et = str(ev.get("event_type", ""))
        if et:
            d.event_type_counts[et] = d.event_type_counts.get(et, 0) + 1
            d.last_event_type = et
        try:
            wall = int(ev.get("wall_ms", 0) or 0)
        except (TypeError, ValueError):
            wall = 0
        if wall > d.total_wall_ms:
            d.total_wall_ms = wall
        payload = ev.get("payload") or {}
        if et == "halt":
            d.halt_reason = payload.get("halt_reason") or d.halt_reason
            d.halt_message = payload.get("message") or d.halt_message
            detail = payload.get("error_detail")
            if isinstance(detail, list) and detail:
                d.error_detail = [str(x) for x in detail]
        elif et == "partial_save":
            d.partial_save_path = payload.get("path") or d.partial_save_path
        elif et == "save_done":
            d.save_done_path = payload.get("path") or d.save_done_path
        elif et == "solve_start":
            d.solve_study = payload.get("study_tag") or d.solve_study
        elif et == "solve_done":
            if payload.get("status") == "success":
                d.solved = True
            d.solve_study = payload.get("study_tag") or d.solve_study
            detail = payload.get("error_detail")
            if isinstance(detail, list) and detail and not d.error_detail:
                d.error_detail = [str(x) for x in detail]
        elif et in ("solver_heartbeat", "mesh_heartbeat"):
            # mesh_heartbeat is first-class (--mesh verb)
            d.heartbeat_count += 1
        elif et == "mesh_census":
            d.mesh_census = dict(payload)
        elif et.endswith("_error"):
            # Mid-run error event (mesh_error, result_error, ...). Count
            # it and accumulate its detail — de-duplicated, order-kept.
            d.error_events += 1
            for line in (payload.get("error_detail") or []):
                s = str(line)
                if s and s not in d.error_event_detail:
                    d.error_event_detail.append(s)
            msg = payload.get("message")
            if msg and str(msg) not in d.error_event_detail:
                d.error_event_detail.append(str(msg))
        elif et == "result_probe":
            name = str(payload.get("name") or "")
            if name:
                d.results[f"probe:{name}"] = dict(payload)
        elif et == "result_global":
            name = str(payload.get("name") or "")
            if name:
                d.results[f"global:{name}"] = dict(payload)
    return d


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------

@dataclass
class TelemetryHealthResult:
    """Outcome of a health check over a telemetry digest.

    passed=False means the caller's budget/expectation was not met.
    This is *not* the same as "solve failed" — a caller may deliberately
    accept an informational halt (e.g. tlist_truncation) as success.
    """
    passed: bool
    halt_reason: str | None
    reasons: list[str] = field(default_factory=list)
    digest: TelemetryDigest | None = None

    def summary(self) -> str:
        if self.passed:
            return f"telemetry OK (halt_reason={self.halt_reason})"
        return (f"telemetry FAILED (halt_reason={self.halt_reason}): "
                + "; ".join(self.reasons))


def check_health(
    events: Iterable[dict],
    *,
    wall_budget_ms: int | None = None,
    max_partial_save_failures: int = 0,
    allowed_halts: Iterable[str] | None = None,
    max_error_events: int | None = None,
) -> TelemetryHealthResult:
    """Check a telemetry stream against caller-supplied budgets.

    Args:
      events: Iterable of telemetry events (from parse or sidecar read).
      wall_budget_ms: If set, fail when total_wall_ms exceeds this value.
      max_partial_save_failures: Fail when partial_save_failed events
        exceed this count (default 0 — any failure is a problem).
      allowed_halts: Set of halt_reason strings the caller considers
        acceptable. Defaults to SUCCESS_HALTS ∪ INFORMATIONAL_HALTS,
        so a caller who wants only pure success must pass {"success"}.
      max_error_events: If set, fail when mid-run "*_error" events
        (digest.error_events — e.g. a mutator-emitted mesh_error) exceed
        this count. Default None keeps the historical behavior: error
        events are informational, since swallowing a mesh exception to
        preserve a partial mesh is a legitimate pattern. Pass 0 to
        require an error-event-free run.

    Returns TelemetryHealthResult.passed = True iff all checks pass.
    """
    events = list(events)
    d = digest(events)
    allowed = set(allowed_halts) if allowed_halts is not None \
        else (SUCCESS_HALTS | INFORMATIONAL_HALTS)

    reasons: list[str] = []

    halt = d.halt_reason
    if halt is None:
        reasons.append("no halt event emitted")
    elif halt not in allowed:
        reasons.append(f"halt_reason={halt!r} not in allowed={sorted(allowed)}")

    if wall_budget_ms is not None and d.total_wall_ms > wall_budget_ms:
        reasons.append(
            f"wall_ms={d.total_wall_ms} exceeds budget={wall_budget_ms}"
        )

    partial_failed = (
        d.event_type_counts.get("partial_save_failed", 0)
        + d.event_type_counts.get("partial_save_timeout", 0)
    )
    if partial_failed > max_partial_save_failures:
        reasons.append(
            f"partial_save_failed count {partial_failed} "
            f"exceeds max {max_partial_save_failures}"
        )

    if max_error_events is not None and d.error_events > max_error_events:
        detail_head = "; ".join(d.error_event_detail[:3])
        reasons.append(
            f"error_events={d.error_events} exceeds max={max_error_events}"
            + (f" (first: {detail_head})" if detail_head else "")
        )

    return TelemetryHealthResult(
        passed=not reasons,
        halt_reason=halt,
        reasons=reasons,
        digest=d,
    )


# ---------------------------------------------------------------------------
# DB ingest (optional — delegates to db.py)
# ---------------------------------------------------------------------------

def ingest_sidecar_to_db(
    conn,
    build_id: str | None,
    mph_path: Path,
    *,
    events: list[dict] | None = None,
) -> int:
    """Load a telemetry sidecar and store its events in the DB.

    If `events` is provided, uses them directly (caller already parsed);
    otherwise reads the sidecar next to mph_path. Returns the number of
    events stored.

    Import of db is local to avoid circular imports when db.py is
    extended to call back into telemetry utilities.
    """
    from comsol_support.db import store_telemetry_events

    if events is None:
        events = read_telemetry_sidecar(mph_path)
    if not events:
        return 0
    return store_telemetry_events(
        conn, build_id=build_id, output_path=str(mph_path), events=events,
    )


def _discover_sidecar_mphs(path: Path) -> list[Path]:
    """Return .mph paths whose telemetry sidecar is present under `path`.

    Gating is on *sidecar* existence, not .mph existence — a halted or
    partial build may leave a sidecar without a valid .mph, and the
    telemetry is still worth ingesting.

    Accepts:
      - a .mph path (its sidecar is ingested iff the sidecar exists);
      - a ".mph.telemetry.jsonl" sidecar path (ingested directly);
      - a directory (walked for "*.mph.telemetry.jsonl" files).
    """
    path = Path(path)
    mphs: list[Path] = []
    if path.is_dir():
        for sidecar in sorted(path.rglob("*.mph.telemetry.jsonl")):
            mphs.append(Path(str(sidecar)[: -len(".telemetry.jsonl")]))
        return mphs
    # File-like input. Accept either the sidecar directly or a .mph that
    # has one.
    name = path.name
    if name.endswith(".mph.telemetry.jsonl"):
        if path.exists():
            mphs.append(Path(str(path)[: -len(".telemetry.jsonl")]))
    elif name.endswith(".mph"):
        if sidecar_path_for(path).exists():
            mphs.append(path)
    return mphs


def ingest_from_path(
    conn,
    path: Path,
    *,
    build_id: str | None = None,
    clear_existing: bool = True,
) -> dict:
    """Ingest telemetry sidecars at `path` (file or directory) into the DB.

    Idempotent when a build_id is supplied: if clear_existing is True
    (the default), existing rows for that build_id are removed before
    inserting. Without a build_id, ingestion is purely additive — run
    duplicate ingests at your own risk.

    Returns a small summary dict: {"mph_count": N, "event_count": M}.
    """
    from comsol_support.db import clear_telemetry_for_build

    mphs = _discover_sidecar_mphs(Path(path))
    if build_id and clear_existing and mphs:
        clear_telemetry_for_build(conn, build_id)

    total = 0
    for mph in mphs:
        total += ingest_sidecar_to_db(conn, build_id, mph)
    return {"mph_count": len(mphs), "event_count": total}


# ---------------------------------------------------------------------------
# CLI adapter
# ---------------------------------------------------------------------------

def add_ingest_telemetry_subparser(subs) -> None:
    """Attach the `ingest-telemetry` subparser. Called from cli.build_parser()."""
    p = subs.add_parser(
        "ingest-telemetry",
        help=(
            "Ingest telemetry sidecars (*.mph.telemetry.jsonl) into the "
            "SQLite telemetry_events table."
        ),
    )
    p.add_argument(
        "path",
        help=(
            "Path to a .mph (its sidecar will be ingested), a "
            ".mph.telemetry.jsonl sidecar, or a directory to walk."
        ),
    )
    p.add_argument("--db", required=True, help="Path to the SQLite database.")
    p.add_argument(
        "--build-id",
        default=None,
        help=(
            "Optional build_id to associate with ingested events. When set, "
            "existing rows for that build_id are cleared first (idempotent)."
        ),
    )


def cmd_ingest_telemetry(args) -> int:
    """CLI handler for `ingest-telemetry`, dispatched from cli.py."""
    import sys as _sys
    from comsol_support.db import init_db

    path = Path(args.path)
    conn = init_db(Path(args.db))
    try:
        summary = ingest_from_path(conn, path, build_id=args.build_id)
    finally:
        conn.close()

    if summary["mph_count"] == 0:
        print(
            f"ingest-telemetry: no telemetry sidecars found at {path}",
            file=_sys.stderr,
        )
        return 2

    print(
        f"ingested {summary['event_count']} event(s) from "
        f"{summary['mph_count']} sidecar(s) into {args.db}"
        + (f" under build_id={args.build_id}" if args.build_id else "")
    )
    return 0
