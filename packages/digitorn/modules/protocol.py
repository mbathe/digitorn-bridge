"""Module layer - IModule Protocol (the public contract for all modules)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from digitorn.modules.base import ExecutionContext, Platform
    from digitorn.modules.manifest import ModuleManifest

@runtime_checkable
class IModule(Protocol):
    """Minimum contract every Digitorn module must satisfy."""

    MODULE_ID: str
    """Unique snake_case identifier (e.g. `"filesystem"`)."""

    VERSION: str
    """Semantic version string (`"MAJOR.MINOR.PATCH"`, PEP-440)."""

    SUPPORTED_PLATFORMS: list["Platform"]
    """Platforms this module runs on. `[Platform.ALL]` for cross-platform."""

    async def execute(
        self,
        action: str,
        params: dict[str, Any],
        context: "ExecutionContext | None" = None,
    ) -> Any:
        """Dispatch *action* with *params* and optional *context*."""
        ...

    def get_manifest(self) -> "ModuleManifest":
        """Return the full capability manifest for this module."""
        ...

    async def on_start(self) -> None:
        """Called when the module transitions to ACTIVE."""
        ...

    async def on_stop(self) -> None:
        """Called when the module is being disabled or the daemon shuts down."""
        ...
