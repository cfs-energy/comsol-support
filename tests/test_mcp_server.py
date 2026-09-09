"""Tests for the MCP stdio server: tool dispatch, JSON-RPC protocol, config generation."""

import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

from comsol_support.db import (
    init_db,
    store_fragment,
    store_knowledge_row,
)
from comsol_support.mcp_server import (
    TOOLS,
    dispatch,
    handle_tool_call,
    read_message,
    write_message,
)


class TestReadWriteMessage(unittest.TestCase):
    """Test JSON-RPC Content-Length framing."""

    def test_write_read_roundtrip(self):
        buf = io.BytesIO()
        original_stdout = sys.stdout
        sys.stdout = type("FakeStdout", (), {"buffer": buf})()
        try:
            write_message({"jsonrpc": "2.0", "id": 1, "result": "ok"})
        finally:
            sys.stdout = original_stdout

        buf.seek(0)
        original_stdin = sys.stdin
        sys.stdin = type("FakeStdin", (), {"buffer": buf})()
        try:
            msg = read_message()
        finally:
            sys.stdin = original_stdin

        self.assertEqual(msg["id"], 1)
        self.assertEqual(msg["result"], "ok")

    def test_read_eof_returns_none(self):
        buf = io.BytesIO(b"")
        original_stdin = sys.stdin
        sys.stdin = type("FakeStdin", (), {"buffer": buf})()
        try:
            msg = read_message()
        finally:
            sys.stdin = original_stdin
        self.assertIsNone(msg)

    def test_read_no_content_length_returns_none(self):
        buf = io.BytesIO(b"X-Custom: value\r\n\r\n")
        original_stdin = sys.stdin
        sys.stdin = type("FakeStdin", (), {"buffer": buf})()
        try:
            msg = read_message()
        finally:
            sys.stdin = original_stdin
        self.assertIsNone(msg)

    def test_write_correct_content_length(self):
        buf = io.BytesIO()
        original_stdout = sys.stdout
        sys.stdout = type("FakeStdout", (), {"buffer": buf})()
        try:
            write_message({"test": True})
        finally:
            sys.stdout = original_stdout

        output = buf.getvalue().decode("utf-8")
        body_json = json.dumps({"test": True})
        self.assertIn(f"Content-Length: {len(body_json.encode('utf-8'))}", output)


