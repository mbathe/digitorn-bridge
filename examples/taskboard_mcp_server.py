#!/usr/bin/env python3
"""Task Board MCP server — realistic structured content for testing.

Simulates a project management API similar to Notion/Linear/Airtable:
- Tasks have typed content blocks (paragraph, checklist, heading)
- Writer tools accept JSON string params (like Notion's `_json` pattern)
- Getter tools return structured responses (for schema probing)

This tests the full structural hints pipeline:
  probe → template extraction → prompt injection → LLM writes correct JSON

Tools:
  READERS (for probing):
  - search_tasks(query?)       — search/list all tasks
  - get_task(task_id)          — get a task with its properties
  - get_task_content(task_id)  — get content blocks of a task

  WRITERS (with _json params):
  - create_task(title, properties_json?, content_json?)
  - update_task(task_id, properties_json?, content_json?)
  - append_content(task_id, blocks_json)
"""

import json
import sys
import uuid
from datetime import datetime, timezone


# ---------------------------------------------------------------------------
# In-memory storage
# ---------------------------------------------------------------------------

_TASKS: dict[str, dict] = {}


def _seed_data() -> None:
    """Create sample data so probing finds something."""
    task_id = "task-001"
    _TASKS[task_id] = {
        "id": task_id,
        "title": "Setup CI/CD pipeline",
        "status": "in_progress",
        "priority": "high",
        "assignee": "alice",
        "created_at": "2025-01-15T10:00:00Z",
        "updated_at": "2025-01-16T14:30:00Z",
        "properties": {
            "status": {"type": "select", "select": {"name": "In Progress"}},
            "priority": {"type": "select", "select": {"name": "High"}},
            "assignee": {"type": "person", "person": {"name": "Alice", "email": "alice@example.com"}},
            "due_date": {"type": "date", "date": {"start": "2025-02-01"}},
            "tags": {"type": "multi_select", "multi_select": [
                {"name": "devops"},
                {"name": "infrastructure"},
            ]},
            "estimate": {"type": "number", "number": 8},
        },
        "content": [
            {
                "type": "heading",
                "heading": {"level": 2, "text": "Pipeline Requirements"},
            },
            {
                "type": "paragraph",
                "paragraph": {
                    "rich_text": [
                        {"type": "text", "text": {"content": "We need to set up GitHub Actions for automated testing and deployment."}},
                    ],
                },
            },
            {
                "type": "checklist",
                "checklist": {
                    "items": [
                        {"text": "Configure build step", "checked": True},
                        {"text": "Add unit test runner", "checked": True},
                        {"text": "Setup deployment to staging", "checked": False},
                        {"text": "Add production deploy gate", "checked": False},
                    ],
                },
            },
            {
                "type": "paragraph",
                "paragraph": {
                    "rich_text": [
                        {"type": "text", "text": {"content": "Target: all PRs should run tests in under 5 minutes."}},
                    ],
                },
            },
        ],
    }

    task2_id = "task-002"
    _TASKS[task2_id] = {
        "id": task2_id,
        "title": "Write API documentation",
        "status": "todo",
        "priority": "medium",
        "assignee": "bob",
        "created_at": "2025-01-16T09:00:00Z",
        "updated_at": "2025-01-16T09:00:00Z",
        "properties": {
            "status": {"type": "select", "select": {"name": "Todo"}},
            "priority": {"type": "select", "select": {"name": "Medium"}},
            "assignee": {"type": "person", "person": {"name": "Bob", "email": "bob@example.com"}},
            "tags": {"type": "multi_select", "multi_select": [
                {"name": "documentation"},
            ]},
        },
        "content": [
            {
                "type": "paragraph",
                "paragraph": {
                    "rich_text": [
                        {"type": "text", "text": {"content": "Document all REST endpoints with examples."}},
                    ],
                },
            },
        ],
    }


_seed_data()


# ---------------------------------------------------------------------------
# Tool definitions
# ---------------------------------------------------------------------------

