"""OpenAI strict-mode normalisation for tool schemas."""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


# Keywords whose value is a single sub-schema.
_SUBSCHEMA_KEYS_SINGLE = ("not", "if", "then", "else", "contains", "propertyNames")
# Keywords whose value is a list of sub-schemas.
_SUBSCHEMA_KEYS_LIST = ("anyOf", "oneOf", "allOf", "prefixItems")
# Keywords whose value is a dict {name: sub-schema}.
_SUBSCHEMA_KEYS_MAP = ("properties", "patternProperties", "$defs", "definitions", "dependentSchemas")

_OPENAI_ALLOWED_FORMATS = frozenset({
    "date-time", "date", "time",
    "email", "hostname",
    "ipv4", "ipv6", "uuid",
})


def _is_object_shape(schema: dict[str, Any]) -> bool:
    """True when the schema should obey object-strict rules."""
    if isinstance(schema.get("properties"), dict):
        return True
    t = schema.get("type")
    if t == "object":
        return True
    if isinstance(t, list) and "object" in t:
        return True
    return False


def normalize_strict_schema(schema: Any) -> None:
    """Mutate `schema` in place so every object-shaped node satisfies"""
    if not isinstance(schema, dict):
        return

    # 0a. Drop unsupported `format` values (e.g. MCP fetch's `uri`).
    fmt = schema.get("format")
    if isinstance(fmt, str) and fmt not in _OPENAI_ALLOWED_FORMATS:
        schema.pop("format", None)

    if (
        not schema.get("type")
        and not any(k in schema for k in (
            "anyOf", "oneOf", "allOf", "$ref", "enum", "const",
        ))
    ):
        schema["type"] = "string"

    # 1. Object-shape rules: complete required + force additionalProperties=false.
    if _is_object_shape(schema):
        props = schema.get("properties")
        if isinstance(props, dict) and props:
            # FORCE (not setdefault) because a YAML / Pydantic schema may
            # have shipped `required=[<incomplete>]` and we must fix it.
            schema["required"] = list(props.keys())
        ap = schema.get("additionalProperties")
        if not isinstance(ap, dict):
            schema["additionalProperties"] = False

    # 2. Recurse into every sub-schema location.
    for key in _SUBSCHEMA_KEYS_MAP:
        block = schema.get(key)
        if isinstance(block, dict):
            for v in block.values():
                normalize_strict_schema(v)

    for key in _SUBSCHEMA_KEYS_LIST:
        block = schema.get(key)
        if isinstance(block, list):
            for v in block:
                normalize_strict_schema(v)

    for key in _SUBSCHEMA_KEYS_SINGLE:
        normalize_strict_schema(schema.get(key))

    # `items` can be a single schema (current spec) or a list of schemas
    # (legacy tuple form). Cover both.
    items = schema.get("items")
    if isinstance(items, dict):
        normalize_strict_schema(items)
    elif isinstance(items, list):
        for it in items:
            normalize_strict_schema(it)

    # `additionalProperties` when it's itself a schema (dict-of-X case).
    ap = schema.get("additionalProperties")
    if isinstance(ap, dict):
        normalize_strict_schema(ap)


def find_strict_violations(
    schema: Any, path: tuple[str, ...] = (),
) -> list[tuple[tuple[str, ...], str]]:
    """Return every node in `schema` that violates OpenAI strict mode."""
    violations: list[tuple[tuple[str, ...], str]] = []
    if not isinstance(schema, dict):
        return violations

    if _is_object_shape(schema):
        props = schema.get("properties")
        if isinstance(props, dict) and props:
            required = schema.get("required") or []
            missing = [k for k in props if k not in required]
            if missing:
                violations.append((path, f"required is missing keys: {missing}"))
        ap = schema.get("additionalProperties")
        if ap is not False and not isinstance(ap, dict):
            violations.append((path, "additionalProperties must be false"))

    # Recurse identically to normalize_strict_schema.
    for key in _SUBSCHEMA_KEYS_MAP:
        block = schema.get(key)
        if isinstance(block, dict):
            for name, v in block.items():
                violations.extend(find_strict_violations(v, path + (key, name)))
    for key in _SUBSCHEMA_KEYS_LIST:
        block = schema.get(key)
        if isinstance(block, list):
            for idx, v in enumerate(block):
                violations.extend(find_strict_violations(v, path + (key, str(idx))))
    for key in _SUBSCHEMA_KEYS_SINGLE:
        sub = schema.get(key)
        if isinstance(sub, dict):
            violations.extend(find_strict_violations(sub, path + (key,)))

    items = schema.get("items")
    if isinstance(items, dict):
        violations.extend(find_strict_violations(items, path + ("items",)))
    elif isinstance(items, list):
        for idx, it in enumerate(items):
            violations.extend(find_strict_violations(it, path + ("items", str(idx))))

    ap = schema.get("additionalProperties")
    if isinstance(ap, dict):
        violations.extend(find_strict_violations(ap, path + ("additionalProperties",)))

    return violations


_NORMALIZED_LIST_IDS: set[int] = set()


def normalize_strict_tools(tools: list[dict[str, Any]] | None) -> bool:
    """Apply `normalize_strict_schema` to each tool's parameter block."""
    if not tools:
        return False
    sid = id(tools)
    if sid in _NORMALIZED_LIST_IDS:
        return False  # fast-path: this list is already strict-valid
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        fn = tool.get("function")
        if isinstance(fn, dict):
            params = fn.get("parameters")
            if isinstance(params, dict):
                normalize_strict_schema(params)
        params = tool.get("parameters") or tool.get("input_schema")
        if isinstance(params, dict):
            normalize_strict_schema(params)
    _NORMALIZED_LIST_IDS.add(sid)
    return True


def assert_strict_tools(
    tools: list[dict[str, Any]] | None,
    *,
    raise_on_violation: bool = False,
) -> list[tuple[str, tuple[str, ...], str]]:
    """Walk every tool's schema and surface remaining strict violations."""
    out: list[tuple[str, tuple[str, ...], str]] = []
    if not tools:
        return out
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        fn = tool.get("function") or {}
        name = fn.get("name") or tool.get("name") or "<unknown>"
        params = (
            (fn.get("parameters") if isinstance(fn, dict) else None)
            or tool.get("parameters")
            or tool.get("input_schema")
        )
        if not isinstance(params, dict):
            continue
        for path, reason in find_strict_violations(params):
            out.append((name, path, reason))
            if raise_on_violation:
                raise ValueError(
                    f"tool {name!r} violates OpenAI strict mode at "
                    f"{'.'.join(path) or '<root>'}: {reason}"
                )
    return out
