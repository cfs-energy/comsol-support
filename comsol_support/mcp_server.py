#!/usr/bin/env python3
"""Minimal MCP server exposing COMSOL build tools over stdio.

Launched as a subprocess by Claude Code via --mcp-config.
Reads the SQLite database path from the COMSOL_DB environment variable.

Protocol: JSON-RPC 2.0 over stdio with Content-Length framing (MCP standard).

Tools exposed (10):
  - search_api: search the COMSOL Java API knowledge base (CLI: `comsol-support search`)
  - get_fragment / search_fragments: corpus-mined code fragments
  - get_solver_telemetry / get_solver_results / search_telemetry: solver
    telemetry keyed by a caller-chosen run id (build_id)
  - list_physics_options / list_studies_for_physics /
    list_default_plots_for_physics: native-interface catalog
  - search_gotchas: the known-gotchas catalog
"""

import json
import os
import sys
from pathlib import Path

# Ensure comsol_support is importable regardless of launch cwd
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from comsol_support.db import (
    get_fragment,
    get_telemetry_events,
    init_db,
    search_fragments,
    search_knowledge,
    search_telemetry,
)
from comsol_support.knowledge_format import format_knowledge_row


def read_message():
    """Read a JSON-RPC message with Content-Length header from stdin."""
    headers = {}
    while True:
        line = sys.stdin.buffer.readline()
        if not line:
            return None
        decoded = line.decode("utf-8")
        if decoded.strip() == "":
            break
        if ":" in decoded:
            key, value = decoded.split(":", 1)
            headers[key.strip()] = value.strip()

    content_length = int(headers.get("Content-Length", 0))
    if content_length == 0:
        return None

    body = sys.stdin.buffer.read(content_length)
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        # Framing is broken (truncated stream / corrupt client) — treat
        # as EOF for a clean shutdown rather than crashing with a
        # traceback; the byte stream cannot be re-synchronized.
        return None


def write_message(msg):
    """Write a JSON-RPC message with Content-Length header to stdout."""
    body = json.dumps(msg).encode("utf-8")
    header = f"Content-Length: {len(body)}\r\n\r\n"
    sys.stdout.buffer.write(header.encode("utf-8"))
    sys.stdout.buffer.write(body)
    sys.stdout.buffer.flush()