TOOLS = [
    # --- Readers ---
    {
        "name": "search_tasks",
        "description": "Search tasks by query string. Returns matching tasks with their properties.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Search query (matches title and content). Leave empty to list all.",
                },
            },
        },
    },
    {
        "name": "get_task",
        "description": "Get a task by ID, including all properties.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "The task ID"},
            },
            "required": ["task_id"],
        },
    },
    {
        "name": "get_task_content",
        "description": "Get the content blocks of a task (paragraphs, headings, checklists).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "The task ID"},
            },
            "required": ["task_id"],
        },
    },
    # --- Writers ---
    {
        "name": "create_task",
        "description": (
            "Create a new task.\n\n"
            "Args:\n"
            "  title (str): Task title\n"
            "  properties_json (str): JSON string of task properties. "
            "Use the exact structure returned by get_task.\n"
            "  content_json (str): JSON string of content blocks (array). "
            "Use the exact structure returned by get_task_content.\n"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "Task title"},
                "properties_json": {
                    "type": "string",
                    "description": (
                        "JSON string with task properties. "
                        "Must follow the exact structure from get_task response."
                    ),
                },
                "content_json": {
                    "type": "string",
                    "description": (
                        "JSON string array of content blocks. "
                        "Must follow the exact structure from get_task_content response."
                    ),
                },
            },
            "required": ["title"],
        },
    },
    {
        "name": "update_task",
        "description": (
            "Update an existing task's properties and/or content.\n\n"
            "Args:\n"
            "  task_id (str): The task ID to update\n"
            "  properties_json (str): JSON string of properties to update. "
            "Use the exact structure returned by get_task.\n"
            "  content_json (str): JSON string of content blocks to replace. "
            "Use the exact structure returned by get_task_content.\n"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "The task ID to update"},
                "properties_json": {
                    "type": "string",
                    "description": (
                        "JSON string with task properties to update. "
                        "Must follow the exact structure from get_task response."
                    ),
                },
                "content_json": {
                    "type": "string",
                    "description": (
                        "JSON string array of content blocks to replace. "
                        "Must follow the exact structure from get_task_content response."
                    ),
                },
            },
            "required": ["task_id"],
        },
    },
    {
        "name": "append_content",
        "description": (
            "Append content blocks to an existing task.\n\n"
            "Args:\n"
            "  task_id (str): The task ID\n"
            "  blocks_json (str): JSON string array of content blocks to append. "
            "Use the exact structure returned by get_task_content.\n"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "The task ID"},
                "blocks_json": {
                    "type": "string",
                    "description": (
                        "JSON string array of content blocks to append. "
                        "Must follow the exact structure from get_task_content response."
                    ),
                },
            },
            "required": ["task_id", "blocks_json"],
        },
    },
]


# ---------------------------------------------------------------------------
# Tool execution
# ---------------------------------------------------------------------------

