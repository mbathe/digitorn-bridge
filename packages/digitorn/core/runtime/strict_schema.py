"""OpenAI strict-mode normalisation for tool schemas.

OpenAI's strict mode (and LiteLLM's enforcement of it on every
OpenAI-compatible backend the gateway dispatches to) rejects a
function schema unless, at EVERY object-shaped node:

  * ``required`` lists every key in ``properties``
  * ``additionalProperties`` is ``False``

These rules apply recursively to every sub-schema, no matter how
deeply nested or where it appears (object property values, array
items, anyOf/oneOf/allOf branches, not, if/then/else, $defs that
weren't pre-inlined, additionalProperties when it's itself a
schema, etc.).

Pydantic flags only fields without a default as required, and
sometimes omits ``type: "object"`` when the shape is already
determined by ``properties`` alone. Both habits trip OpenAI strict.

This module owns the **one** canonical normalisation routine. We
call it in two places:

  1. ``context_builder.builder.build_direct_tools`` -- proactive,
     at schema-build time, so the LLM sees a clean shape.
  2. ``openai_compat.OpenAICompatProvider._build_params`` -- defensive,
     right before tools go on the wire, so any path that didn't go
     through (1) still ships strict-valid schemas.

The normalisation is shape-only: defaults stay where Pydantic put
them, so a model that ships ``description: ""`` is still accepted on
the receiving end.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


# Every JSON Schema keyword whose value is (or contains) another schema.
# We recurse into all of these so a strict violation can never hide in
# a corner of the schema tree.

# Keywords whose value is a single sub-schema.
_SUBSCHEMA_KEYS_SINGLE = ("not", "if", "then", "else", "contains", "propertyNames")
# Keywords whose value is a list of sub-schemas.
_SUBSCHEMA_KEYS_LIST = ("anyOf", "oneOf", "allOf", "prefixItems")
# Keywords whose value is a dict {name: sub-schema}.
_SUBSCHEMA_KEYS_MAP = ("properties", "patternProperties", "$defs", "definitions", "dependentSchemas")

# OpenAI strict mode rejects any ``format`` value not in this whitelist.
# JSON Schema defines dozens more (uri, uri-reference, regex, password,
# binary, byte, ...) but OpenAI 400s on them. MCP servers commonly use
# ``uri`` for URL fields; we drop unsupported formats so the tool stays
# callable. ``format`` is a hint -- the LLM doesn't strictly need it.
_OPENAI_ALLOWED_FORMATS = frozenset({
    "date-time", "date", "time",
    "email", "hostname",
    "ipv4", "ipv6", "uuid",
})


def _is_object_shape(schema: dict[str, Any]) -> bool:
    """True when the schema should obey object-strict rules.

    A schema is "object-shaped" for OpenAI strict purposes when EITHER
    it carries a ``properties`` block (regardless of whether ``type``
    is set) OR ``type`` is explicitly ``"object"`` (or contains it in
    a list of allowed types). Pydantic doesn't always emit ``type``
    when ``properties`` alone fully determines the shape, so going
    by ``type`` alone is unsafe.
    """
    if isinstance(schema.get("properties"), dict):
        return True
    t = schema.get("type")
    if t == "object":
        return True
    if isinstance(t, list) and "object" in t:
        return True
    return False


def normalize_strict_schema(schema: Any) -> None:
    """Mutate ``schema`` in place so every object-shaped node satisfies
    OpenAI strict mode (``required`` complete + ``additionalProperties: false``).

    Walks recursively into every place JSON Schema can hide a sub-schema:
    ``properties``, ``patternProperties``, ``$defs``/``definitions``,
    ``items`` (single or tuple form), ``prefixItems``, ``anyOf``,
    ``oneOf``, ``allOf``, ``not``, ``if``/``then``/``else``,
    ``contains``, ``propertyNames``, ``dependentSchemas``, and
    ``additionalProperties`` when it's itself a schema dict.

    No-op when ``schema`` is not a dict.
    """
    if not isinstance(schema, dict):
        return

    # 0a. Drop unsupported ``format`` values (e.g. MCP fetch's ``uri``).
    fmt = schema.get("format")
    if isinstance(fmt, str) and fmt not in _OPENAI_ALLOWED_FORMATS:
        schema.pop("format", None)

    # 0b. Inject default ``type`` on schemas OpenAI strict mode requires
    # to have one. Empty {} sub-schemas (e.g. ``list`` / ``dict`` /
    # ``Any`` translated by Pydantic into ``items: {}``) get
    # ``"string"`` - the most permissive single type strict accepts.
    # Skipped when the schema has a compositional alternative
    # (anyOf/oneOf/allOf/$ref) or a value constraint (enum/const), since
    # those define the shape without ``type``. The proper long-term fix
    # is for tool authors to tighten ``list`` → ``list[X]``.
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
            # have shipped ``required=[<incomplete>]`` and we must fix it.
            schema["required"] = list(props.keys())
        # ``additionalProperties`` may already be set to a SCHEMA (the
        # ``dict[str, X]`` case). Only force-false when it's not a dict;
        # leave dict-shaped additionalProperties alone and recurse into it.
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

    # ``items`` can be a single schema (current spec) or a list of schemas
    # (legacy tuple form). Cover both.
    items = schema.get("items")
    if isinstance(items, dict):
        normalize_strict_schema(items)
    elif isinstance(items, list):
        for it in items:
            normalize_strict_schema(it)

    # ``additionalProperties`` when it's itself a schema (dict-of-X case).
    ap = schema.get("additionalProperties")
    if isinstance(ap, dict):
        normalize_strict_schema(ap)


def find_strict_violations(
    schema: Any, path: tuple[str, ...] = (),
) -> list[tuple[tuple[str, ...], str]]:
    """Return every node in ``schema`` that violates OpenAI strict mode.

    Each entry is ``(path, reason)`` where ``path`` mirrors the JSON
    Pointer-style location OpenAI itself uses in error messages
    (e.g. ``("properties", "form", "anyOf", "0", "items")``).

    Used by ``assert_strict_schema`` and by the defensive log line in
    ``OpenAICompatProvider`` to attribute any regression to a precise
    location.
    """
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


# Fast-path cache: tool lists are typically built once per app deploy
# (in ``build_direct_tools``) and the SAME list object is then passed
# to ``provider.chat()`` for every turn of that session. Normalising
# the same list 50× per conversation would be wasteful even at ~30µs
# per pass. We tag the list's ``id()`` after the first pass; the
# second call is a single ``int in set`` lookup (~100 ns) and returns.
#
# Memory: 1 int per unique tool-list object. Daemon-lifetime; bounded
# by the number of apps deployed (handful). Negligible.
#
# Correctness: tool schemas are mutated in place by the first
# normalize, so subsequent calls would be no-ops anyway. The cache
# just makes "no-op" cost O(1) instead of O(nodes-in-tree). It cannot
# return a false positive: an id() is stable for the lifetime of the
# object, and Python only reuses ids after garbage collection -- which
# never happens to deploy-time tool lists.
_NORMALIZED_LIST_IDS: set[int] = set()


def normalize_strict_tools(tools: list[dict[str, Any]] | None) -> bool:
    """Apply ``normalize_strict_schema`` to each tool's parameter block.

    Accepts the OpenAI function-calling tool list shape:
        [{"type": "function", "function": {"name": ..., "parameters": {...}}}]

    Also covers raw top-level ``parameters`` / ``input_schema`` for
    non-OpenAI tool shapes (Anthropic native, MCP).

    Mutates in place. No-op on ``None`` / empty list / malformed entries.

    Uses an ``id()``-keyed fast-path: the second+ call with the same
    list object returns in O(1).

    Returns ``True`` when work was performed (fresh list seen for the
    first time), ``False`` when the call hit the fast-path. Callers
    use this signal to decide whether to re-run the heavier
    ``assert_strict_tools`` pass.
    """
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
    """Walk every tool's schema and surface remaining strict violations.

    Returns a list of ``(tool_name, path, reason)`` tuples. When
    ``raise_on_violation`` is True, raises ``ValueError`` on the first
    violation - useful for unit tests and the daemon's startup self-check.

    When false (the default), just returns the list so the caller can
    log + continue. Production use: log loud + drop the tool rather
    than crash the agent turn.
    """
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
