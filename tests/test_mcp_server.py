#!/usr/bin/env python3
"""Minimal MCP server over stdio for E2E testing.

Implements the MCP protocol (JSON-RPC 2.0 over stdin/stdout):
- initialize / initialized handshake
- tools/list — exposes 3 test tools
- tools/call — executes them
- ping

Tools:
  - echo(message) — returns the message back
  - add(a, b)     — returns a + b
  - weather(city)  — returns fake weather data
"""

import json
import sys
from datetime import datetime


# --- Tool definitions (MCP format) ---

TOOLS = [
    {
        "name": "echo",
        "description": "Echo a message back — useful for testing connectivity.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "message": {"type": "string", "description": "The message to echo"},
            },
            "required": ["message"],
        },
    },
    {
        "name": "add",
        "description": "Add two numbers together and return the sum.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "a": {"type": "number", "description": "First number"},
                "b": {"type": "number", "description": "Second number"},
            },
            "required": ["a", "b"],
        },
    },
    {
        "name": "weather",
        "description": "Get current weather for a city (fake data for testing).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "city": {"type": "string", "description": "City name"},
            },
            "required": ["city"],
        },
    },
]


# --- Tool execution ---

def execute_tool(name: str, arguments: dict) -> list[dict]:
    """Execute a tool and return MCP content blocks."""
    if name == "echo":
        msg = arguments.get("message", "")
        return [{"type": "text", "text": f"Echo: {msg}"}]

    if name == "add":
        a = arguments.get("a", 0)
        b = arguments.get("b", 0)
        result = a + b
        return [{"type": "text", "text": f"{a} + {b} = {result}"}]

    if name == "weather":
        city = arguments.get("city", "Unknown")
        return [
            {
                "type": "text",
                "text": json.dumps(
                    {
                        "city": city,
                        "temperature": 22,
                        "unit": "celsius",
                        "condition": "sunny",
                        "humidity": 45,
                        "wind_speed": 12,
                        "timestamp": datetime.now().isoformat(),
                    }
                ),
            }
        ]

    return [{"type": "text", "text": f"Unknown tool: {name}"}]


# --- JSON-RPC dispatch ---

def handle_request(msg: dict) -> dict | None:
    """Handle a JSON-RPC 2.0 request. Returns response or None for notifications."""
    method = msg.get("method", "")
    params = msg.get("params", {})
    msg_id = msg.get("id")

    # Notifications (no id) — no response expected
    if msg_id is None:
        return None

    if method == "initialize":
        return {
            "jsonrpc": "2.0",
            "id": msg_id,
            "result": {
                "protocolVersion": "2024-11-05",
                "serverInfo": {
                    "name": "test-mcp-server",
                    "version": "1.0.0",
                },
                "capabilities": {
                    "tools": {"listChanged": False},
                },
            },
        }

    if method == "ping":
        return {"jsonrpc": "2.0", "id": msg_id, "result": {}}

    if method == "tools/list":
        return {
            "jsonrpc": "2.0",
            "id": msg_id,
            "result": {"tools": TOOLS},
        }

    if method == "tools/call":
        tool_name = params.get("name", "")
        arguments = params.get("arguments", {})
        content = execute_tool(tool_name, arguments)
        return {
            "jsonrpc": "2.0",
            "id": msg_id,
            "result": {"content": content, "isError": False},
        }

    if method == "resources/list":
        return {"jsonrpc": "2.0", "id": msg_id, "result": {"resources": []}}

    if method == "prompts/list":
        return {"jsonrpc": "2.0", "id": msg_id, "result": {"prompts": []}}

    # Unknown method
    return {
        "jsonrpc": "2.0",
        "id": msg_id,
        "error": {
            "code": -32601,
            "message": f"Method not found: {method}",
        },
    }


# --- Main loop ---

def main():
    """Read JSON-RPC messages from stdin, write responses to stdout."""
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue

        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue

        response = handle_request(msg)
        if response is not None:
            sys.stdout.write(json.dumps(response) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
