"""Module layer - @action decorator and ActionEntry."""

from __future__ import annotations

import functools
from dataclasses import dataclass, field
from typing import Any, Callable

from digitorn.modules.manifest import ActionSpec

@dataclass
class ActionEntry:
    """A single entry in a module's `_action_registry`."""

    name: str
    handler: Callable[..., Any]
    spec: ActionSpec
    params_model: type | None = None

def _extract_schema(
    model: type,
) -> tuple[dict[str, Any], list[Any]]:
    from digitorn.modules.manifest import ParamSpec

    schema = model.model_json_schema()

    defs = schema.pop("$defs", None) or schema.pop("definitions", None)

    required_set = set(schema.get("required", []))
    properties = schema.get("properties", {})

    param_specs: list[ParamSpec] = []
    for name, prop in properties.items():
        resolved = _resolve_prop(prop, defs)

        param_specs.append(ParamSpec(
            name=name,
            type=resolved.get("type", "string"),
            description=resolved.get("description", ""),
            required=name in required_set,
            default=resolved.get("default"),
            enum=resolved.get("enum"),
        ))

    return schema, param_specs

def _resolve_prop(
    prop: dict[str, Any], defs: dict[str, Any] | None,
) -> dict[str, Any]:
    if "$ref" in prop and defs:
        ref_name = prop["$ref"].rsplit("/", 1)[-1]
        if ref_name in defs:
            merged = dict(defs[ref_name])
            merged.update({k: v for k, v in prop.items() if k != "$ref"})
            return merged

    if "anyOf" in prop:
        non_null = [t for t in prop["anyOf"] if t.get("type") != "null"]
        if non_null:
            merged = dict(non_null[0])
            for key in ("description", "default"):
                if key in prop:
                    merged.setdefault(key, prop[key])
            return merged

    return prop

def action(
    *,
    description: str,
    params_model: type | None = None,
    permissions: list[str] | None = None,
    risk_level: str = "",
    irreversible: bool = False,
    require_approval: bool = False,
    data_classification: str = "",
    tags: list[str] | None = None,
    aliases: list[str] | None = None,
    platforms: list[str] | None = None,
    side_effects: list[str] | None = None,
    streams_progress: bool = False,
    execution_mode: str = "async",
    examples: list[dict[str, Any]] | None = None,
    tool_prompt: str = "",
    cli_label: str = "",
    cli_param: str = "",
    cli_show_output: bool = False,
    cli_output_lines: int = 5,
    internal: bool = False,
    display_verb: str = "",
    display_detail_param: str = "",
    display_icon: str = "",
    display_channel: str = "",
    display_hidden: bool = False,
    display_category: str = "",
    display_group: str = "",
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Register a method as a named module action."""

    def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
        if permissions:
            from digitorn.core.permissions import validate_permissions

            invalid = validate_permissions(list(permissions))
            if invalid:
                raise ValueError(
                    f"@action({fn.__name__}): unknown system permission(s): "
                    f"{invalid!r}.  Use digitorn.core.permissions.Permission "
                    f"or a custom 'module_id:capability' string."
                )

        param_specs: list[Any] = []
        input_schema: dict[str, Any] | None = None

        if params_model is not None:
            input_schema, param_specs = _extract_schema(params_model)

        spec = ActionSpec(
            name=fn.__name__,
            description=description,
            tool_prompt=tool_prompt,
            params=param_specs,
            input_schema=input_schema,
            permissions=list(permissions or []),
            risk_level=risk_level,
            irreversible=irreversible,
            require_approval=require_approval,
            data_classification=data_classification,
            tags=list(tags or []),
            aliases=list(aliases or []),
            platforms=list(platforms or ["all"]),
            side_effects=list(side_effects or []),
            streams_progress=streams_progress,
            execution_mode=execution_mode,
            examples=list(examples or []),
            cli_label=cli_label,
            cli_param=cli_param,
            cli_show_output=cli_show_output,
            cli_output_lines=cli_output_lines,
            internal=internal,
            display_verb=display_verb,
            display_detail_param=display_detail_param,
            display_icon=display_icon,
            display_channel=display_channel,
            display_hidden=display_hidden,
            display_category=display_category,
            display_group=display_group,
        )

        fn._action_spec = spec  # type: ignore[attr-defined]

        import inspect as _inspect

        if _inspect.iscoroutinefunction(fn):
            @functools.wraps(fn)
            async def wrapper(*args: Any, **kwargs: Any) -> Any:
                return await fn(*args, **kwargs)
        else:
            @functools.wraps(fn)
            def wrapper(*args: Any, **kwargs: Any) -> Any:  # type: ignore[misc]
                return fn(*args, **kwargs)

        wrapper._action_spec = spec  # type: ignore[attr-defined]

        class _ActionDescriptor:
            """Non-data descriptor that registers itself at class creation."""

            def __set_name__(self, owner: type, name: str) -> None:
                if "_action_registry" not in owner.__dict__:
                    parent_registry = getattr(owner, "_action_registry", {})
                    owner._action_registry = dict(parent_registry)

                entry = ActionEntry(
                    name=name, handler=wrapper, spec=spec,
                    params_model=params_model,
                )
                owner._action_registry[name] = entry
                spec.name = name

                setattr(owner, name, wrapper)

            def __get__(self, obj: Any, objtype: type | None = None) -> Any:
                return wrapper

        return _ActionDescriptor()  # type: ignore[return-value]

    return decorator
