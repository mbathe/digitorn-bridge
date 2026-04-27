"""Service bus — central registry for inter-module communication.

Modules register services they provide; other modules call services by name.
All calls route through the provider's ``execute()`` method so that security
decorators, audit trail, and rate limiting still apply.

Architecture::

    ComputerControlModule
        ctx.call_service("vision", "parse_screen", {})
            +-- ServiceBus.call("vision", "parse_screen", {})
                    +-- vision_module.execute("parse_screen", {})
                            +-- vision_module._action_parse_screen({})

No direct imports between modules — the ServiceBus is the only mediator.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from digitorn.modules.exceptions import ServiceNotFoundError
from digitorn.modules.log import get_logger

if TYPE_CHECKING:
    from digitorn.modules.base import BaseModule

log = get_logger(__name__)


@dataclass
class ServiceRegistration:
    """Internal record of a registered service."""

    name: str
    module_id: str
    provider: "BaseModule"
    methods: list[str] = field(default_factory=list)
    description: str = ""


class ServiceBus:
    """Central service registry and dispatcher.

    Usage::

        bus = ServiceBus()
        bus.register_service("vision", vision_module, ["parse_screen", "find_element"])
        result = await bus.call("vision", "parse_screen", {"mode": "full"})
    """

    def __init__(self) -> None:
        self._services: dict[str, ServiceRegistration] = {}

    def register_service(
        self,
        name: str,
        provider: "BaseModule",
        methods: list[str] | None = None,
        description: str = "",
    ) -> None:
        """Register a service provided by a module.

        Args:
            name:        Service name (e.g. ``"vision"``).
            provider:    Module instance providing the service.
            methods:     Available methods.  If empty, auto-discovered from
                         ``_action_*`` methods on the provider.
            description: Human-readable service description.
        """
        resolved_methods = methods or []
        if not resolved_methods:
            resolved_methods = [
                attr.removeprefix("_action_")
                for attr in dir(provider)
                if attr.startswith("_action_") and callable(getattr(provider, attr, None))
            ]

        reg = ServiceRegistration(
            name=name,
            module_id=getattr(provider, "MODULE_ID", name),
            provider=provider,
            methods=resolved_methods,
            description=description,
        )
        if name in self._services:
            old = self._services[name]
            if old.provider is reg.provider:
                pass
            elif old.module_id != reg.module_id:
                log.warning(
                    "service_replaced service=%s old_provider=%s new_provider=%s",
                    name, old.module_id, reg.module_id,
                )
            else:
                log.debug(
                    "service_reregistered service=%s module_id=%s",
                    name, reg.module_id,
                )
        self._services[name] = reg
        log.debug(
            "service_registered service=%s module_id=%s methods=%s",
            name,
            reg.module_id,
            resolved_methods,
        )

    def unregister_service(self, name: str) -> None:
        """Remove a service from the bus."""
        removed = self._services.pop(name, None)
        if removed:
            log.debug(
                "service_unregistered service=%s module_id=%s",
                name, removed.module_id,
            )

    async def call(
        self, service: str, method: str, params: dict[str, Any] | None = None
    ) -> Any:
        """Call a service method.

        Routes through the provider's ``execute()`` method so all security
        decorators and audit trails remain active.

        Args:
            service: Registered service name.
            method:  Action/method to invoke.
            params:  Parameters dict.

        Returns:
            The result from the provider's action handler.

        Raises:
            ServiceNotFoundError: If no service with this name is registered.
        """
        reg = self._services.get(service)
        if reg is None:
            raise ServiceNotFoundError(service=service)

        log.debug(
            "service_call service=%s method=%s provider=%s",
            service, method, reg.module_id,
        )
        return await reg.provider.execute(method, params or {})

    def is_available(self, service: str) -> bool:
        """Check if a service is registered."""
        return service in self._services

    def list_services(self) -> list[dict[str, Any]]:
        """List all registered services as dicts."""
        return [
            {
                "name": reg.name,
                "module_id": reg.module_id,
                "methods": reg.methods,
                "description": reg.description,
            }
            for reg in self._services.values()
        ]

    def get_provider(self, service: str) -> "BaseModule | None":
        """Return the provider module for a service, or None."""
        reg = self._services.get(service)
        return reg.provider if reg else None

    def collect_workspace_cards(self) -> list[dict[str, Any]]:
        """Collect workspace cards from all modules that provide them.

        Calls ``workspace_card()`` on each registered provider.
        Modules return None to opt out (the default).
        """
        cards: list[dict[str, Any]] = []
        seen: set[str] = set()
        for reg in self._services.values():
            mid = reg.module_id
            if mid in seen:
                continue
            seen.add(mid)
            try:
                card = reg.provider.workspace_card()
                if card is not None:
                    card.setdefault("id", mid)
                    card.setdefault("label", mid.replace("_", " ").title())
                    card.setdefault("priority", 50)
                    cards.append(card)
            except Exception:
                pass  # Non-critical: workspace card collection is best-effort
        cards.sort(key=lambda c: c.get("priority", 50))
        return cards

    @property
    def service_count(self) -> int:
        return len(self._services)
