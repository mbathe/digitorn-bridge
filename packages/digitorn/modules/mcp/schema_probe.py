"""MCP API schema probing - auto-discover JSON structures at startup."""

from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger(__name__)

_META_KEYS = frozenset({
    "id", "object", "parent", "parent_id",
    "created_time", "last_edited_time",
    "created_by", "last_edited_by",
    "has_children", "archived", "in_trash",
    "url", "public_url", "request_id",
    "cover", "icon", "href", "plain_text",
    "next_cursor", "has_more",
    "developer_survey", "status",
})

_DEFAULT_STYLE_KEYS = frozenset({
    "color", "background_color", "is_toggleable", "is_column_list",
    "language",
})
_DEFAULT_STYLE_VALUES = frozenset({
    "default", False, "plain text", "",
})

_MAX_TEMPLATE_CHARS = 2500
_MAX_TOTAL_CHARS = 5000
_MAX_PROBES = 3

async def probe_mcp_server(
    pool: Any,
    server_id: str,
) -> dict[str, str]:
    """Probe an MCP server to discover API response structures."""
    entry = pool.get_server(server_id)
    if entry is None or entry.status != "connected":
        return {}

    if not _has_writer_tools(entry.tools):
        return {}

    sample_id = await _find_sample_id(pool, server_id, entry)
    if not sample_id:
        logger.debug("schema_probe: no sample resource for server=%s", server_id)
        return {}

    templates: dict[str, str] = {}
    total_chars = 0
    probed = 0

    getter_tools = _prioritize_getters(entry.tools)

    for tool in getter_tools:
        if probed >= _MAX_PROBES or total_chars >= _MAX_TOTAL_CHARS:
            break

        schema = tool.input_schema or {}
        required = set(schema.get("required", []))
        id_params = [p for p in required if p.endswith("_id") or p == "id"]
        if len(id_params) != 1:
            continue

        try:
            result = await pool.call_tool(
                server_id, tool.name, {id_params[0]: sample_id}
            )
            probed += 1

            if result.is_error or not result.text:
                continue

            template = _extract_template_from_response(result.text)
            if template:
                if len(template) > _MAX_TEMPLATE_CHARS:
                    template = template[:_MAX_TEMPLATE_CHARS] + "\n..."
                templates[tool.name] = template
                total_chars += len(template)
                logger.info(
                    "schema_probe: captured %s.%s (%d chars)",
                    server_id, tool.name, len(template),
                )
        except Exception as exc:
            logger.debug(
                "schema_probe: %s.%s failed: %s",
                server_id, tool.name, exc,
            )
            probed += 1

    return templates

def _has_writer_tools(tools: list[Any]) -> bool:
    for tool in tools:
        schema = tool.input_schema or {}
        props = schema.get("properties", {})
        for p_name in props:
            if p_name.endswith("_json"):
                return True
            desc = (props[p_name].get("description") or "").lower()
            if any(kw in desc for kw in ("json string", "json object", "json array")):
                return True
    return False

def _prioritize_getters(tools: list[Any]) -> list[Any]:
    getters = [t for t in tools if t.name.startswith("get_")]

    def _priority(tool: Any) -> int:
        name = tool.name.lower()
        if "children" in name or "block" in name:
            return 0
        if "page" in name:
            return 1
        if "database" in name:
            return 2
        return 3

    return sorted(getters, key=_priority)

async def _find_sample_id(
    pool: Any,
    server_id: str,
    entry: Any,
) -> str | None:
    for tool in entry.tools:
        if "search" not in tool.name.lower():
            continue
        schema = tool.input_schema or {}
        props = schema.get("properties", {})
        required = set(schema.get("required", []))

        call_params: dict[str, Any] = {}
        for p in required:
            p_schema = props.get(p, {})
            p_type = p_schema.get("type", "string")
            if p_type == "string":
                call_params[p] = ""
            elif p_type == "integer":
                call_params[p] = 1

        try:
            result = await pool.call_tool(server_id, tool.name, call_params)
            if not result.is_error and result.text:
                rid = _extract_first_id(result.text)
                if rid:
                    return rid
        except Exception:
            continue

    for tool in entry.tools:
        if not tool.name.startswith("list_"):
            continue
        schema = tool.input_schema or {}
        required = schema.get("required", [])
        if required:
            continue

        try:
            result = await pool.call_tool(server_id, tool.name, {})
            if not result.is_error and result.text:
                rid = _extract_first_id(result.text)
                if rid:
                    return rid
        except Exception:
            continue

    return None

