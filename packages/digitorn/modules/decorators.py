"""Module layer — @action decorator and ActionEntry.

The ``@action`` decorator is the **single source of truth** for every action
a module exposes:

- It declares the description, permissions, risk level, tags, and all other
  spec metadata *once*, at definition time.
- It populates ``cls._action_registry: dict[str, ActionEntry]`` at class
  creation — **a dict, not a naming convention**.
- It attaches the ``ActionSpec`` to the method as ``handler._action_spec``
  so ``ModuleManifest.from_module()`` can recover it without a live instance.
- ``BaseModule.execute()`` checks ``_action_registry`` first (O(1) dict
  lookup), then falls back to the legacy ``_action_<name>`` convention for
  backward-compatibility.

DRY principle
-------------
Because ``@action`` captures the full spec, ``get_manifest()`` becomes a
one-liner::

    def get_manifest(self) -> ModuleManifest:
        return ModuleManifest.from_module(self).model_copy(update={
            "description": "...",
            "author": "...",
        })

No need to maintain a parallel list of ``ActionSpec`` objects — the registry
IS the manifest.

Usage::

    from digitorn.module.decorators import action
    from digitorn.security.permissions import Permission

    class FilesystemModule(BaseModule):

        @action(
            description="Read the text content of a file.",
            permissions=[Permission.FILESYSTEM_READ],
            tags=["io", "read"],
        )
        async def read_file(
            self,
            params: ReadFileParams,
            ctx: ExecutionContext,
        ) -> ActionResult[FileContent]:
            ...
"""

from __future__ import annotations

import functools
from dataclasses import dataclass, field
from typing import Any, Callable

from digitorn.modules.manifest import ActionSpec


@dataclass
class ActionEntry:
    """A single entry in a module's ``_action_registry``.

    Ties the callable handler to its fully-resolved ``ActionSpec`` in one
    place.  The registry is a ``dict[str, ActionEntry]`` keyed by action
    name so all lookups are O(1).

    Attributes:
        name:         Action name (same key as the dict key — kept for clarity).
        handler:      The unbound method (or bound method after instantiation).
        spec:         Full ``ActionSpec`` populated by the ``@action`` decorator.
        params_model: Optional Pydantic BaseModel class for automatic parameter
                      validation.  When set, ``BaseModule.execute()`` validates
                      the incoming dict and passes a typed model instance to the
                      handler instead of a raw dict.
    """

    name: str
    handler: Callable[..., Any]
    spec: ActionSpec
    params_model: type | None = None


def _extract_schema(
    model: type,
) -> tuple[dict[str, Any], list[Any]]:
    """Extract a full JSON schema and a ``ParamSpec`` list from a Pydantic model.

    Returns:
        (input_schema, param_specs) — the raw JSON schema dict and a list of
        ``ParamSpec`` objects for backward-compatible introspection.
    """
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
    """Resolve ``$ref`` and ``anyOf`` in a JSON schema property.

    Handles the common Pydantic patterns:
    - ``{"$ref": "#/$defs/Foo"}`` → inline the definition
    - ``{"anyOf": [{"type": "integer"}, {"type": "null"}]}`` → pick the
      non-null type
    """
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
    """Register a method as a named module action.

    Applying ``@action`` to a method:

    1. Builds an :class:`ActionSpec` from the decorator arguments.
    2. Attaches the spec to the method as ``handler._action_spec``.
    3. Registers both under the parent class's ``_action_registry`` dict via
       the ``__set_name__`` descriptor trick (executed automatically by Python
       at class creation time).

    The method name becomes the action name (no ``_action_`` prefix needed).

    Args:
        description:         Human-readable description shown in the dashboard
                             and LLM tool definitions.  **Required.**
        params_model:        Pydantic ``BaseModel`` subclass describing the
                             action's input parameters.  When set:
                             (1) The full JSON schema is auto-generated and
                             stored in ``ActionSpec.input_schema`` so LLM
                             agents see every parameter with type, description,
                             and constraints.
                             (2) ``BaseModule.execute()`` auto-validates the
                             incoming dict and passes a typed model instance
                             to the handler — invalid params yield a clean
                             ``ActionResult(success=False)`` instead of an
                             unhandled exception.
        permissions:         List of permission strings (or ``StrEnum`` values)
                             required to call this action.
        risk_level:          ``"low"``, ``"medium"``, or ``"high"``.  Inferred
                             from permissions when omitted.
        irreversible:        ``True`` if the action cannot be undone.  Enables
                             a user-confirmation gate before execution.
        require_approval:    ``True`` to force user approval before execution,
                             regardless of the app's capabilities config.
                             The action policy is forced to ``approve``.
        data_classification: ``"public"``, ``"internal"``, ``"sensitive"``, or
                             ``"restricted"``.
        tags:                Arbitrary labels for filtering / grouping.
        aliases:             Search aliases — alternative names, verbs, and
                             translations that should match this action in
                             tool discovery.  Each module declares its own
                             vocabulary.  Example: ``["lire", "cat", "voir"]``.
        platforms:           Platforms this action supports.  Defaults to the
                             module-level ``SUPPORTED_PLATFORMS``.
        side_effects:        Declared side effects (``"filesystem_write"``,
                             ``"network_request"``, etc.).
        streams_progress:    ``True`` if the action emits incremental progress
                             updates via the stream channel.
        execution_mode:      ``"sync"``, ``"async"`` (default), ``"background"``
                             or ``"scheduled"``.
        examples:            Few-shot examples for the LLM.
    """

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
