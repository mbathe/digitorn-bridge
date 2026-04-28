"""Module context - controlled gateway for inter-module communication.

Every module receives a ``ModuleContext`` during server startup.  The context
provides structured access to:

  - **ServiceBus**: call other modules' services by name
  - **EventBus**: emit and subscribe to events
  - **Settings**: read-only access to daemon configuration
  - **Logger**: pre-bound structlog logger scoped to the module
  - **KV store**: per-module key-value state storage (optional)
  - **SecurityManager**: permission checks (optional)

This replaces the ad-hoc injection patterns (``set_registry()``,
``set_daemon()``, ``set_recorder()``) with a single, standardised interface.

Usage::

    result = await self.ctx.call_service("vision", "parse_screen", {"mode": "full"})
    await self.ctx.emit_event("digitorn.modules", {"event": "vision_called"})
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable

from digitorn.modules.log import get_logger

if TYPE_CHECKING:
    from digitorn.modules.service_bus import ServiceBus

log = get_logger(__name__)


@dataclass
class ModuleContext:
    """Per-module gateway to system services.

    Attributes:
        module_id:        The owning module's MODULE_ID.
        event_bus:        EventBus for emitting/subscribing to events.
        service_bus:      ServiceBus for inter-module service calls.
        settings:         Daemon-wide settings (read-only by convention).
        logger:           Pre-bound structlog logger.
        kv_store:         Per-module KV store (optional, None if not configured).
        security_manager: SecurityManager reference (optional).
    """

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
        """Call a service registered on the ServiceBus.

        Args:
            service: Service name (e.g. ``"vision"``).
            method:  Method/action to invoke (e.g. ``"parse_screen"``).
            params:  Parameters dict passed to the action handler.

        Returns:
            The service call result.

        Raises:
            ServiceNotFoundError: If no service with this name is registered.
        """
        return await self.service_bus.call(service, method, params or {})

    async def emit_event(self, topic: str, data: dict[str, Any]) -> None:
        """Emit an event to the EventBus.

        The event dict is stamped with ``module_id`` automatically.
        """
        data.setdefault("module_id", self.module_id)
        await self.event_bus.emit(topic, data)

    def register_service(
        self,
        name: str,
        handler: "Any",
        methods: list[str] | None = None,
        description: str = "",
    ) -> None:
        """Register a service on the ServiceBus.

        Args:
            name:        Service name (e.g. ``"vision"``).
            handler:     The module instance providing the service (usually ``self``).
            methods:     List of available method names.  If None, auto-discovered
                         from the handler's ``_action_*`` methods.
            description: Human-readable description of the service.
        """
        self.service_bus.register_service(
            name=name,
            provider=handler,
            methods=methods or [],
            description=description,
        )