def _extract_first_id(response_text: str) -> str | None:
    try:
        data = json.loads(response_text)
    except (json.JSONDecodeError, ValueError):
        return None

    if isinstance(data, dict):
        results = data.get("results", [])
        if isinstance(results, list) and results:
            first = results[0]
            if isinstance(first, dict):
                return first.get("id")
        if "id" in data:
            return data["id"]

    if isinstance(data, list) and data:
        first = data[0]
        if isinstance(first, dict):
            return first.get("id")

    return None

def _extract_template_from_response(response_text: str) -> str | None:
    if not response_text or not response_text.strip().startswith(("{", "[")):
        return None

    try:
        data = json.loads(response_text)
    except (json.JSONDecodeError, ValueError):
        return None

    if isinstance(data, dict):
        if "results" in data and isinstance(data["results"], list):
            items = data["results"]
            rich_items = [
                item for item in items
                if _has_content(item)
            ]
            selected = (rich_items or items)[:2]
            if not selected:
                return None
            cleaned = [_clean_structure(item) for item in selected]
            result = json.dumps(cleaned, indent=2, ensure_ascii=False)
            result = _fill_empty_content_arrays(result)
            return result

        cleaned = _clean_structure(data)
        result = json.dumps(cleaned, indent=2, ensure_ascii=False)
        return _fill_empty_content_arrays(result)

    if isinstance(data, list):
        items = data[:2]
        if not items:
            return None
        cleaned = [_clean_structure(item) for item in items]
        result = json.dumps(cleaned, indent=2, ensure_ascii=False)
        return _fill_empty_content_arrays(result)

    return None

def _has_content(item: Any) -> bool:
    if not isinstance(item, dict):
        return False
    for v in item.values():
        if isinstance(v, dict):
            for inner_v in v.values():
                if isinstance(inner_v, list) and inner_v:
                    return True
    return False

def _fill_empty_content_arrays(template_json: str) -> str:
    try:
        data = json.loads(template_json)
    except (json.JSONDecodeError, ValueError):
        return template_json

    _fill_empty_arrays_recursive(data)
    return json.dumps(data, indent=2, ensure_ascii=False)

def _fill_empty_arrays_recursive(value: Any, parent_has_type: bool = False) -> None:
    if isinstance(value, dict):
        has_type = "type" in value and isinstance(value.get("type"), str)

        for k, v in value.items():
            if isinstance(v, list) and not v and k != "type":
                if has_type or parent_has_type:
                    value[k] = [
                        {"type": "text", "text": {"content": "..."}}
                    ]
            elif isinstance(v, (dict, list)):
                _fill_empty_arrays_recursive(v, parent_has_type=has_type)
    elif isinstance(value, list):
        for item in value:
            if isinstance(item, (dict, list)):
                _fill_empty_arrays_recursive(item, parent_has_type)

def _clean_structure(
    value: Any,
    depth: int = 0,
    max_depth: int = 7,
) -> Any:
    if depth >= max_depth:
        return "..."

    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for k, v in value.items():
            if k in _META_KEYS:
                continue
            if k == "annotations" and isinstance(v, dict) and "bold" in v:
                continue
            cleaned = _clean_structure(v, depth + 1, max_depth)
            if cleaned is None:
                continue
            if k in _DEFAULT_STYLE_KEYS and cleaned in _DEFAULT_STYLE_VALUES:
                continue
            result[k] = cleaned
        return result

    if isinstance(value, list):
        if not value:
            return []
        first = _clean_structure(value[0], depth + 1, max_depth)
        return [first]

    if isinstance(value, str):
        if len(value) <= 30 and value.replace("_", "").replace("-", "").isalnum():
            return value
        return "..."

    if isinstance(value, (bool, int, float)) or value is None:
        return value

    return "..."
