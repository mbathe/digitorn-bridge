"""Tests — MCP Protocol: JSON-RPC types, MCP data types, message builders, parsers.

Covers:
- JsonRpcRequest serialization
- JsonRpcResponse deserialization (success + error)
- JsonRpcNotification serialization
- MCPToolDef, MCPResourceDef, MCPPromptDef, MCPPromptArgument from_dict
- MCPToolResult from_dict + text property
- All message builders (initialize, ping, tools, resources, prompts)
- All response parsers (tools_list, resources_list, prompts_list, tool_result)
"""
from __future__ import annotations

import json

import pytest

from digitorn.modules.mcp.protocol import (
    JsonRpcError,
    JsonRpcNotification,
    JsonRpcRequest,
    JsonRpcResponse,
    MCPPromptArgument,
    MCPPromptDef,
    MCPResourceDef,
    MCPToolDef,
    MCPToolResult,
    MCP_CLIENT_INFO,
    MCP_PROTOCOL_VERSION,
    build_initialize,
    build_initialized,
    build_ping,
    build_prompts_get,
    build_prompts_list,
    build_resources_list,
    build_resources_read,
    build_tools_call,
    build_tools_list,
    parse_prompts_list,
    parse_resources_list,
    parse_tool_result,
    parse_tools_list,
)


# ── JSON-RPC Request ─────────────────────────────────────────────────────


class TestJsonRpcRequest:
    def test_to_json_minimal(self):
        req = JsonRpcRequest(method="ping", id=1)
        data = json.loads(req.to_json())
        assert data == {"jsonrpc": "2.0", "method": "ping", "id": 1}

    def test_to_json_with_params(self):
        req = JsonRpcRequest(method="tools/call", params={"name": "foo"}, id=42)
        data = json.loads(req.to_json())
        assert data["params"] == {"name": "foo"}
        assert data["id"] == 42

    def test_to_json_no_id(self):
        req = JsonRpcRequest(method="test")
        data = json.loads(req.to_json())
        assert "id" not in data

    def test_to_json_no_params(self):
        req = JsonRpcRequest(method="test", id=1)
        data = json.loads(req.to_json())
        assert "params" not in data


# ── JSON-RPC Response ────────────────────────────────────────────────────


class TestJsonRpcResponse:
    def test_from_dict_success(self):
        resp = JsonRpcResponse.from_dict({
            "jsonrpc": "2.0",
            "id": 1,
            "result": {"tools": []},
        })
        assert resp.id == 1
        assert resp.result == {"tools": []}
        assert resp.error is None

    def test_from_dict_error(self):
        resp = JsonRpcResponse.from_dict({
            "jsonrpc": "2.0",
            "id": 2,
            "error": {"code": -32600, "message": "Invalid Request", "data": "extra"},
        })
        assert resp.id == 2
        assert resp.result is None
        assert resp.error is not None
        assert resp.error.code == -32600
        assert resp.error.message == "Invalid Request"
        assert resp.error.data == "extra"

    def test_from_dict_error_defaults(self):
        resp = JsonRpcResponse.from_dict({"id": 3, "error": {}})
        assert resp.error.code == -1
        assert resp.error.message == "Unknown error"
        assert resp.error.data is None

    def test_from_dict_no_id(self):
        resp = JsonRpcResponse.from_dict({"result": "ok"})
        assert resp.id is None
        assert resp.result == "ok"


# ── JSON-RPC Notification ────────────────────────────────────────────────


class TestJsonRpcNotification:
    def test_to_json_no_params(self):
        notif = JsonRpcNotification(method="notifications/initialized")
        data = json.loads(notif.to_json())
        assert data == {"jsonrpc": "2.0", "method": "notifications/initialized"}

    def test_to_json_with_params(self):
        notif = JsonRpcNotification(method="test", params={"key": "val"})
        data = json.loads(notif.to_json())
        assert data["params"] == {"key": "val"}


