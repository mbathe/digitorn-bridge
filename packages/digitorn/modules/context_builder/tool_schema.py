"""Tool schema conversion - ActionSpec/ActionEntry → JSON Schema.

Converts module action specs into the universal JSON Schema format
used by LLM function calling (OpenAI, Anthropic, etc.).
"""

from __future__ import annotations

from typing import Any


# Sentinel JSON Schema fragment for the optional ``intent`` field that
# ``inject_intent: true`` apps prepend to every tool. Kept here next to
# the schema builder so the description (which the LLM reads) lives at
# the same place as the injection logic.
_INTENT_FIELD_SCHEMA: dict[str, Any] = {
    "type": "string",
    "description": (
        "REQUIRED METADATA. This is NOT a tool argument — it is "
        "stripped from your input before the tool runs (the handler "
        "never sees it). Write ONE present-continuous verb phrase "
        "(2-5 words) describing what you are doing for the user RIGHT "
        "NOW. Examples: 'Analyzing requirements', 'Reviewing "
        "components', 'Searching the codebase', 'Fixing the bug', "
        "'Running tests', 'Drafting the response'. Always include "
        "this as the FIRST argument in every tool call — the UI "
        "renders it as a live progress indicator that the user sees "
        "while your call is in flight."
    ),
}


def inject_intent_field(schema: dict[str, Any]) -> dict[str, Any]:
    """Return ``schema`` with an ``intent`` string property prepended
    to ``properties`` and added to ``required``.

    Idempotent: skips if ``intent`` is already present (e.g. a tool
    that declares its own ``intent`` arg keeps it). Mutates a copy,
    not the input.

    The order of keys in ``properties`` matters: Python dicts preserve
    insertion order, providers like Anthropic stream JSON in dict
    order, and we want ``intent`` to land in the LLM's first token
    burst so the frontend can show the verb before any other arg is
    parsed.
    """
    schema = dict(schema)
    props = dict(schema.get("properties") or {})
    if "intent" in props:
        return schema
    new_props: dict[str, Any] = {"intent": dict(_INTENT_FIELD_SCHEMA)}
    new_props.update(props)
    schema["properties"] = new_props
    required = list(schema.get("required") or [])
    if "intent" not in required:
        required.insert(0, "intent")
    schema["required"] = required
    return schema


def action_entry_to_json_schema(action_entry: Any) -> dict[str, Any]:
    """Extract JSON Schema from an ActionEntry.

    Priority:
    1. params_model.model_json_schema() - Pydantic v2 (preferred)
    2. spec.input_schema - pre-computed by @action decorator
    3. Build from spec.params (ParamSpec list) - legacy fallback
    4. Empty schema - action takes no parameters

    Returns a JSON Schema ``{"type": "object", "properties": ..., "required": ...}``
    """
    if action_entry.params_model is not None:
        schema = action_entry.params_model.model_json_schema()
        if schema is not None:
            defs = schema.get("$defs") or schema.get("definitions") or {}
            if defs:
                schema = _resolve_refs(schema, defs)
            schema.pop("title", None)
            schema.pop("$defs", None)
            schema.pop("definitions", None)
            # Remove properties marked as hidden (json_schema_extra={"hidden": True})
            # This keeps the LLM tool schema clean - only shows params the model needs
            props = schema.get("properties") or {}
            if not isinstance(props, dict):
                return schema
            hidden = [k for k, v in props.items() if isinstance(v, dict) and v.get("hidden")]
            for k in hidden:
                props.pop(k, None)
                # Also remove from required if present
                req = schema.get("required", [])
                if k in req:
                    req.remove(k)
            return schema

    spec = action_entry.spec

    if spec.input_schema:
        return dict(spec.input_schema)

    if spec.params:
        return _params_to_schema(spec.params)

    return {"type": "object", "properties": {}, "required": []}


def _resolve_refs(node: Any, defs: dict[str, Any]) -> Any:
    """Recursively inline ``$ref`` pointers using *defs*.

    Turns ``{"$ref": "#/$defs/Foo"}`` into the actual Foo schema,
    so the resulting schema is self-contained (no $defs needed).
    """
    if isinstance(node, dict):
        if "$ref" in node and len(node) == 1:
            ref_path = node["$ref"]
            ref_name = ref_path.rsplit("/", 1)[-1]
            resolved = defs.get(ref_name, node)
            resolved = _resolve_refs(dict(resolved), defs)
            resolved.pop("title", None)
            return resolved
        return {k: _resolve_refs(v, defs) for k, v in node.items()}
    if isinstance(node, list):
        return [_resolve_refs(item, defs) for item in node]
    return node


def _params_to_schema(params: list) -> dict[str, Any]:
    """Convert a list of ParamSpec into JSON Schema."""
    properties: dict[str, Any] = {}
    required: list[str] = []

    for p in params:
        prop: dict[str, Any] = {"type": p.type, "description": p.description}
        if p.enum is not None:
            prop["enum"] = p.enum
        if p.default is not None:
            prop["default"] = p.default
        if p.example is not None:
            prop["examples"] = [p.example]
        properties[p.name] = prop
        if p.required:
            required.append(p.name)

    schema: dict[str, Any] = {"type": "object", "properties": properties}
    if required:
        schema["required"] = required
    return schema