TOOLS = [
    {
        "name": "get_fragment",
        "description": (
            "Retrieve a COMSOL Java code fragment by ID for Tier A composition. "
            "Returns the fragment's Java code, stage, tier, pattern name, "
            "and corpus frequency."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "fragment_id": {
                    "type": "integer",
                    "description": "The fragment ID to retrieve.",
                },
            },
            "required": ["fragment_id"],
        },
    },
    {
        "name": "search_api",
        "description": (
            "Search the COMSOL Java API knowledge base "
            "(Javadoc classes, methods, property keys). "
            "Optionally filter by build stage for scoped retrieval."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Search query for API classes, methods, or property keys.",
                },
                "stage": {
                    "type": "string",
                    "description": (
                        "Optional. Filter to knowledge relevant to this build stage "
                        "(e.g. geometry, physics, mesh)."
                    ),
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum results (default 10, max 50).",
                    "default": 10,
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "get_solver_telemetry",
        "description": (
            "Retrieve structured solver/run events captured from a past "
            "or in-flight build. Events include run_start, build_done, "
            "solve_start, solver_heartbeat (live-progress beats every "
            "~2s during study.run), solve_done, result_probe, "
            "result_global, save_done, partial_save, halt. Pass since_id "
            "to poll for new events on an active build without re-reading "
            "history."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "build_id": {
                    "type": "string",
                    "description": "The build ID whose telemetry to retrieve.",
                },
                "event_type": {
                    "type": "string",
                    "description": (
                        "Optional. Filter to a single event_type "
                        "(e.g. 'halt', 'solve_done', 'partial_save')."
                    ),
                },
                "since_id": {
                    "type": "integer",
                    "description": (
                        "Optional. Return only events with id strictly "
                        "greater than this value. Use the id of the last "
                        "event from the previous call to tail an active "
                        "build."
                    ),
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum events (default 200, max 2000).",
                    "default": 200,
                },
            },
            "required": ["build_id"],
        },
    },
    {
        "name": "get_solver_results",
        "description": (
            "Return the structured numerical results extracted after a "
            "successful solve: probe time histories and global "
            "evaluations defined in the postprocessing stage. Returns "
            "the most recent value per result tag, plus any "
            "result_error entries from tags whose extraction failed. "
            "Use this as the 'give me the answers' view — distinct from "
            "get_solver_telemetry which returns the full event stream."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "build_id": {
                    "type": "string",
                    "description": "The build ID whose results to retrieve.",
                },
                "limit": {
                    "type": "integer",
                    "description": (
                        "Maximum result events (default 200, max 2000)."
                    ),
                    "default": 200,
                },
            },
            "required": ["build_id"],
        },
    },
    {
        "name": "search_telemetry",
        "description": (
            "FTS search over the payload JSON of telemetry events across "
            "all runs (build_id is the caller-chosen run key). Useful for "
            "finding prior runs that halted with "
            "a particular reason, errored on a specific study, or emitted "
            "a payload matching arbitrary text. Matches event_type, "
            "payload_json, build_id, and output_path."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": (
                        "FTS5 search query (keywords, halt reasons, "
                        "study tags, etc.)."
                    ),
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum results (default 20, max 100).",
                    "default": 20,
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "list_physics_options",
        "description": (
            "List COMSOL native physics interfaces for a domain keyword, "
            "ranked by setup cost (lowest first). Use this BEFORE writing "
            "any model.physics().create() call when the intent names a "
            "standard domain (heat, flow, stress, em_low, em_high, "
            "acoustics, transport, chem, custom). The top-ranked result "
            "is almost always the right starting point."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "domain": {
                    "type": "string",
                    "description": (
                        "Canonical domain keyword or an intent keyword that "
                        "resolves to one via the synonym map "
                        "(e.g. 'heat', 'thermal', 'temperature' all → heat)."
                    ),
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum results (default 5, max 20).",
                    "default": 5,
                },
            },
            "required": ["domain"],
        },
    },
    {
        "name": "list_studies_for_physics",
        "description": (
            "Return the study types that a native physics interface "
            "ships with as defaults (e.g., 'ht' → stat, time). Use "
            "before creating a Study to pick a preset step rather than "
            "hand-configuring a SolverSequence."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "physics_tag": {
                    "type": "string",
                    "description": "Physics interface tag_prefix (e.g. 'ht', 'spf', 'solid').",
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum results (default 5, max 20).",
                    "default": 5,
                },
            },
            "required": ["physics_tag"],
        },
    },
    {
        "name": "list_default_plots_for_physics",
        "description": (
            "Return the plot and probe types that ship as defaults for "
            "a native physics interface (e.g., 'ht' → surf, line, "
            "probe_dom). Use before creating a plot group to pick a "
            "preset rather than a custom dataset + feature combination."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "physics_tag": {
                    "type": "string",
                    "description": "Physics interface tag_prefix (e.g. 'ht', 'spf', 'solid').",
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum results (default 5, max 20).",
                    "default": 5,
                },
            },
            "required": ["physics_tag"],
        },
    },
    {
        "name": "search_fragments",
        "description": (
            "Search corpus-mined COMSOL Java code fragments by stage and pattern. "
            "Returns fragments ordered by corpus frequency (most common first). "
            "Use for Tier B exemplar discovery during stage execution."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "stage": {
                    "type": "string",
                    "description": (
                        "Build stage to search in "
                        "(geometry, physics, mesh, studies, etc.)."
                    ),
                },
                "pattern_name": {
                    "type": "string",
                    "description": (
                        "Optional. Filter by feature pattern name "
                        "(e.g. Block, HeatTransfer)."
                    ),
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum results (default 5, max 20).",
                    "default": 5,
                },
            },
            "required": ["stage"],
        },
    },
    {
        "name": "search_gotchas",
        "description": (
            "Search the known-gotchas catalog (docs/known-gotchas.md) of "
            "confirmed COMSOL + headless-solve pitfalls by symptom "
            "keyword. Returns whole matching entries with their stable "
            "G-DOMAIN-PHENOMENON ids. Query this before authoring a new "
            "diagnostic harness for a recurring failure."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": (
                        "Symptom keyword or phrase (literal substring, "
                        "case-insensitive)."
                    ),
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum entries returned (default 3, max 10).",
                    "default": 3,
                },
            },
            "required": ["query"],
        },
    },
]