# ── MCP Data Types ───────────────────────────────────────────────────────


class TestMCPToolDef:
    def test_from_dict_full(self):
        tool = MCPToolDef.from_dict({
            "name": "post_message",
            "description": "Post a message to a channel",
            "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}},
        })
        assert tool.name == "post_message"
        assert tool.description == "Post a message to a channel"
        assert tool.input_schema["type"] == "object"

    def test_from_dict_minimal(self):
        tool = MCPToolDef.from_dict({})
        assert tool.name == ""
        assert tool.description == ""
        assert tool.input_schema == {}

    def test_from_dict_missing_schema(self):
        tool = MCPToolDef.from_dict({"name": "test"})
        assert tool.name == "test"
        assert tool.input_schema == {}


class TestMCPResourceDef:
    def test_from_dict_full(self):
        res = MCPResourceDef.from_dict({
            "uri": "file:///tmp/test.txt",
            "name": "test.txt",
            "description": "A test file",
            "mimeType": "text/plain",
        })
        assert res.uri == "file:///tmp/test.txt"
        assert res.name == "test.txt"
        assert res.mime_type == "text/plain"

    def test_from_dict_minimal(self):
        res = MCPResourceDef.from_dict({})
        assert res.uri == ""
        assert res.mime_type == ""


class TestMCPPromptDef:
    def test_from_dict_with_arguments(self):
        prompt = MCPPromptDef.from_dict({
            "name": "summarize",
            "description": "Summarize text",
            "arguments": [
                {"name": "text", "description": "Text to summarize", "required": True},
                {"name": "length", "description": "Max words"},
            ],
        })
        assert prompt.name == "summarize"
        assert len(prompt.arguments) == 2
        assert prompt.arguments[0].name == "text"
        assert prompt.arguments[0].required is True
        assert prompt.arguments[1].required is False

    def test_from_dict_no_arguments(self):
        prompt = MCPPromptDef.from_dict({"name": "hello"})
        assert prompt.arguments == []


class TestMCPPromptArgument:
    def test_from_dict(self):
        arg = MCPPromptArgument.from_dict({
            "name": "topic",
            "description": "The topic",
            "required": True,
        })
        assert arg.name == "topic"
        assert arg.required is True

    def test_defaults(self):
        arg = MCPPromptArgument.from_dict({})
        assert arg.name == ""
        assert arg.required is False


class TestMCPToolResult:
    def test_from_dict_text(self):
        result = MCPToolResult.from_dict({
            "content": [{"type": "text", "text": "hello world"}],
            "isError": False,
        })
        assert result.text == "hello world"
        assert result.is_error is False

    def test_from_dict_error(self):
        result = MCPToolResult.from_dict({
            "content": [{"type": "text", "text": "something broke"}],
            "isError": True,
        })
        assert result.is_error is True
        assert "something broke" in result.text

    def test_text_multiple_content(self):
        result = MCPToolResult(content=[
            {"type": "text", "text": "line1"},
            {"type": "image", "mimeType": "image/png"},
            {"type": "resource", "resource": {"uri": "file:///test"}},
            {"type": "text", "text": "line2"},
        ])
        text = result.text
        assert "line1" in text
        assert "line2" in text
        assert "[Image: image/png]" in text
        assert "[Resource: file:///test]" in text

    def test_text_empty(self):
        result = MCPToolResult(content=[])
        assert result.text == ""

    def test_from_dict_defaults(self):
        result = MCPToolResult.from_dict({})
        assert result.content == []
        assert result.is_error is False


# ── Message Builders ─────────────────────────────────────────────────────