class TestHandleToolCall(unittest.TestCase):
    """Test tool dispatch against a real temp DB."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        cls.db_path = Path(cls.tmp) / "test_mcp.db"
        cls.conn = init_db(cls.db_path)

        store_knowledge_row(cls.conn, "GeomSequence",
                            method="create", signature="String tag, String type",
                            stage="geometry", module="geom",
                            description="Create geometry primitive")
        store_knowledge_row(cls.conn, "PhysicsNode",
                            method="set", signature="String prop, Object val",
                            stage="physics", module="physics",
                            description="Set physics property")

        store_fragment(cls.conn, "geometry", "block_3d",
                       'model.component("comp1").geom("geom1").create("blk1", "Block");',
                       description="Create a 3D block")


    def test_get_fragment_found(self):
        result = handle_tool_call(self.conn, "get_fragment", {"fragment_id": 1})
        self.assertIn("block_3d", result)
        self.assertIn("Block", result)

    def test_get_fragment_not_found(self):
        result = handle_tool_call(self.conn, "get_fragment", {"fragment_id": 9999})
        self.assertIn("Fragment not found", result)

    def test_get_fragment_invalid_id(self):
        result = handle_tool_call(self.conn, "get_fragment",
                                  {"fragment_id": "not_an_int"})
        self.assertIn("Invalid fragment_id", result)

    def test_search_api_returns_results(self):
        result = handle_tool_call(self.conn, "search_api",
                                  {"query": "GeomSequence"})
        self.assertIn("GeomSequence", result)
        self.assertIn("create", result)

    def test_search_api_with_stage_filter(self):
        result = handle_tool_call(self.conn, "search_api",
                                  {"query": "set OR create", "stage": "geometry"})
        self.assertIn("GeomSequence", result)
        self.assertNotIn("PhysicsNode", result)

    def test_search_api_no_match(self):
        result = handle_tool_call(self.conn, "search_api",
                                  {"query": "nonexistent_class_xyz"})
        self.assertIn("No matching API entries", result)

    def test_search_api_bad_fts_query(self):
        result = handle_tool_call(self.conn, "search_api",
                                  {"query": "AND OR NOT *()^"})
        self.assertIsInstance(result, str)
        self.assertNotIn("Traceback", result)


class TestToolDispatchUnknown(unittest.TestCase):
    """Test unknown tool name handling."""

    def test_unknown_tool(self):
        tmp = tempfile.mkdtemp()
        conn = init_db(Path(tmp) / "test.db")
        result = handle_tool_call(conn, "nonexistent_tool", {})
        self.assertIn("Unknown tool: nonexistent_tool", result)
        conn.close()


class TestMcpJsonRpcProtocol(unittest.TestCase):
    """Test JSON-RPC protocol dispatch logic."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        cls.conn = init_db(Path(cls.tmp) / "proto.db")

    def test_initialize_response(self):
        msg = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
        resp = dispatch(self.conn, msg)
        self.assertEqual(resp["id"], 1)
        result = resp["result"]
        self.assertIn("protocolVersion", result)
        self.assertIn("capabilities", result)
        self.assertIn("tools", result["capabilities"])
        self.assertEqual(result["serverInfo"]["name"], "comsol-support-tools")

    def test_notification_initialized_no_response(self):
        msg = {"jsonrpc": "2.0", "method": "notifications/initialized"}
        resp = dispatch(self.conn, msg)
        self.assertIsNone(resp)

    def test_tools_list_response(self):
        msg = {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}
        resp = dispatch(self.conn, msg)
        tools = resp["result"]["tools"]
        self.assertEqual(len(tools), 10)

    def test_tools_list_tool_names(self):
        msg = {"jsonrpc": "2.0", "id": 3, "method": "tools/list"}
        resp = dispatch(self.conn, msg)
        names = {t["name"] for t in resp["result"]["tools"]}
        expected = {"get_fragment", "search_api", "search_fragments",
                    "get_solver_telemetry", "search_telemetry",
                    "get_solver_results",
                    "list_physics_options", "list_studies_for_physics",
                    "list_default_plots_for_physics", "search_gotchas"}
        self.assertEqual(names, expected)

    def test_tool_call_response_format(self):
        msg = {
            "jsonrpc": "2.0", "id": 4, "method": "tools/call",
            "params": {"name": "search_api", "arguments": {"query": "test"}},
        }
        resp = dispatch(self.conn, msg)
        content = resp["result"]["content"]
        self.assertEqual(len(content), 1)
        self.assertEqual(content[0]["type"], "text")
        self.assertIsInstance(content[0]["text"], str)

    def test_unknown_method_error(self):
        msg = {"jsonrpc": "2.0", "id": 5, "method": "unknown/method"}
        resp = dispatch(self.conn, msg)
        self.assertIn("error", resp)
        self.assertEqual(resp["error"]["code"], -32601)

    def test_unknown_method_no_id_no_response(self):
        msg = {"jsonrpc": "2.0", "method": "unknown/method"}
        resp = dispatch(self.conn, msg)
        self.assertIsNone(resp)

    def test_tool_call_exception_returns_error_response(self):
        """A tool handler crash must produce a JSON-RPC error, not kill
        the server loop."""
        from unittest.mock import patch
        msg = {
            "jsonrpc": "2.0", "id": 9, "method": "tools/call",
            "params": {"name": "search_api", "arguments": {"query": "x"}},
        }
        with patch("comsol_support.mcp_server.handle_tool_call",
                   side_effect=RuntimeError("db exploded")):
            resp = dispatch(self.conn, msg)
        self.assertIn("error", resp)
        self.assertEqual(resp["error"]["code"], -32603)
        self.assertIn("db exploded", resp["error"]["message"])

    def test_search_gotchas_tool_dispatch(self):
        """The gotchas catalog is reachable through the MCP surface."""
        msg = {
            "jsonrpc": "2.0", "id": 10, "method": "tools/call",
            "params": {"name": "search_gotchas",
                       "arguments": {"query": "disconnect"}},
        }
        resp = dispatch(self.conn, msg)
        text = resp["result"]["content"][0]["text"]
        self.assertIn("G-DISCONNECT-HANGS", text)

    def test_search_gotchas_no_match(self):
        msg = {
            "jsonrpc": "2.0", "id": 11, "method": "tools/call",
            "params": {"name": "search_gotchas",
                       "arguments": {"query": "zzz-no-such-symptom"}},
        }
        resp = dispatch(self.conn, msg)
        text = resp["result"]["content"][0]["text"]
        self.assertIn("No known gotchas", text)


class TestRetiredBuildCatalogTools(unittest.TestCase):
    """The build-catalog tools were removed with the orchestrator (2026-08);
    a call to one must be answered like any unknown tool, not crash."""

    def test_retired_tools_absent_and_unknown(self):
        conn = init_db(Path(tempfile.mkdtemp()) / "r.db")
        names = {t["name"] for t in TOOLS}
        for retired in ("search_builds", "get_build", "list_build_catalog"):
            self.assertNotIn(retired, names)
            self.assertIn("Unknown tool", handle_tool_call(conn, retired, {}))


class TestToolsConstant(unittest.TestCase):
    """Verify the TOOLS constant structure."""

    def test_tools_count(self):
        self.assertEqual(len(TOOLS), 10)

    def test_all_tools_have_input_schema(self):
        for tool in TOOLS:
            self.assertIn("inputSchema", tool)
            self.assertEqual(tool["inputSchema"]["type"], "object")

    def test_all_tools_have_description(self):
        for tool in TOOLS:
            self.assertIsInstance(tool["description"], str)
            self.assertGreater(len(tool["description"]), 10)

    def test_fragment_id_is_integer_type(self):
        frag_tool = next(t for t in TOOLS if t["name"] == "get_fragment")
        schema = frag_tool["inputSchema"]["properties"]["fragment_id"]
        self.assertEqual(schema["type"], "integer")


if __name__ == "__main__":
    unittest.main()