def handle_tool_call(conn, tool_name, arguments):
    """Dispatch a tool call and return the result text."""
    if tool_name == "get_fragment":
        try:
            fragment_id = int(arguments.get("fragment_id", 0))
        except (ValueError, TypeError):
            return "Invalid fragment_id: must be an integer."
        try:
            frag = get_fragment(conn, fragment_id)
        except Exception:
            frag = None

        if frag:
            return (
                f"Fragment {frag['id']}: {frag.get('pattern_name', '')}\n"
                f"  Stage: {frag.get('stage', '')} | "
                f"Tier: {frag.get('tier', '')} | "
                f"Corpus freq: {frag.get('corpus_freq', 0)}\n"
                f"  Description: {frag.get('description', '') or '(none)'}\n"
                f"Java code:\n{frag.get('java_code', '')}"
            )
        return f"Fragment not found: {fragment_id}"

    elif tool_name == "search_api":
        query = arguments.get("query", "")
        stage = arguments.get("stage")
        limit = min(arguments.get("limit", 10), 50)
        try:
            rows = search_knowledge(conn, query, stage=stage, limit=limit)
        except Exception:
            return f"Search error: query '{query}' could not be processed. Try simpler keywords."

        if rows:
            return "\n\n".join(format_knowledge_row(r) for r in rows)
        return "No matching API entries found."

    elif tool_name == "get_solver_telemetry":
        build_id = arguments.get("build_id", "")
        event_type = arguments.get("event_type") or None
        raw_since = arguments.get("since_id")
        try:
            since_id = int(raw_since) if raw_since is not None else None
        except (TypeError, ValueError):
            since_id = None
        limit = min(int(arguments.get("limit", 200) or 200), 2000)
        try:
            rows = get_telemetry_events(
                conn, build_id, event_type=event_type,
                since_id=since_id, limit=limit,
            )
        except Exception:
            return f"Telemetry query error for build '{build_id}'."

        if not rows:
            filt = f" (event_type={event_type})" if event_type else ""
            sinc = f" (since_id={since_id})" if since_id is not None else ""
            return (f"No telemetry events for build '{build_id}'"
                    f"{filt}{sinc}.")

        parts = []
        for r in rows:
            payload = r.get("payload_json") or "{}"
            parts.append(
                f"[id={r.get('id', '')} {r.get('wall_ms', '')} ms] "
                f"{r.get('event_type', '')}: {payload}"
            )
        last_id = rows[-1].get("id", "")
        header = (
            f"{len(rows)} telemetry event(s) for build {build_id}"
            + (f" (event_type={event_type})" if event_type else "")
            + (f" (since_id={since_id})" if since_id is not None else "")
            + f" [last_id={last_id}]:"
        )
        return header + "\n" + "\n".join(parts)

    elif tool_name == "get_solver_results":
        build_id = arguments.get("build_id", "")
        limit = min(int(arguments.get("limit", 200) or 200), 2000)
        try:
            rows = get_telemetry_events(
                conn, build_id,
                event_types=("result_probe", "result_global", "result_error"),
                limit=limit,
            )
        except Exception:
            return f"Results query error for build '{build_id}'."

        if not rows:
            return (f"No solver results for build '{build_id}'. "
                    "(Build may be unsolved, in-flight, or have no "
                    "postprocessing probes/globals defined.)")

        probes: dict[str, dict] = {}
        globals_: dict[str, dict] = {}
        errors: list[dict] = []
        for r in rows:
            try:
                p = json.loads(r.get("payload_json") or "{}")
            except (TypeError, json.JSONDecodeError):
                continue
            et = r.get("event_type")
            # Prefer the user-facing name; fall back to the opaque tag.
            key = p.get("name") or p.get("tag") or ""
            if et == "result_probe" and key:
                probes[key] = p
            elif et == "result_global" and key:
                globals_[key] = p
            elif et == "result_error":
                errors.append(p)

        def _unit_suffix(p):
            u = p.get("unit") or ""
            return f" {u}" if u else ""

        parts = [f"Solver results for build {build_id}:"]
        if globals_:
            parts.append(f"\nGlobals ({len(globals_)}):")
            for name, p in globals_.items():
                shape = p.get("shape") or []
                series = p.get("series") or []
                first = ""
                if series and isinstance(series, list) and series:
                    row0 = series[0]
                    if isinstance(row0, list) and row0:
                        first = str(row0[0])
                parts.append(
                    f"  {name}: {first or '(empty)'}{_unit_suffix(p)} "
                    f"[shape={shape}]"
                )
        if probes:
            parts.append(f"\nProbes ({len(probes)}):")
            for name, p in probes.items():
                shape = p.get("shape") or []
                unit = p.get("unit") or ""
                unit_tag = f" [{unit}]" if unit else ""
                parts.append(f"  {name}{unit_tag}: shape={shape}")
        if errors:
            parts.append(f"\nExtraction errors ({len(errors)}):")
            for p in errors:
                parts.append(
                    f"  [{p.get('kind', '?')}] {p.get('tag', '')}: "
                    f"{p.get('error', '')}"
                )
        return "\n".join(parts)

    elif tool_name == "search_telemetry":
        query = arguments.get("query", "")
        limit = min(int(arguments.get("limit", 20) or 20), 100)
        try:
            rows = search_telemetry(conn, query, limit=limit)
        except Exception:
            return (f"Telemetry search error: query '{query}' could not be "
                    "processed. Try simpler keywords.")

        if not rows:
            return f"No telemetry events match '{query}'."

        parts = []
        for r in rows:
            parts.append(
                f"build={r.get('build_id') or '(none)'} "
                f"out={r.get('output_path') or '(none)'} "
                f"[{r.get('wall_ms', '')} ms] "
                f"{r.get('event_type', '')}: "
                f"{r.get('payload_json') or '{}'}"
            )
        return "\n".join(parts)

    elif tool_name == "list_physics_options":
        from comsol_support.native_catalog import (
            list_interfaces, lookup_domain,
        )
        domain_arg = (arguments.get("domain") or "").strip()
        limit = min(int(arguments.get("limit", 5) or 5), 20)
        if not domain_arg:
            return "Provide a domain keyword (e.g. 'heat', 'flow', 'stress')."
        # Accept either a canonical domain OR an intent keyword that
        # resolves via the synonym map. Both paths return the same rows.
        canonical = lookup_domain(conn, domain_arg) or domain_arg
        try:
            rows = list_interfaces(
                conn, domain_keyword=canonical,
                stage="physics", limit=limit,
            )
        except Exception:
            return f"Catalog query error for domain '{domain_arg}'."
        if not rows:
            return (
                f"No native physics interfaces cataloged for domain "
                f"'{domain_arg}'. This intent may be novel — consider a "
                "custom PDE with a brief override rationale, or rephrase "
                "the domain if the intent matches a standard category."
            )
        parts = [
            f"Native physics options for domain '{canonical}' "
            f"({len(rows)} ranked by setup cost):"
        ]
        for r in rows:
            auto = ", ".join(r.get("auto_features") or []) or "(none)"
            parts.append(
                f"  [rank={r['setup_cost_rank']} freq={r['corpus_freq']}] "
                f"{r['tag_prefix']} ({r['class_name']})"
                f"\n    {r.get('notes') or ''}"
                f"\n    auto-created features: {auto}"
                f"\n    default studies: "
                f"{', '.join(r.get('default_studies') or []) or '(none)'}"
                f" | default plots: "
                f"{', '.join(r.get('default_plots') or []) or '(none)'}"
            )
        return "\n".join(parts)

    elif tool_name == "list_studies_for_physics":
        from comsol_support.native_catalog import list_studies_for_physics
        phys = (arguments.get("physics_tag") or "").strip()
        limit = min(int(arguments.get("limit", 5) or 5), 20)
        if not phys:
            return "Provide a physics_tag (e.g. 'ht', 'spf', 'solid')."
        try:
            rows = list_studies_for_physics(
                conn, physics_tag=phys, limit=limit,
            )
        except Exception:
            return f"Catalog query error for physics_tag '{phys}'."
        if not rows:
            return (
                f"No study defaults cataloged for physics '{phys}'. "
                "Use 'stat' for steady-state or 'time' for transient as a "
                "reasonable fallback."
            )
        parts = [f"Default studies for physics '{phys}':"]
        for r in rows:
            parts.append(
                f"  [rank={r['setup_cost_rank']} freq={r['corpus_freq']}] "
                f"{r['tag_prefix']} ({r['class_name']})"
                f"\n    {r.get('notes') or ''}"
            )
        return "\n".join(parts)

    elif tool_name == "list_default_plots_for_physics":
        from comsol_support.native_catalog import list_plots_for_physics
        phys = (arguments.get("physics_tag") or "").strip()
        limit = min(int(arguments.get("limit", 5) or 5), 20)
        if not phys:
            return "Provide a physics_tag (e.g. 'ht', 'spf', 'solid')."
        try:
            rows = list_plots_for_physics(
                conn, physics_tag=phys, limit=limit,
            )
        except Exception:
            return f"Catalog query error for physics_tag '{phys}'."
        if not rows:
            return (
                f"No plot defaults cataloged for physics '{phys}'. "
                "Use 'surf' (surface plot) as a reasonable starting point."
            )
        parts = [f"Default plots/probes for physics '{phys}':"]
        for r in rows:
            parts.append(
                f"  [rank={r['setup_cost_rank']} freq={r['corpus_freq']}] "
                f"{r['tag_prefix']} ({r['class_name']})"
                f"\n    {r.get('notes') or ''}"
            )
        return "\n".join(parts)

    elif tool_name == "search_fragments":
        stage = arguments.get("stage", "")
        pattern = arguments.get("pattern_name")
        limit = min(arguments.get("limit", 5), 20)
        try:
            results = search_fragments(
                conn, stage, pattern_name=pattern, limit=limit
            )
        except Exception:
            return f"Fragment search error for stage '{stage}'."

        if results:
            parts = []
            for f in results:
                co_occ = ""
                if f.get("co_occurrence_json"):
                    try:
                        co_data = json.loads(f["co_occurrence_json"])
                        top_items = list(co_data.items())[:5]
                        co_occ = " | Co-occurs with: " + ", ".join(
                            f"{k} ({v}x)" for k, v in top_items
                        )
                    except (json.JSONDecodeError, TypeError):
                        pass
                parts.append(
                    f"Fragment {f['id']}: {f.get('pattern_name', '')} "
                    f"[{f.get('stage', '')}, Tier {f.get('tier', '')}, "
                    f"freq={f.get('corpus_freq', 0)}]"
                    f"{co_occ}\n"
                    f"  {f.get('description', '') or ''}\n"
                    f"Code:\n{f.get('java_code', '')}"
                )
            return "\n\n---\n\n".join(parts)
        return f"No fragments found for stage '{stage}'."

    elif tool_name == "search_gotchas":
        from comsol_support.gotcha_search import search_gotchas
        query = arguments.get("query", "")
        limit = min(int(arguments.get("limit", 3) or 3), 10)
        try:
            hits = search_gotchas(query)[:limit]
        except ValueError as e:
            return f"Search error: {e}"
        if hits:
            return "\n\n---\n\n".join(g.body for g in hits)
        return f"No known gotchas matched {query!r}."

    return f"Unknown tool: {tool_name}"


