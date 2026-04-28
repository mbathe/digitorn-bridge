"""Tool schema conversion - ActionSpec/ActionEntry → JSON Schema.

Converts module action specs into the universal JSON Schema format
used by LLM function calling (OpenAI, Anthropic, etc.).
"""

from __future__ import annotations

from typing import Any


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
