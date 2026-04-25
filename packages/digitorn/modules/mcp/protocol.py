"""MCP protocol — JSON-RPC 2.0 message types and MCP-specific methods.

Implements the Model Context Protocol (MCP) wire format:
- JSON-RPC 2.0 request/response/notification
- MCP lifecycle: initialize, initialized, ping
- MCP capabilities: tools/list, tools/call, resources/list, resources/read,
  prompts/list, prompts/get

All messages are serialized as JSON. Transport is responsible for framing
(newline-delimited for stdio, SSE for HTTP).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any


@dataclass
class JsonRpcRequest:
    """A JSON-RPC 2.0 request."""

    method: str
    params: dict[str, Any] | None = None
    id: int | str | None = None

    def to_json(self) -> str:
        msg: dict[str, Any] = {"jsonrpc": "2.0", "method": self.method}
        if self.params is not None:
            msg["params"] = self.params
        if self.id is not None:
            msg["id"] = self.id
        return json.dumps(msg)


@dataclass
class JsonRpcResponse:
    """A JSON-RPC 2.0 response."""

    id: int | str | None
    result: Any = None
    error: JsonRpcError | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> JsonRpcResponse:
        error = None
        if "error" in data:
            err = data["error"]
            error = JsonRpcError(
                code=err.get("code", -1),
                message=err.get("message", "Unknown error"),
                data=err.get("data"),
            )
        return cls(
            id=data.get("id"),
            result=data.get("result"),
            error=error,
        )


@dataclass
class JsonRpcError:
    """A JSON-RPC 2.0 error object."""

    code: int
    message: str
    data: Any = None


@dataclass
class JsonRpcNotification:
    """A JSON-RPC 2.0 notification (no id, no response expected)."""

    method: str
    params: dict[str, Any] | None = None

    def to_json(self) -> str:
        msg: dict[str, Any] = {"jsonrpc": "2.0", "method": self.method}
        if self.params is not None:
            msg["params"] = self.params
        return json.dumps(msg)


@dataclass
class MCPToolDef:
    """An MCP tool definition (from tools/list)."""

    name: str
    description: str = ""
    input_schema: dict[str, Any] = field(default_factory=dict)
    annotations: dict[str, Any] = field(default_factory=dict)
    meta: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> MCPToolDef:
        return cls(
            name=data.get("name", ""),
            description=data.get("description", ""),
            input_schema=data.get("inputSchema", {}),
            annotations=data.get("annotations", {}),
            meta=data.get("_meta", {}),
        )


@dataclass
class MCPResourceDef:
    """An MCP resource definition (from resources/list)."""

    uri: str
    name: str = ""
    description: str = ""
    mime_type: str = ""

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> MCPResourceDef:
        return cls(
            uri=data.get("uri", ""),
            name=data.get("name", ""),
            description=data.get("description", ""),
            mime_type=data.get("mimeType", ""),
        )


@dataclass
class MCPPromptDef:
    """An MCP prompt definition (from prompts/list)."""

    name: str
    description: str = ""
    arguments: list[MCPPromptArgument] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> MCPPromptDef:
        args = [
            MCPPromptArgument.from_dict(a)
            for a in data.get("arguments", [])
        ]
        return cls(
            name=data.get("name", ""),
            description=data.get("description", ""),
            arguments=args,
        )


@dataclass
class MCPPromptArgument:
    """An argument for an MCP prompt template."""

    name: str
    description: str = ""
    required: bool = False

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> MCPPromptArgument:
        return cls(
            name=data.get("name", ""),
            description=data.get("description", ""),
            required=data.get("required", False),
        )


@dataclass
class MCPToolResult:
    """Result from an MCP tools/call."""

    content: list[dict[str, Any]] = field(default_factory=list)
    is_error: bool = False

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> MCPToolResult:
        return cls(
            content=data.get("content", []),
            is_error=data.get("isError", False),
        )

    @property
    def text(self) -> str:
        """Extract text content from the result."""
        parts = []
        for item in self.content:
            if item.get("type") == "text":
                parts.append(item.get("text", ""))
            elif item.get("type") == "image":
                parts.append(f"[Image: {item.get('mimeType', 'unknown')}]")
            elif item.get("type") == "resource":
                res = item.get("resource", {})
                parts.append(f"[Resource: {res.get('uri', 'unknown')}]")
        return "\n".join(parts)


MCP_PROTOCOL_VERSION = "2024-11-05"

MCP_CLIENT_INFO = {
    "name": "digitorn",
    "version": "1.0.0",
}

MCP_CLIENT_CAPABILITIES: dict[str, Any] = {}


def build_initialize(request_id: int | str) -> JsonRpcRequest:
    """Build an initialize request."""
    return JsonRpcRequest(
        method="initialize",
        params={
            "protocolVersion": MCP_PROTOCOL_VERSION,
            "capabilities": MCP_CLIENT_CAPABILITIES,
            "clientInfo": MCP_CLIENT_INFO,
        },
        id=request_id,
    )


def build_initialized() -> JsonRpcNotification:
    """Build the initialized notification (sent after initialize response)."""
    return JsonRpcNotification(method="notifications/initialized")


def build_ping(request_id: int | str) -> JsonRpcRequest:
    """Build a ping request."""
    return JsonRpcRequest(method="ping", id=request_id)


def build_tools_list(request_id: int | str) -> JsonRpcRequest:
    """Build a tools/list request."""
    return JsonRpcRequest(method="tools/list", id=request_id)


def build_tools_call(
    request_id: int | str,
    name: str,
    arguments: dict[str, Any] | None = None,
) -> JsonRpcRequest:
    """Build a tools/call request."""
    params: dict[str, Any] = {"name": name}
    if arguments:
        params["arguments"] = arguments
    return JsonRpcRequest(method="tools/call", params=params, id=request_id)


def build_resources_list(request_id: int | str) -> JsonRpcRequest:
    """Build a resources/list request."""
    return JsonRpcRequest(method="resources/list", id=request_id)


def build_resources_read(
    request_id: int | str, uri: str
) -> JsonRpcRequest:
    """Build a resources/read request."""
    return JsonRpcRequest(
        method="resources/read",
        params={"uri": uri},
        id=request_id,
    )


def build_prompts_list(request_id: int | str) -> JsonRpcRequest:
    """Build a prompts/list request."""
    return JsonRpcRequest(method="prompts/list", id=request_id)


def build_prompts_get(
    request_id: int | str,
    name: str,
    arguments: dict[str, Any] | None = None,
) -> JsonRpcRequest:
    """Build a prompts/get request."""
    params: dict[str, Any] = {"name": name}
    if arguments:
        params["arguments"] = arguments
    return JsonRpcRequest(
        method="prompts/get", params=params, id=request_id
    )


def parse_tools_list(result: Any) -> list[MCPToolDef]:
    """Parse tools/list response result."""
    if not isinstance(result, dict):
        return []
    tools = result.get("tools", [])
    return [MCPToolDef.from_dict(t) for t in tools if isinstance(t, dict)]


def parse_resources_list(result: Any) -> list[MCPResourceDef]:
    """Parse resources/list response result."""
    if not isinstance(result, dict):
        return []
    resources = result.get("resources", [])
    return [MCPResourceDef.from_dict(r) for r in resources if isinstance(r, dict)]


def parse_prompts_list(result: Any) -> list[MCPPromptDef]:
    """Parse prompts/list response result."""
    if not isinstance(result, dict):
        return []
    prompts = result.get("prompts", [])
    return [MCPPromptDef.from_dict(p) for p in prompts if isinstance(p, dict)]


def parse_tool_result(result: Any) -> MCPToolResult:
    """Parse tools/call response result."""
    if not isinstance(result, dict):
        return MCPToolResult(content=[{"type": "text", "text": str(result)}])
    return MCPToolResult.from_dict(result)