def execute_tool(name: str, arguments: dict) -> list[dict]:
    """Execute a tool and return MCP content blocks."""

    if name == "search_tasks":
        query = (arguments.get("query") or "").lower()
        results = []
        for task in _TASKS.values():
            if not query or query in task["title"].lower():
                results.append({
                    "id": task["id"],
                    "title": task["title"],
                    "status": task["status"],
                    "priority": task["priority"],
                })
        return [{"type": "text", "text": json.dumps({"results": results}, indent=2)}]

    if name == "get_task":
        task_id = arguments.get("task_id", "")
        task = _TASKS.get(task_id)
        if not task:
            return [{"type": "text", "text": json.dumps({"error": f"Task not found: {task_id}"})}]
        # Return without content (use get_task_content for that)
        result = {k: v for k, v in task.items() if k != "content"}
        return [{"type": "text", "text": json.dumps(result, indent=2)}]

    if name == "get_task_content":
        task_id = arguments.get("task_id", "")
        task = _TASKS.get(task_id)
        if not task:
            return [{"type": "text", "text": json.dumps({"error": f"Task not found: {task_id}"})}]
        return [{"type": "text", "text": json.dumps(task["content"], indent=2)}]

    if name == "create_task":
        title = arguments.get("title", "Untitled")
        task_id = f"task-{uuid.uuid4().hex[:6]}"
        now = datetime.now(timezone.utc).isoformat()

        # Parse properties
        properties = {}
        props_json = arguments.get("properties_json")
        if props_json:
            try:
                properties = json.loads(props_json)
            except json.JSONDecodeError as e:
                return [{"type": "text", "text": json.dumps({
                    "error": f"Invalid properties_json: {e}",
                    "hint": "Must be a valid JSON string. See get_task response for structure.",
                })}]

        # Parse content
        content = []
        content_json = arguments.get("content_json")
        if content_json:
            try:
                content = json.loads(content_json)
            except json.JSONDecodeError as e:
                return [{"type": "text", "text": json.dumps({
                    "error": f"Invalid content_json: {e}",
                    "hint": "Must be a valid JSON array. See get_task_content response for structure.",
                })}]

        # Validate content structure
        validation = _validate_content(content)
        if validation:
            return [{"type": "text", "text": json.dumps({
                "error": f"Invalid content structure: {validation}",
                "hint": "Use the exact structure from get_task_content. See template.",
            })}]

        # Validate properties structure
        validation = _validate_properties(properties)
        if validation:
            return [{"type": "text", "text": json.dumps({
                "error": f"Invalid properties structure: {validation}",
                "hint": "Use the exact structure from get_task. See template.",
            })}]

        _TASKS[task_id] = {
            "id": task_id,
            "title": title,
            "status": properties.get("status", {}).get("select", {}).get("name", "todo"),
            "priority": properties.get("priority", {}).get("select", {}).get("name", "medium"),
            "assignee": properties.get("assignee", {}).get("person", {}).get("name", ""),
            "created_at": now,
            "updated_at": now,
            "properties": properties,
            "content": content,
        }
        return [{"type": "text", "text": json.dumps({"created": task_id, "title": title})}]

    if name == "update_task":
        task_id = arguments.get("task_id", "")
        task = _TASKS.get(task_id)
        if not task:
            return [{"type": "text", "text": json.dumps({"error": f"Task not found: {task_id}"})}]

        props_json = arguments.get("properties_json")
        if props_json:
            try:
                props = json.loads(props_json)
                validation = _validate_properties(props)
                if validation:
                    return [{"type": "text", "text": json.dumps({
                        "error": f"Invalid properties structure: {validation}",
                    })}]
                task["properties"].update(props)
            except json.JSONDecodeError as e:
                return [{"type": "text", "text": json.dumps({"error": f"Invalid properties_json: {e}"})}]

        content_json = arguments.get("content_json")
        if content_json:
            try:
                content = json.loads(content_json)
                validation = _validate_content(content)
                if validation:
                    return [{"type": "text", "text": json.dumps({
                        "error": f"Invalid content structure: {validation}",
                    })}]
                task["content"] = content
            except json.JSONDecodeError as e:
                return [{"type": "text", "text": json.dumps({"error": f"Invalid content_json: {e}"})}]

        task["updated_at"] = datetime.now(timezone.utc).isoformat()
        return [{"type": "text", "text": json.dumps({"updated": task_id})}]

    if name == "append_content":
        task_id = arguments.get("task_id", "")
        task = _TASKS.get(task_id)
        if not task:
            return [{"type": "text", "text": json.dumps({"error": f"Task not found: {task_id}"})}]

        blocks_json = arguments.get("blocks_json", "")
        try:
            blocks = json.loads(blocks_json)
        except json.JSONDecodeError as e:
            return [{"type": "text", "text": json.dumps({
                "error": f"Invalid blocks_json: {e}",
                "hint": "Must be a valid JSON array. See get_task_content response for structure.",
            })}]

        validation = _validate_content(blocks)
        if validation:
            return [{"type": "text", "text": json.dumps({
                "error": f"Invalid content structure: {validation}",
                "hint": "Use the exact structure from get_task_content.",
            })}]

        task["content"].extend(blocks)
        task["updated_at"] = datetime.now(timezone.utc).isoformat()
        return [{"type": "text", "text": json.dumps({
            "appended": len(blocks),
            "total_blocks": len(task["content"]),
        })}]

    return [{"type": "text", "text": json.dumps({"error": f"Unknown tool: {name}"})}]