class TestMessageBuilders:
    def test_build_initialize(self):
        req = build_initialize(1)
        assert req.method == "initialize"
        assert req.id == 1
        assert req.params["protocolVersion"] == MCP_PROTOCOL_VERSION
        assert req.params["clientInfo"] == MCP_CLIENT_INFO

    def test_build_initialized(self):
        notif = build_initialized()
        assert notif.method == "notifications/initialized"
        assert notif.params is None

    def test_build_ping(self):
        req = build_ping(5)
        assert req.method == "ping"
        assert req.id == 5

    def test_build_tools_list(self):
        req = build_tools_list(10)
        assert req.method == "tools/list"
        assert req.id == 10

    def test_build_tools_call_with_args(self):
        req = build_tools_call(11, "post_message", {"text": "hi"})
        assert req.method == "tools/call"
        assert req.params["name"] == "post_message"
        assert req.params["arguments"] == {"text": "hi"}

    def test_build_tools_call_no_args(self):
        req = build_tools_call(12, "list_channels")
        assert req.method == "tools/call"
        assert req.params["name"] == "list_channels"
        assert "arguments" not in req.params

    def test_build_resources_list(self):
        req = build_resources_list(20)
        assert req.method == "resources/list"

    def test_build_resources_read(self):
        req = build_resources_read(21, "file:///tmp/x")
        assert req.method == "resources/read"
        assert req.params["uri"] == "file:///tmp/x"

    def test_build_prompts_list(self):
        req = build_prompts_list(30)
        assert req.method == "prompts/list"

    def test_build_prompts_get_with_args(self):
        req = build_prompts_get(31, "summarize", {"length": "short"})
        assert req.method == "prompts/get"
        assert req.params["name"] == "summarize"
        assert req.params["arguments"] == {"length": "short"}

    def test_build_prompts_get_no_args(self):
        req = build_prompts_get(32, "hello")
        assert req.params["name"] == "hello"
        assert "arguments" not in req.params


# ── Response Parsers ─────────────────────────────────────────────────────


class TestResponseParsers:
    def test_parse_tools_list(self):
        tools = parse_tools_list({
            "tools": [
                {"name": "a", "description": "Tool A", "inputSchema": {"type": "object"}},
                {"name": "b", "description": "Tool B"},
            ]
        })
        assert len(tools) == 2
        assert tools[0].name == "a"
        assert tools[1].name == "b"

    def test_parse_tools_list_empty(self):
        assert parse_tools_list({}) == []
        assert parse_tools_list({"tools": []}) == []

    def test_parse_tools_list_not_dict(self):
        assert parse_tools_list("invalid") == []
        assert parse_tools_list(None) == []

    def test_parse_tools_list_skips_non_dict_items(self):
        tools = parse_tools_list({"tools": [{"name": "ok"}, "bad", 42]})
        assert len(tools) == 1

    def test_parse_resources_list(self):
        resources = parse_resources_list({
            "resources": [
                {"uri": "file:///a", "name": "a.txt", "mimeType": "text/plain"},
            ]
        })
        assert len(resources) == 1
        assert resources[0].uri == "file:///a"

    def test_parse_resources_list_empty(self):
        assert parse_resources_list({}) == []
        assert parse_resources_list(None) == []

    def test_parse_prompts_list(self):
        prompts = parse_prompts_list({
            "prompts": [
                {"name": "greet", "description": "Greeting", "arguments": [
                    {"name": "name", "required": True},
                ]},
            ]
        })
        assert len(prompts) == 1
        assert prompts[0].name == "greet"
        assert prompts[0].arguments[0].required is True

    def test_parse_prompts_list_empty(self):
        assert parse_prompts_list({}) == []
        assert parse_prompts_list(None) == []

    def test_parse_tool_result_dict(self):
        result = parse_tool_result({
            "content": [{"type": "text", "text": "done"}],
            "isError": False,
        })
        assert result.text == "done"
        assert result.is_error is False

    def test_parse_tool_result_non_dict(self):
        result = parse_tool_result("raw string")
        assert result.text == "raw string"
        assert result.is_error is False

    def test_parse_tool_result_none(self):
        result = parse_tool_result(None)
        assert result.text == "None"