def dispatch(conn, msg):
    """Dispatch a single JSON-RPC message. Returns response dict or None."""
    method = msg.get("method", "")
    msg_id = msg.get("id")

    if method == "initialize":
        return {
            "jsonrpc": "2.0",
            "id": msg_id,
            "result": {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "comsol-support-tools", "version": "1.0.0"},
            },
        }

    elif method == "notifications/initialized":
        return None

    elif method == "tools/list":
        return {
            "jsonrpc": "2.0",
            "id": msg_id,
            "result": {"tools": TOOLS},
        }

    elif method == "tools/call":
        params = msg.get("params", {})
        tool_name = params.get("name", "")
        arguments = params.get("arguments", {})
        try:
            text = handle_tool_call(conn, tool_name, arguments)
        except Exception as e:  # noqa: BLE001 — one bad call must not kill the server
            return {
                "jsonrpc": "2.0",
                "id": msg_id,
                "error": {
                    "code": -32603,
                    "message": f"{tool_name} failed: {e}",
                },
            }
        return {
            "jsonrpc": "2.0",
            "id": msg_id,
            "result": {
                "content": [{"type": "text", "text": text}],
            },
        }

    elif msg_id is not None:
        return {
            "jsonrpc": "2.0",
            "id": msg_id,
            "error": {"code": -32601, "message": f"Unknown method: {method}"},
        }

    return None


def main():
    db_path = os.environ.get("COMSOL_DB", "")
    if not db_path:
        print("Error: COMSOL_DB environment variable is required.", file=sys.stderr)
        sys.exit(1)

    conn = init_db(Path(db_path))

    while True:
        msg = read_message()
        if msg is None:
            break

        response = dispatch(conn, msg)
        if response is not None:
            write_message(response)

    conn.close()


if __name__ == "__main__":
    main()