# ---------------------------------------------------------------------------
# Validation — rejects invented structures
# ---------------------------------------------------------------------------

_VALID_BLOCK_TYPES = {"heading", "paragraph", "checklist"}
_VALID_PROPERTY_TYPES = {"select", "multi_select", "person", "date", "number", "text"}


def _validate_content(content: list) -> str | None:
    """Validate content blocks match the expected structure.

    Returns error message or None if valid.
    """
    if not isinstance(content, list):
        return "content must be a JSON array"

    for i, block in enumerate(content):
        if not isinstance(block, dict):
            return f"block[{i}] must be an object"
        btype = block.get("type")
        if btype not in _VALID_BLOCK_TYPES:
            return (
                f"block[{i}].type = '{btype}' is invalid. "
                f"Valid types: {sorted(_VALID_BLOCK_TYPES)}. "
                f"Do NOT invent types like 'text', 'block', etc."
            )
        # Check structure matches type
        if btype not in block:
            return f"block[{i}] has type='{btype}' but no '{btype}' key"

        inner = block[btype]
        if btype == "paragraph":
            if "rich_text" not in inner:
                return (
                    f"block[{i}].paragraph must have 'rich_text' (array), "
                    f"NOT 'text'. Use: {{\"type\": \"paragraph\", "
                    f"\"paragraph\": {{\"rich_text\": [{{\"type\": \"text\", "
                    f"\"text\": {{\"content\": \"...\"}}}}]}}}}"
                )
            if not isinstance(inner["rich_text"], list):
                return f"block[{i}].paragraph.rich_text must be an array"

        elif btype == "heading":
            if "text" not in inner and "level" not in inner:
                return f"block[{i}].heading must have 'level' and 'text'"

        elif btype == "checklist":
            if "items" not in inner:
                return f"block[{i}].checklist must have 'items' (array)"

        # Reject invented keys
        allowed_top = {"type", btype}
        extra = set(block.keys()) - allowed_top
        if extra:
            return (
                f"block[{i}] has unexpected keys: {extra}. "
                f"Only 'type' and '{btype}' are allowed at top level."
            )

    return None


def _validate_properties(properties: dict) -> str | None:
    """Validate properties match the expected structure."""
    if not isinstance(properties, dict):
        return "properties must be a JSON object"

    for key, value in properties.items():
        if not isinstance(value, dict):
            return f"property '{key}' must be an object with 'type' field"
        ptype = value.get("type")
        if ptype not in _VALID_PROPERTY_TYPES:
            return (
                f"property '{key}'.type = '{ptype}' is invalid. "
                f"Valid types: {sorted(_VALID_PROPERTY_TYPES)}"
            )

    return None


# ---------------------------------------------------------------------------
# JSON-RPC dispatch
# ---------------------------------------------------------------------------

def handle_request(msg: dict) -> dict | None:
    method = msg.get("method", "")
    params = msg.get("params", {})
    msg_id = msg.get("id")

    if msg_id is None:
        return None

    if method == "initialize":
        return {
            "jsonrpc": "2.0",
            "id": msg_id,
            "result": {
                "protocolVersion": "2024-11-05",
                "serverInfo": {"name": "taskboard-mcp", "version": "1.0.0"},
                "capabilities": {"tools": {"listChanged": False}},
            },
        }

    if method == "ping":
        return {"jsonrpc": "2.0", "id": msg_id, "result": {}}

    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": msg_id, "result": {"tools": TOOLS}}

    if method == "tools/call":
        tool_name = params.get("name", "")
        arguments = params.get("arguments", {})
        content = execute_tool(tool_name, arguments)
        return {"jsonrpc": "2.0", "id": msg_id, "result": {"content": content, "isError": False}}

    if method == "resources/list":
        return {"jsonrpc": "2.0", "id": msg_id, "result": {"resources": []}}

    if method == "prompts/list":
        return {"jsonrpc": "2.0", "id": msg_id, "result": {"prompts": []}}

    return {
        "jsonrpc": "2.0",
        "id": msg_id,
        "error": {"code": -32601, "message": f"Method not found: {method}"},
    }


def main():
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
