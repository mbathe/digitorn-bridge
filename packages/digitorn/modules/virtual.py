"""Module Spec v3 - Virtual Module Factory."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from digitorn.modules.base import BaseModule, Platform
from digitorn.modules.manifest import (
    ActionSpec,
    Capability,
    ModuleManifest,
    ParamSpec,
)

@dataclass
class VirtualAction:
    """Description of a dynamically created action."""

    handler: Callable[..., Any]
    description: str = ""
    params: list[ParamSpec] = field(default_factory=list)
    returns: str = "object"
    returns_description: str = ""
    permission_required: str = "local_worker"
    execution_mode: str = "async"
    side_effects: list[str] = field(default_factory=list)
    output_schema: dict[str, Any] | None = None
    capabilities: list[Capability] = field(default_factory=list)

class VirtualModule(BaseModule):
    """A module created dynamically by VirtualModuleFactory."""

    def __init__(
        self,
        module_id: str,
        version: str,
        description: str,
        actions: dict[str, VirtualAction],
        author: str = "",
        tags: list[str] | None = None,
        platforms: list[Platform] | None = None,
        declared_permissions: list[str] | None = None,
        declared_capabilities: list[Capability] | None = None,
    ) -> None:
        self.MODULE_ID = module_id
        self.VERSION = version
        self.SUPPORTED_PLATFORMS = platforms or [Platform.ALL]
        self._description = description
        self._virtual_actions = actions
        self._author = author
        self._tags = tags or []
        self._declared_permissions = declared_permissions or []
        self._declared_capabilities = declared_capabilities or []
        super().__init__()

        for name, va in self._virtual_actions.items():
            spec = ActionSpec(
                name=name,
                description=va.description,
                params=va.params,
                returns=va.returns,
                returns_description=va.returns_description,
                permission_required=va.permission_required,
                execution_mode=va.execution_mode,
                side_effects=va.side_effects,
                output_schema=va.output_schema,
                capabilities=va.capabilities,
            )
            self.register_action(name, va.handler, spec)

    def get_manifest(self) -> ModuleManifest:
        return ModuleManifest(
            module_id=self.MODULE_ID,
            version=self.VERSION,
            description=self._description,
            author=self._author,
            tags=self._tags,
            platforms=[p.value for p in self.SUPPORTED_PLATFORMS],
            actions=list(self._dynamic_specs.values()),
            declared_permissions=self._declared_permissions,
            declared_capabilities=self._declared_capabilities,
        )

class VirtualModuleFactory:
    """Factory for creating VirtualModule instances at runtime."""

    def create(
        self,
        module_id: str,
        version: str = "1.0.0",
        description: str = "",
        actions: dict[str, VirtualAction] | None = None,
        author: str = "",
        tags: list[str] | None = None,
        platforms: list[Platform] | None = None,
        declared_permissions: list[str] | None = None,
        declared_capabilities: list[Capability] | None = None,
    ) -> VirtualModule:
        """Create a VirtualModule with multiple actions."""
        return VirtualModule(
            module_id=module_id,
            version=version,
            description=description,
            actions=actions or {},
            author=author,
            tags=tags,
            platforms=platforms,
            declared_permissions=declared_permissions,
            declared_capabilities=declared_capabilities,
        )

    def from_callable(
        self,
        module_id: str,
        handler: Callable[..., Any],
        action_name: str = "execute",
        description: str = "",
        version: str = "1.0.0",
        params: list[ParamSpec] | None = None,
        permission_required: str = "local_worker",
    ) -> VirtualModule:
        """Create a single-action VirtualModule from a callable."""
        return self.create(
            module_id=module_id,
            version=version,
            description=description or f"Virtual module: {module_id}",
            actions={
                action_name: VirtualAction(
                    handler=handler,
                    description=description or f"Execute {action_name}",
                    params=params or [],
                    permission_required=permission_required,
                ),
            },
        )

    def from_tool_schema(
        self,
        module_id: str,
        tool_schemas: list[dict[str, Any]],
        handler_map: dict[str, Callable[..., Any]],
        version: str = "1.0.0",
        description: str = "",
    ) -> VirtualModule:
        """Create a VirtualModule from LangChain-style tool schemas."""
        actions: dict[str, VirtualAction] = {}
        for schema in tool_schemas:
            name = schema["name"]
            if name not in handler_map:
                continue
            params: list[ParamSpec] = []
            props = schema.get("parameters", {}).get("properties", {})
            required = schema.get("parameters", {}).get("required", [])
            for pname, pspec in props.items():
                params.append(
                    ParamSpec(
                        name=pname,
                        type=pspec.get("type", "string"),
                        description=pspec.get("description", ""),
                        required=pname in required,
                        default=pspec.get("default"),
                    )
                )
            actions[name] = VirtualAction(
                handler=handler_map[name],
                description=schema.get("description", ""),
                params=params,
            )

        return self.create(
            module_id=module_id,
            version=version,
            description=description or f"Virtual module from {len(tool_schemas)} tool schemas",
            actions=actions,
        )
