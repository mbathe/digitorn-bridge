"""Path sandbox enforcement for MCP tool calls."""

from __future__ import annotations

import os
from typing import Any


_PATH_FIELD_NAMES = frozenset({
    "path", "file_path", "filepath", "filename",
    "dir", "directory", "folder", "cwd", "workdir", "workspace",
    "source", "src", "target", "dst", "destination", "output",
    "input_path", "output_path",
})


def _looks_like_path(value: str) -> bool:
    if not value or len(value) < 2:
        return False
    if "://" in value:
        return False
    if value.startswith("/"):
        if value in ("/dev/null", "/dev/stdin", "/dev/stdout", "/dev/stderr"):
            return False
        return True
    if value.startswith("~"):
        return True
    if value.startswith("\\"):
        return True
    if len(value) >= 2 and value[1] == ":" and value[0].isalpha():
        return True
    return False


def _collect_path_fields_from_schema(schema: Any) -> set[str]:
    out: set[str] = set()
    if not isinstance(schema, dict):
        return out
    props = schema.get("properties") or {}
    if isinstance(props, dict):
        for name, sub in props.items():
            if not isinstance(name, str):
                continue
            if name.lower() in _PATH_FIELD_NAMES:
                out.add(name)
                continue
            if isinstance(sub, dict):
                fmt = sub.get("format")
                desc = sub.get("description", "")
                if fmt == "path":
                    out.add(name)
                    continue
                if isinstance(desc, str) and (
                    "absolute path" in desc.lower()
                    or "file path" in desc.lower()
                    or "directory path" in desc.lower()
                ):
                    out.add(name)
                    continue
                nested = _collect_path_fields_from_schema(sub)
                del nested
    return out


def enforce_args(
    args: Any,
    *,
    policy: Any,
    schema_path_fields: set[str] | None = None,
) -> None:
    """Walk args and call policy.enforce on every path-like value."""
    schema_path_fields = schema_path_fields or set()
    _walk(args, policy, schema_path_fields, parent_key=None)


def _walk(
    node: Any,
    policy: Any,
    schema_fields: set[str],
    parent_key: str | None,
) -> None:
    if isinstance(node, dict):
        for k, v in node.items():
            _walk(v, policy, schema_fields, parent_key=str(k) if k is not None else None)
        return
    if isinstance(node, (list, tuple)):
        for v in node:
            _walk(v, policy, schema_fields, parent_key=parent_key)
        return
    if isinstance(node, str):
        is_schema_path = bool(
            parent_key and parent_key in schema_fields
        )
        is_value_path = _looks_like_path(node)
        if is_schema_path or is_value_path:
            policy.enforce(node)
        return
