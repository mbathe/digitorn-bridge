"""Path sandbox enforcement for MCP tool calls.

MCP tools are executed by external servers we don't control — but we
DO control what we send. Before dispatching a tool call, we walk the
argument tree and enforce the agent's ``PathPolicy`` on every value
that looks like a path. Out-of-sandbox values raise
``PermissionDeniedError`` so the agent's MCP tool calls inherit the
same workdir confinement as native ``filesystem`` / ``workspace`` /
``shell`` actions.

Two layers cooperate:

  1. **Schema-driven** — when the MCP tool's input schema declares
     fields by a path-shaped name (``path``, ``file_path``,
     ``directory``, ``cwd``, ``source``, ``target``, ...), the
     interceptor knows to enforce on those fields directly.

  2. **Value-driven (filet de sécurité)** — every other string value
     in the args tree is sniffed: if it starts with ``/``, ``\\``,
     ``~``, or a Windows drive letter, AND doesn't look like a URL,
     it's enforced. Catches the long tail of tools whose schema
     doesn't annotate path fields explicitly.

The walk is recursive: nested dicts, lists, tuples are all covered.
Both layers are best-effort — they raise rather than mask. The agent
sees a clean ``PermissionDeniedError`` and learns to use relative
paths.
"""

from __future__ import annotations

import os
from typing import Any


# Field names that, when present in a JSON schema's properties OR an
# args dict, very likely carry a filesystem path. Tightened to common
# conventions — adding broader matches risks false positives on string
# fields that happen to be named ``name`` etc.
_PATH_FIELD_NAMES = frozenset({
    "path", "file_path", "filepath", "filename",
    "dir", "directory", "folder", "cwd", "workdir", "workspace",
    "source", "src", "target", "dst", "destination", "output",
    "input_path", "output_path",
})


def _looks_like_path(value: str) -> bool:
    """Cheap value-driven path heuristic.

    True when the string starts with a filesystem-path marker AND
    doesn't look like a URL / glob / pseudo-path. Used as the second
    layer when the schema doesn't tell us which fields are paths.
    """
    if not value or len(value) < 2:
        return False
    # URLs win — caller might pass an http:// path
    if "://" in value:
        return False
    # Absolute Unix / Git Bash
    if value.startswith("/"):
        # Common pseudo-paths the agent legitimately uses literally
        if value in ("/dev/null", "/dev/stdin", "/dev/stdout", "/dev/stderr"):
            return False
        return True
    # Tilde-rooted
    if value.startswith("~"):
        return True
    # Backslash-rooted (Windows network share / abs)
    if value.startswith("\\"):
        return True
    # Windows drive letter: ``X:`` or ``X:\``
    if len(value) >= 2 and value[1] == ":" and value[0].isalpha():
        return True
    return False


def _collect_path_fields_from_schema(schema: Any) -> set[str]:
    """Walk a JSON schema and return the field names that should be
    treated as paths. Layer 1 fuel.
    """
    out: set[str] = set()
    if not isinstance(schema, dict):
        return out
    # Standard JSON Schema: ``properties: {name: {...}}``
    props = schema.get("properties") or {}
    if isinstance(props, dict):
        for name, sub in props.items():
            if not isinstance(name, str):
                continue
            if name.lower() in _PATH_FIELD_NAMES:
                out.add(name)
                continue
            # Explicit format=path or description hint
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
                # Recurse into nested object schemas
                nested = _collect_path_fields_from_schema(sub)
                # Nested fields don't bubble up by name (we only care
                # about top-level for the enforce walk), so this is
                # informational at the moment.
                del nested
    return out


def enforce_args(
    args: Any,
    *,
    policy: Any,
    schema_path_fields: set[str] | None = None,
) -> None:
    """Walk ``args`` and call ``policy.enforce`` on every path-like
    value found. Raises ``PermissionDeniedError`` on the first violation.

    Layer 1 (schema): fields whose name is in ``schema_path_fields``
    are unconditionally enforced (no heuristic needed).

    Layer 2 (heuristic): every other string value is run through
    ``_looks_like_path``; matches are enforced.
    """
    schema_path_fields = schema_path_fields or set()
    _walk(args, policy, schema_path_fields, parent_key=None)


def _walk(
    node: Any,
    policy: Any,
    schema_fields: set[str],
    parent_key: str | None,
) -> None:
    """Recursive walker that calls ``policy.enforce`` on path-like leaves."""
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
            # ``policy.enforce`` raises PermissionDeniedError on
            # out-of-sandbox; we let it propagate so the agent sees
            # the structured error.
            policy.enforce(node)
        return
    # Other primitives (bool, int, None, float) skipped.
