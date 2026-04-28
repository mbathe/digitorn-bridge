"""Module lifecycle manager - state machine and orchestration.

Manages the lifecycle state of every registered module, enforces valid
transitions, calls lifecycle hooks (``on_start``, ``on_stop``, ``on_pause``,
``on_resume``), and emits events to the EventBus.

State machine::

    LOADED --> STARTING --> ACTIVE <--> PAUSED
                            |
                            +--> STOPPING --> DISABLED
                                   |
                                 ERROR (on failure)

Usage::

    lifecycle = ModuleLifecycleManager(registry, event_bus, service_bus)
    lifecycle.set_type("filesystem", ModuleType.SYSTEM)
    await lifecycle.start_all()
    await lifecycle.pause_module("browser")
    await lifecycle.stop_all()
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

TOPIC_MODULES = "digitorn.modules"

from digitorn.modules.exceptions import (
    ModuleLifecycleError,
    ModuleNotFoundError,
    ActionDisabledError,
)
from digitorn.modules.log import get_logger
from digitorn.modules.types import (
    ModuleState,
    ModuleType,
    SYSTEM_MODULE_IDS,
    VALID_TRANSITIONS,
)

if TYPE_CHECKING:
    from digitorn.core.events.models import UniversalEvent
    from digitorn.modules.registry import ModuleRegistry
    from digitorn.modules.service_bus import ServiceBus

log = get_logger(__name__)


class ModuleLifecycleManager:
    """Orchestrate module lifecycle transitions and action toggles.

    Each module's state is tracked internally.  The manager calls the
    appropriate lifecycle hook on the module instance and emits events
    to the EventBus on every state change.
    """

    def __init__(
        self,
        registry: "ModuleRegistry",
        event_bus: Any,
        service_bus: "ServiceBus | None" = None,
        state_store: Any | None = None,
    ) -> None:
        self._registry = registry
        self._event_bus = event_bus
        self._service_bus = service_bus
        self._state_store = state_store
        self._states: dict[str, ModuleState] = {}
        self._types: dict[str, ModuleType] = {}
        self._disabled_actions: dict[str, dict[str, str]] = {}
        self._event_subscriptions: dict[str, list[tuple[str, Any]]] = {}


    def get_state(self, module_id: str) -> ModuleState:
        """Return the current lifecycle state of a module."""
        return self._states.get(module_id, ModuleState.LOADED)

    def get_type(self, module_id: str) -> ModuleType:
        """Return the module type (SYSTEM or USER)."""
        return self._types.get(module_id, ModuleType.USER)

    def set_type(self, module_id: str, mtype: ModuleType) -> None:
        """Set the module type.  Typically called once during server startup."""
        self._types[module_id] = mtype

    def is_system_module(self, module_id: str) -> bool:
        """Check if a module is a protected system module."""
        return (
            module_id in SYSTEM_MODULE_IDS
            or self._types.get(module_id) == ModuleType.SYSTEM
        )


    async def start_module(self, module_id: str) -> None:
        """Transition a module to ACTIVE state (LOADED/PAUSED/DISABLED/ERROR -> ACTIVE).

        Calls ``on_start()`` (from LOADED/DISABLED/ERROR) or ``on_resume()``
        (from PAUSED) on the module instance.

        On first start (from LOADED/DISABLED/ERROR), restores saved state
        from the state store (if available) and auto-subscribes to declared
        event topics.
        """
        current = self.get_state(module_id)
        module = self._registry.get(module_id)

        if current == ModuleState.PAUSED:
            self._validate_transition(module_id, current, ModuleState.STARTING)
            self._set_state(module_id, ModuleState.STARTING)
            try:
                await module.on_resume()
                self._set_state(module_id, ModuleState.ACTIVE)
                await self._emit_lifecycle_event(module_id, "resumed")
            except Exception as exc:
                self._set_state(module_id, ModuleState.ERROR)
                await self._emit_lifecycle_event(module_id, "resume_failed", error=str(exc))
                raise
        else:
            self._validate_transition(module_id, current, ModuleState.STARTING)
            self._set_state(module_id, ModuleState.STARTING)
            try:
                await self._restore_module_state(module_id, module)
                await module.on_start()
                self._set_state(module_id, ModuleState.ACTIVE)
                await self._emit_lifecycle_event(module_id, "started")
            except Exception as exc:
                self._set_state(module_id, ModuleState.ERROR)
                await self._emit_lifecycle_event(module_id, "start_failed", error=str(exc))
                raise

        self._auto_subscribe_events(module_id, module)

        self._auto_register_services(module_id, module)

    async def stop_module(self, module_id: str) -> None:
        """Transition a module to DISABLED state.

        Saves the module's state snapshot before stopping, unsubscribes from
        event topics, then calls ``on_stop()`` on the module instance.

        Raises:
            ModuleLifecycleError: If the module is a system module.
        """
        current = self.get_state(module_id)

        if current in (ModuleState.DISABLED, ModuleState.LOADED):
            return

        self._validate_transition(module_id, current, ModuleState.STOPPING)
        self._set_state(module_id, ModuleState.STOPPING)

        module = self._registry.get(module_id)

        await self._save_module_state(module_id, module)

        self._auto_unsubscribe_events(module_id)

        self._auto_unregister_services(module_id)

        try:
            await module.on_stop()
            self._set_state(module_id, ModuleState.DISABLED)
            await self._emit_lifecycle_event(module_id, "stopped")
        except Exception as exc:
            self._set_state(module_id, ModuleState.ERROR)
            await self._emit_lifecycle_event(module_id, "stop_failed", error=str(exc))
            raise

    async def pause_module(self, module_id: str) -> None:
        """Transition a module to PAUSED state (ACTIVE -> PAUSED).

        Calls ``on_pause()`` on the module instance.
        """
        current = self.get_state(module_id)
        self._validate_transition(module_id, current, ModuleState.PAUSED)

        module = self._registry.get(module_id)
        try:
            await module.on_pause()
            self._set_state(module_id, ModuleState.PAUSED)
            await self._emit_lifecycle_event(module_id, "paused")
        except Exception as exc:
            self._set_state(module_id, ModuleState.ERROR)
            await self._emit_lifecycle_event(module_id, "pause_failed", error=str(exc))
            raise

    async def resume_module(self, module_id: str) -> None:
        """Transition a module from PAUSED to ACTIVE.

        Calls ``on_resume()`` on the module instance.
        """
        await self.start_module(module_id)

    async def restart_module(self, module_id: str) -> None:
        """Stop and then start a module."""
        current = self.get_state(module_id)
        if current not in (ModuleState.LOADED, ModuleState.DISABLED):
            await self.stop_module(module_id)
        await self.start_module(module_id)


    async def start_all(self) -> dict[str, str]:
        """Start all registered modules that are in LOADED state.

        Returns a dict of module_id -> result ("ok", "skipped", or error message).
        """
        results: dict[str, str] = {}
        for module_id in self._registry.list_available():
            state = self.get_state(module_id)
            if state not in (ModuleState.LOADED, ModuleState.DISABLED):
                results[module_id] = "skipped"
                continue
            try:
                await self.start_module(module_id)
                results[module_id] = "ok"
            except Exception as exc:
                results[module_id] = str(exc)
                log.warning("start_all_module_failed module_id=%s error=%s", module_id, exc, exc_info=True)
        return results

    async def stop_all(self) -> None:
        """Stop all active modules in reverse registration order."""
        for module_id in reversed(self._registry.list_available()):
            state = self.get_state(module_id)
            if state in (ModuleState.ACTIVE, ModuleState.PAUSED):
                try:
                    await self.stop_module(module_id)
                except Exception as exc:
                    log.warning("stop_all_module_failed module_id=%s error=%s", module_id, exc, exc_info=True)


    def disable_action(self, module_id: str, action: str, reason: str = "") -> None:
        """Disable a specific action on a module."""
        if module_id not in self._disabled_actions:
            self._disabled_actions[module_id] = {}
        self._disabled_actions[module_id][action] = reason
        log.info("action_disabled", module_id=module_id, action=action, reason=reason)

    def enable_action(self, module_id: str, action: str) -> None:
        """Re-enable a previously disabled action."""
        if module_id in self._disabled_actions:
            self._disabled_actions[module_id].pop(action, None)
            if not self._disabled_actions[module_id]:
                del self._disabled_actions[module_id]
        log.info("action_enabled", module_id=module_id, action=action)

    def is_action_enabled(self, module_id: str, action: str) -> bool:
        """Check if an action is enabled (not disabled)."""
        return action not in self._disabled_actions.get(module_id, {})

    def get_disabled_actions(self, module_id: str) -> dict[str, str]:
        """Return disabled actions and their reasons for a module."""
        return dict(self._disabled_actions.get(module_id, {}))


    async def install_module(self, module_id: str) -> None:
        """Call on_install() on a newly installed module and emit event."""
        module = self._registry.get(module_id)
        try:
            await module.on_install()
            await self._emit_lifecycle_event(module_id, "installed")
        except Exception as exc:
            self._set_state(module_id, ModuleState.ERROR)
            await self._emit_lifecycle_event(module_id, "install_failed", error=str(exc))
            raise

    async def upgrade_module(self, module_id: str, old_version: str) -> None:
        """Call on_update() on an upgraded module and emit event."""
        module = self._registry.get(module_id)
        try:
            await module.on_update(old_version)
            await self._emit_lifecycle_event(
                module_id, "upgraded", old_version=old_version
            )
        except Exception as exc:
            self._set_state(module_id, ModuleState.ERROR)
            await self._emit_lifecycle_event(
                module_id, "upgrade_failed", old_version=old_version, error=str(exc)
            )
            raise

    async def uninstall_module(self, module_id: str) -> None:
        """Clean up lifecycle state for an uninstalled module and emit event."""
        self._states.pop(module_id, None)
        self._disabled_actions.pop(module_id, None)
        await self._emit_lifecycle_event(module_id, "uninstalled")


    async def update_config(self, module_id: str, config: dict[str, Any]) -> None:
        """Update a module's runtime configuration.

        If the module declares a CONFIG_MODEL, validates the config dict
        against the schema before applying. Raises ValidationError on
        invalid input.
        """
        module = self._registry.get(module_id)
        if module.CONFIG_MODEL is not None:
            module.CONFIG_MODEL.model_validate(config)
        await module.on_config_update(config)
        await self._emit_lifecycle_event(module_id, "config_updated")


    def get_full_report(self) -> dict[str, dict[str, Any]]:
        """Return a comprehensive status report for all modules."""
        report: dict[str, dict[str, Any]] = {}
        for module_id in self._registry.list_available():
            report[module_id] = {
                "state": self.get_state(module_id).value,
                "type": self.get_type(module_id).value,
                "disabled_actions": self.get_disabled_actions(module_id),
            }
        return report


    def _set_state(self, module_id: str, state: ModuleState) -> None:
        """Set a module's state (internal - no validation)."""
        old = self._states.get(module_id, ModuleState.LOADED)
        self._states[module_id] = state
        log.debug(
            "module_state_changed",
            module_id=module_id,
            old_state=old.value,
            new_state=state.value,
        )

    def _validate_transition(
        self, module_id: str, current: ModuleState, target: ModuleState
    ) -> None:
        """Validate that a state transition is allowed."""
        valid_targets = VALID_TRANSITIONS.get(current, set())
        if target not in valid_targets:
            raise ModuleLifecycleError(
                module_id=module_id,
                current_state=current.value,
                target_state=target.value,
            )

    async def _emit_lifecycle_event(
        self, module_id: str, event_type: str, **extra: Any
    ) -> None:
        """Emit a lifecycle event to the EventBus."""
        from digitorn.core.events.models import UniversalEvent

        topic = f"{TOPIC_MODULES}.{module_id}.{event_type}"
        event = UniversalEvent(
            topic=topic,
            source=f"lifecycle:{module_id}",
            event_type=event_type,
            data={
                "event": f"module_{event_type}",
                "module_id": module_id,
                "state": self.get_state(module_id).value,
                "type": self.get_type(module_id).value,
                **extra,
            },
        )
        try:
            await self._event_bus.publish(event)
        except Exception as exc:
            log.warning("lifecycle_event_emit_failed error=%s", exc, exc_info=True)


    def _auto_subscribe_events(self, module_id: str, module: Any) -> None:
        """Subscribe a module to its declared event topics."""
        try:
            manifest = module.get_manifest()
        except Exception:
            log.debug("manifest_unavailable_for_event_subscribe", module_id=module_id, exc_info=True)
            return

        topics = getattr(manifest, "subscribes_events", [])
        if not topics:
            return

        subscriptions: list[tuple[str, Any]] = []
        for topic in topics:
            async def _callback(
                event: "UniversalEvent", _mod: Any = module
            ) -> None:
                await _mod.on_event(event.topic, event.model_dump())

            self._event_bus.subscribe(topic, _callback)
            subscriptions.append((topic, _callback))
            log.debug(
                "event_auto_subscribed",
                module_id=module_id,
                topic=topic,
            )

        self._event_subscriptions[module_id] = subscriptions

    def _auto_unsubscribe_events(self, module_id: str) -> None:
        """Unsubscribe a module from all its event topics."""
        subscriptions = self._event_subscriptions.pop(module_id, [])
        for topic, callback in subscriptions:
            self._event_bus.unsubscribe(topic, callback)
            log.debug(
                "event_auto_unsubscribed",
                module_id=module_id,
                topic=topic,
            )


    def _auto_register_services(self, module_id: str, module: Any) -> None:
        """Register a module's declared services on the service bus."""
        if self._service_bus is None:
            return

        try:
            manifest = module.get_manifest()
        except Exception:
            log.debug("manifest_unavailable_for_service_register", module_id=module_id, exc_info=True)
            return

        services = getattr(manifest, "provides_services", [])
        if not services:
            return

        for service_name in services:
            self._service_bus.register_service(
                name=service_name,
                provider=module,
                description=f"Auto-registered from module '{module_id}'",
            )
            log.debug(
                "service_auto_registered",
                module_id=module_id,
                service=service_name,
            )

    def _auto_unregister_services(self, module_id: str) -> None:
        """Unregister a module's services from the service bus."""
        if self._service_bus is None:
            return

        try:
            module = self._registry.get(module_id)
            manifest = module.get_manifest()
        except Exception:
            log.debug("manifest_unavailable_for_service_unregister", module_id=module_id, exc_info=True)
            return

        services = getattr(manifest, "provides_services", [])
        for service_name in services:
            self._service_bus.unregister_service(service_name)
            log.debug(
                "service_auto_unregistered",
                module_id=module_id,
                service=service_name,
            )


    async def _save_module_state(self, module_id: str, module: Any) -> None:
        """Persist module's state_snapshot() to the state store."""
        if self._state_store is None:
            return
        try:
            snapshot = module.state_snapshot()
            if snapshot:
                await self._state_store.save(module_id, snapshot)
                log.debug("module_state_saved", module_id=module_id)
        except Exception as exc:
            log.warning(
                "module_state_save_failed",
                module_id=module_id,
                error=str(exc),
                exc_info=True,
            )

    async def _restore_module_state(self, module_id: str, module: Any) -> None:
        """Load saved state and call restore_state() on the module."""
        if self._state_store is None:
            return
        try:
            saved = await self._state_store.load(module_id)
            if saved:
                await module.restore_state(saved)
                log.debug("module_state_restored", module_id=module_id)
        except Exception as exc:
            log.warning(
                "module_state_restore_failed",
                module_id=module_id,
                error=str(exc),
                exc_info=True,
            )
