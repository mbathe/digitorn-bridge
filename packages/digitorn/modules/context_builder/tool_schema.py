"""Tool schema conversion - ActionSpec/ActionEntry → JSON Schema."""

from __future__ import annotations

from typing import Any

# Sentinel JSON Schema for the optional `intent` field injected by
# apps that set `inject_intent: true`.
_INTENT_FIELD_SCHEMA: dict[str, Any] = {
    "type": "string",
    "description": (
        "REQUIRED METADATA. This is NOT a tool argument - it is "
        "stripped from your input before the tool runs (the handler "
        "never sees it). Write ONE present-continuous verb phrase "
        "(2-5 words) describing what you are doing for the user RIGHT "
        "NOW. Examples: 'Analyzing requirements', 'Reviewing "
        "components', 'Searching the codebase', 'Fixing the bug', "
        "'Running tests', 'Drafting the response'. Always include "
        "this as the FIRST argument in every tool call - the UI "
        "renders it as a live progress indicator that the user sees "
        "while your call is in flight."
    ),
}

def inject_intent_field(schema: dict[str, Any]) -> dict[str, Any]:
    """Return `schema` with an `intent` string property prepended."""
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
    """Extract JSON Schema from an ActionEntry."""
    if action_entry.params_model is not None:
        schema = action_entry.params_model.model_json_schema()
        if schema is not None:
            defs = schema.get("$defs") or schema.get("definitions") or {}
            if defs:
                schema = _resolve_refs(schema, defs)
            schema.pop("title", None)
            schema.pop("$defs", None)
            schema.pop("definitions", None)
            props = schema.get("properties") or {}
            if not isinstance(props, dict):
                return schema
            hidden = [k for k, v in props.items() if isinstance(v, dict) and v.get("hidden")]
            for k in hidden:
                props.pop(k, None)
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
