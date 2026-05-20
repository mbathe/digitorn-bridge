"""Module context - controlled gateway for inter-module communication."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable

from digitorn.modules.log import get_logger

if TYPE_CHECKING:
    from digitorn.modules.service_bus import ServiceBus

log = get_logger(__name__)

@dataclass
class ModuleContext:
    """Per-module gateway to system services."""

    module_id: str
    event_bus: Any
    service_bus: "ServiceBus"
    settings: Any
    logger: Any = field(default=None)
    kv_store: Any | None = None
    security_manager: Any | None = None
    sidecar_pool: Any | None = None

    def __post_init__(self) -> None:
        if self.logger is None:
            self.logger = get_logger(f"module.{self.module_id}")

    async def call_service(
        self, service: str, method: str, params: dict[str, Any] | None = None
    ) -> Any:
        """Call a service registered on the ServiceBus."""
        return await self.service_bus.call(service, method, params or {})

    async def emit_event(self, topic: str, data: dict[str, Any]) -> None:
        """Emit an event to the EventBus."""
        data.setdefault("module_id", self.module_id)
        await self.event_bus.emit(topic, data)

    def register_service(
        self,
        name: str,
        handler: "Any",
        methods: list[str] | None = None,
        description: str = "",
    ) -> None:
        """Register a service on the ServiceBus."""
        self.service_bus.register_service(
            name=name,
            provider=handler,
            methods=methods or [],
            description=description,
        )
