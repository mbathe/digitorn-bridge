"""Pydantic parameter models for MCP module actions."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, field_validator

_SERVER_ID_PATTERN = r"^[a-z][a-z0-9_]*$"

class ConnectParams(BaseModel):
    """Connect to an MCP server."""

    server_id: str = Field(
        ...,
        min_length=1,
        max_length=128,
        pattern=_SERVER_ID_PATTERN,
        description="Unique name for this server (e.g. 'slack', 'github'). Lowercase alphanumeric + underscores only.",
    )
    transport: str = Field(
        default="stdio",
        description="Transport type: 'stdio', 'sse', or 'streamable_http'.",
    )
    command: str | None = Field(
        default=None,
        description="Command to run (stdio only, e.g. 'npx', 'python').",
    )
    args: list[str] = Field(
        default_factory=list,
        description="Command arguments (stdio only).",
    )
    env: dict[str, str] = Field(
        default_factory=dict,
        description="Environment variables for the subprocess (stdio only).",
    )
    url: str | None = Field(
        default=None,
        description="Server URL (sse/http only).",
    )
    headers: dict[str, str] = Field(
        default_factory=dict,
        description="HTTP headers (sse/http only).",
    )
    timeout: float = Field(
        default=30.0,
        ge=1.0,
        le=300.0,
        description="Request timeout in seconds.",
    )

class DisconnectParams(BaseModel):
    """Disconnect from an MCP server."""

    server_id: str = Field(
        ...,
        min_length=1,
        max_length=128,
        description="Server ID to disconnect.",
    )

class ReconnectParams(BaseModel):
    """Reconnect a failed MCP server."""

    server_id: str = Field(
        ...,
        min_length=1,
        max_length=128,
        description="Server ID to reconnect.",
    )

class ListServersParams(BaseModel):
    """List all connected MCP servers."""

class ListToolsParams(BaseModel):
    """List tools from a specific MCP server."""

    server_id: str = Field(
        ...,
        min_length=1,
        max_length=128,
        description="Server ID to list tools from.",
    )

class CallToolParams(BaseModel):
    """Call a tool on a specific MCP server."""

    server_id: str = Field(
        ...,
        min_length=1,
        max_length=128,
        description="Server ID where the tool lives.",
    )
    tool_name: str = Field(
        ...,
        min_length=1,
        max_length=256,
        description="Name of the tool to call.",
    )
    arguments: dict[str, Any] = Field(
        default_factory=dict,
        description="Arguments to pass to the tool.",
    )

class ListResourcesParams(BaseModel):
    """List resources from an MCP server."""

    server_id: str = Field(
        ...,
        min_length=1,
        max_length=128,
        description="Server ID to list resources from.",
    )

class ReadResourceParams(BaseModel):
    """Read a resource from an MCP server."""

    server_id: str = Field(
        ...,
        min_length=1,
        max_length=128,
        description="Server ID where the resource lives.",
    )
    uri: str = Field(
        ...,
        min_length=1,
        max_length=2048,
        description="Resource URI to read.",
    )

class ListPromptsParams(BaseModel):
    """List prompt templates from an MCP server."""

    server_id: str = Field(
        ...,
        min_length=1,
        max_length=128,
        description="Server ID to list prompts from.",
    )

class GetPromptParams(BaseModel):
    """Get a prompt template from an MCP server."""

    server_id: str = Field(
        ...,
        min_length=1,
        max_length=128,
        description="Server ID where the prompt lives.",
    )
    prompt_name: str = Field(
        ...,
        min_length=1,
        max_length=256,
        description="Name of the prompt template.",
    )
    arguments: dict[str, Any] = Field(
        default_factory=dict,
        description="Arguments for the prompt template.",
    )

class HealthCheckParams(BaseModel):
    """Health check an MCP server (or all if server_id is omitted)."""

    server_id: str | None = Field(
        default=None,
        max_length=128,
        description="Server ID to check. Omit for all servers.",
    )
