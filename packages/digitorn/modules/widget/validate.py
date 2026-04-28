"""Server-side re-validation of submitted form values.

The Flutter client validates form inputs locally (`required`,
`regex`, `min`, `max`, `type_hint`) before letting the user submit.
But a malicious or buggy client can bypass that, so the daemon
re-runs the same checks against the values it receives in
``POST /widgets/action body.form``.

This module:

1. Walks the app's compiled widgets tree to find every form input
   node by ``name`` (across chat_side / workspace_tabs / modals /
   inline)
2. Builds a ``{name: rules}`` map of validation rules
3. Runs the rules against the submitted form values
4. Returns ``(ok, errors)`` - caller surfaces a 400 with the errors

The validation rules per primitive type are taken from the spec
(§5.4 Input).
"""

from __future__ import annotations

import re
from typing import Any


_INPUT_PRIMITIVES = {
    "text_input", "textarea", "select", "multi_select",
    "radio", "checkbox", "switch", "slider",
    "date", "time", "datetime", "file_upload", "code_editor",
}


def _walk_inputs(node: Any, out: dict[str, dict[str, Any]]) -> None:
    """Collect every input node by name from a widget tree."""
    if node is None:
        return
    if isinstance(node, list):
        for child in node:
            _walk_inputs(child, out)
        return

    # Pydantic WidgetNode → dict
    if hasattr(node, "model_dump"):
        data = node.model_dump(by_alias=True, exclude_none=True)
    elif isinstance(node, dict):
        data = node
    else:
        return

    if data.get("type") in _INPUT_PRIMITIVES:
        name = data.get("name")
        if name:
            out[name] = data

    # Recurse into known container fields
    for field in (
        "children", "item", "first", "second",
        "body", "render", "empty", "loading",
    ):
        child = data.get(field)
        if child is not None:
            _walk_inputs(child, out)


def collect_form_inputs(widgets_cfg: Any) -> dict[str, dict[str, Any]]:
    """Walk every widget zone and return a {input_name: spec} map."""
    out: dict[str, dict[str, Any]] = {}
    if widgets_cfg is None:
        return out

    if widgets_cfg.chat_side is not None:
        _walk_inputs(widgets_cfg.chat_side.tree, out)
    for tab in widgets_cfg.workspace_tabs or []:
        _walk_inputs(tab.tree, out)
    for modal in (widgets_cfg.modals or {}).values():
        _walk_inputs(modal.tree, out)
    for inline in (widgets_cfg.inline or {}).values():
        _walk_inputs(inline.tree, out)
    return out


def validate_form_values(
    inputs: dict[str, dict[str, Any]],
    values: dict[str, Any],
) -> tuple[bool, dict[str, str]]:
    """Re-run client-side validation rules against submitted values.

    Returns ``(ok, errors)`` where ``errors`` is ``{field_name: msg}``.
    Unknown fields (not declared in any input) are ignored - they're
    typically passed through from form scope but not bound to a
    declared widget.
    """
    errors: dict[str, str] = {}

    for name, spec in inputs.items():
        value = values.get(name)
        rules = spec.get("validation") or {}
        type_hint = spec.get("type_hint")
        primitive = spec.get("type")

        # required
        if spec.get("required"):
            if value is None or value == "" or value == []:
                errors[name] = f"{name} is required"
                continue

        # If not required and empty, skip the rest
        if value is None or value == "":
            continue

        # type_hint coercion checks
        if type_hint == "email":
            if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", str(value)):
                errors[name] = "must be a valid email"
                continue
        elif type_hint == "url":
            if not re.match(r"^https?://", str(value)):
                errors[name] = "must be a valid URL"
                continue
        elif type_hint == "number":
            try:
                float(value)
            except (TypeError, ValueError):
                errors[name] = "must be a number"
                continue
        elif type_hint == "tel":
            if not re.match(r"^[\d\s+()-]+$", str(value)):
                errors[name] = "must be a valid phone number"
                continue

        # regex (string fields)
        if "regex" in rules and isinstance(value, str):
            try:
                if not re.match(rules["regex"], value):
                    errors[name] = rules.get("message") or f"{name} does not match pattern"
                    continue
            except re.error:
                pass  # bad regex in YAML - silently skip

        # min / max
        rmin = rules.get("min")
        rmax = rules.get("max")
        if rmin is not None or rmax is not None:
            if isinstance(value, str):
                length = len(value)
                if rmin is not None and length < int(rmin):
                    errors[name] = f"{name} must be at least {rmin} characters"
                    continue
                if rmax is not None and length > int(rmax):
                    errors[name] = f"{name} must be at most {rmax} characters"
                    continue
            elif isinstance(value, (int, float)):
                if rmin is not None and value < float(rmin):
                    errors[name] = f"{name} must be ≥ {rmin}"
                    continue
                if rmax is not None and value > float(rmax):
                    errors[name] = f"{name} must be ≤ {rmax}"
                    continue
            elif isinstance(value, list):
                if rmin is not None and len(value) < int(rmin):
                    errors[name] = f"{name} must have at least {rmin} items"
                    continue
                if rmax is not None and len(value) > int(rmax):
                    errors[name] = f"{name} must have at most {rmax} items"
                    continue

        # multi_select max cap
        if primitive == "multi_select":
            cap = spec.get("max")
            if cap is not None and isinstance(value, list) and len(value) > int(cap):
                errors[name] = f"{name} accepts at most {cap} selections"

        # checkbox required (must be true)
        if primitive == "checkbox" and spec.get("required") and not value:
            errors[name] = f"{name} must be checked"

    return (len(errors) == 0, errors)
