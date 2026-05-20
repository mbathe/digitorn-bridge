"""ChannelRegistry - two-level type/instance management with retry and discovery."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from digitorn.core.app.channels.base import (
    BaseOutputChannel,
    ChannelHealth,
    ChannelMeta,
    ChannelPayload,
    DeliveryContext,
    DeliveryResult,
)

logger = logging.getLogger(__name__)

_ENTRY_POINT_GROUP = "digitorn.channels"

_UserResolver = None


def _get_resolver_class():
    global _UserResolver
    if _UserResolver is None:
        from digitorn.core.app.channels.resolver import UserResolver
        _UserResolver = UserResolver
    return _UserResolver


class ChannelRegistry:
    """Global registry of output channel types and instances."""

    def __init__(self) -> None:
        self._types: dict[str, type[BaseOutputChannel]] = {}
        self._instances: dict[str, BaseOutputChannel] = {}
        self._instance_meta: dict[str, tuple[str, str]] = {}
        self._resolvers: dict[str, Any] = {}
        self._default_instance: str = "llm_notification"


    def register_type(self, channel_class: type[BaseOutputChannel]) -> None:
        """Register a channel type (class) by its CHANNEL_ID."""
        channel_id = channel_class.CHANNEL_ID
        if not channel_id:
            raise ValueError(
                f"Channel class {channel_class.__name__} has empty CHANNEL_ID"
            )
        if channel_id in self._types:
            existing = self._types[channel_id]
            if existing is not channel_class:
                logger.warning(
                    "channel_type_override: %s was %s, now %s",
                    channel_id,
                    existing.__name__,
                    channel_class.__name__,
                )
        self._types[channel_id] = channel_class
        logger.debug("channel_type_registered: %s → %s", channel_id, channel_class.__name__)

    def get_type(self, channel_id: str) -> type[BaseOutputChannel] | None:
        """Get a registered channel type by ID."""
        return self._types.get(channel_id)

    def list_types(self) -> list[ChannelMeta]:
        """List all registered channel types with their metadata."""
        result: list[ChannelMeta] = []
        for cls in self._types.values():
            try:
                instance = cls()
                result.append(instance.channel_meta())
            except Exception:
                result.append(ChannelMeta(
                    channel_id=cls.CHANNEL_ID,
                    name=cls.CHANNEL_NAME,
                    version=cls.CHANNEL_VERSION,
                ))
        return result

    def type_ids(self) -> list[str]:
        """List all registered channel type IDs."""
        return list(self._types.keys())


    def discover_plugins(self) -> int:
        """Scan Python entry points for plugin channels."""
        count = 0
        try:
            from importlib.metadata import entry_points

            eps = entry_points()
            if hasattr(eps, "select"):
                group = eps.select(group=_ENTRY_POINT_GROUP)
            else:
                group = eps.get(_ENTRY_POINT_GROUP, [])

            for ep in group:
                try:
                    cls = ep.load()
                    if (
                        isinstance(cls, type)
                        and issubclass(cls, BaseOutputChannel)
                        and cls is not BaseOutputChannel
                    ):
                        self.register_type(cls)
                        count += 1
                        logger.info(
                            "channel_plugin_loaded: %s from %s",
                            cls.CHANNEL_ID, ep.name,
                        )
                    else:
                        logger.warning(
                            "channel_plugin_skip: %s is not a BaseOutputChannel subclass",
                            ep.name,
                        )
                except Exception as exc:
                    logger.warning(
                        "channel_plugin_error: %s - %s", ep.name, exc,
                    )
        except Exception as exc:
            logger.debug("channel_plugin_discovery_error: %s", exc)
        return count


    def create_instance(
        self,
        instance_name: str,
        channel_type: str,
        config: dict[str, Any],
        *,
        app_id: str = "",
        resolver_config: dict[str, Any] | None = None,
    ) -> BaseOutputChannel:
        """Create a channel instance from a registered type."""
        cls = self._types.get(channel_type)
        if cls is None:
            raise ValueError(
                f"Unknown channel type: '{channel_type}'. "
                f"Available: {sorted(self._types.keys())}"
            )

        instance = cls(channel_config=config)
        self._instances[instance_name] = instance
        self._instance_meta[instance_name] = (channel_type, app_id)

        if resolver_config:
            from digitorn.core.app.channels.resolver import UserResolverConfig
            ResolverCls = _get_resolver_class()
            rc = UserResolverConfig.from_dict(resolver_config)
            self._resolvers[instance_name] = ResolverCls(rc)
            logger.debug(
                "channel_resolver_attached: %s (module=%s, action=%s)",
                instance_name, rc.module, rc.action,
            )

        logger.debug(
            "channel_instance_created: %s (type=%s, app=%s)",
            instance_name, channel_type, app_id,
        )
        return instance

    def register_instance(
        self,
        instance_name: str,
        instance: BaseOutputChannel,
        *,
        app_id: str = "",
    ) -> None:
        """Register a pre-built channel instance."""
        self._instances[instance_name] = instance
        self._instance_meta[instance_name] = (instance.CHANNEL_ID, app_id)

    def get_instance(self, instance_name: str) -> BaseOutputChannel | None:
        """Get a channel instance by name."""
        return self._instances.get(instance_name)

    async def start_instance(self, instance_name: str) -> None:
        """Start a channel instance (call on_start)."""
        instance = self._instances.get(instance_name)
        if instance is not None:
            await instance.on_start()

    async def stop_instance(self, instance_name: str) -> None:
        """Stop a channel instance (call on_stop)."""
        instance = self._instances.get(instance_name)
        if instance is not None:
            await instance.on_stop()

    def remove_instance(self, instance_name: str) -> bool:
        """Remove a channel instance. Returns True if it existed."""
        existed = instance_name in self._instances
        self._instances.pop(instance_name, None)
        self._instance_meta.pop(instance_name, None)
        self._resolvers.pop(instance_name, None)
        return existed

    async def stop_and_remove_for_app(self, app_id: str) -> int:
        """Stop and remove all channel instances for an app."""
        to_remove = [
            name for name, (_, aid) in self._instance_meta.items()
            if aid == app_id
        ]
        for name in to_remove:
            await self.stop_instance(name)
            self.remove_instance(name)
        return len(to_remove)

    def list_instances(self) -> dict[str, str]:
        """List all instances: instance_name → channel_type."""
        return {
            name: meta[0] for name, meta in self._instance_meta.items()
        }


    def set_resolver_modules(
        self,
        instance_name: str,
        modules: dict[str, Any],
    ) -> None:
        """Inject module references into a channel's resolver."""
        resolver = self._resolvers.get(instance_name)
        if resolver is not None:
            resolver.set_modules(modules)

    def set_resolver_user_store(
        self,
        instance_name: str,
        user_store: Any,
    ) -> None:
        """Inject UserStore into a channel's resolver."""
        resolver = self._resolvers.get(instance_name)
        if resolver is not None:
            resolver.set_user_store(user_store)

    def get_resolver(self, instance_name: str) -> Any | None:
        """Get the UserResolver for an instance (or None)."""
        return self._resolvers.get(instance_name)

    async def deliver(
        self,
        channel_name: str,
        app_id: str,
        payload: ChannelPayload | dict[str, Any],
        config: dict[str, Any] | None = None,
        *,
        session_id: str | None = None,
    ) -> DeliveryResult:
        """Route a notification to the specified channel instance."""
        if isinstance(payload, dict):
            payload = ChannelPayload.from_dict(payload)

        config = config or {}

        resolved_name = channel_name
        instance = self._instances.get(channel_name)
        if instance is None:
            for iname, (itype, _) in self._instance_meta.items():
                if itype == channel_name:
                    instance = self._instances.get(iname)
                    resolved_name = iname
                    break

        if instance is None:
            instance = self._instances.get(self._default_instance)
            resolved_name = self._default_instance
            if instance is None:
                return DeliveryResult(
                    success=False,
                    channel_id=channel_name,
                    error=f"Channel '{channel_name}' not found (no fallback available)",
                )
            logger.warning(
                "channel_not_found: '%s', falling back to '%s'",
                channel_name, self._default_instance,
            )

        final_config = config
        resolver = self._resolvers.get(resolved_name)
        if resolver is not None and session_id:
            try:
                final_config = await resolver.resolve(session_id, config)
                logger.debug(
                    "channel_resolver_resolved: %s session=%s fields=%s",
                    resolved_name, session_id, sorted(final_config.keys()),
                )
            except Exception as exc:
                logger.warning(
                    "channel_resolver_error: %s session=%s error=%s",
                    resolved_name, session_id, exc,
                )
                final_config = config
        elif session_id:
            context = DeliveryContext(
                app_id=app_id,
                session_id=session_id,
                output_config=config,
            )
            try:
                final_config = await instance.resolve_recipient(context)
            except Exception as exc:
                logger.warning(
                    "channel_resolve_recipient_error: %s error=%s",
                    resolved_name, exc,
                )
                final_config = config

        return await self._deliver_with_retry(instance, app_id, payload, final_config)

    async def deliver_multi(
        self,
        channel_names: list[str],
        app_id: str,
        payload: ChannelPayload | dict[str, Any],
        config: dict[str, Any] | None = None,
        *,
        session_id: str | None = None,
    ) -> list[DeliveryResult]:
        """Fan out a notification to multiple channels concurrently."""
        tasks = [
            self.deliver(name, app_id, payload, config, session_id=session_id)
            for name in channel_names
        ]
        # return_exceptions=True so one channel failure doesn't cancel the others
        raw_results = await asyncio.gather(*tasks, return_exceptions=True)
        results: list[DeliveryResult] = []
        for i, r in enumerate(raw_results):
            if isinstance(r, BaseException):
                logger.warning(
                    "channel_delivery_exception channel=%s: %s",
                    channel_names[i] if i < len(channel_names) else "?", r,
                )
                results.append(DeliveryResult(
                    success=False,
                    channel_id=channel_names[i] if i < len(channel_names) else "unknown",
                    error=f"{type(r).__name__}: {r}",
                    retryable=False,
                ))
            else:
                results.append(r)
        return results

    async def _deliver_with_retry(
        self,
        instance: BaseOutputChannel,
        app_id: str,
        payload: ChannelPayload,
        config: dict[str, Any],
    ) -> DeliveryResult:
        """Execute delivery with retry loop based on channel policy."""
        policy = instance.retry_policy()
        last_result: DeliveryResult | None = None

        for attempt in range(1 + policy.max_retries):
            start = time.monotonic()
            try:
                result = await instance.deliver(app_id, payload, config)
                elapsed_ms = (time.monotonic() - start) * 1000

                if result.success or result.buffered:
                    instance._record_success(elapsed_ms)
                    await self._record_channel_sent(
                        instance, config, result, success=True,
                    )
                    return result

                if not result.retryable:
                    instance._record_failure(result.error or "delivery failed")
                    await self._record_channel_sent(
                        instance, config, result, success=False,
                    )
                    return result

                last_result = result

            except Exception as exc:
                elapsed_ms = (time.monotonic() - start) * 1000
                last_result = DeliveryResult(
                    success=False,
                    channel_id=instance.CHANNEL_ID,
                    error=str(exc),
                    retryable=True,
                )
                instance._record_failure(str(exc))

            if attempt < policy.max_retries:
                delay = min(
                    policy.backoff_base * (policy.backoff_multiplier ** attempt),
                    policy.backoff_max,
                )
                logger.debug(
                    "channel_retry: %s attempt=%d/%d delay=%.1fs error=%s",
                    instance.CHANNEL_ID,
                    attempt + 1,
                    policy.max_retries,
                    delay,
                    last_result.error if last_result else "?",
                )
                await asyncio.sleep(delay)

        # All retries exhausted - record the final failure too.
        if last_result is not None:
            await self._record_channel_sent(
                instance, config, last_result, success=False,
            )
            return last_result
        final = DeliveryResult(
            success=False,
            channel_id=instance.CHANNEL_ID,
            error="All retry attempts exhausted",
        )
        await self._record_channel_sent(
            instance, config, final, success=False,
        )
        return final

    async def _record_channel_sent(
        self,
        instance: BaseOutputChannel,
        config: dict[str, Any],
        result: DeliveryResult,
        *,
        success: bool,
    ) -> None:
        """Push a `channel_sent` row into the current activation's timeline."""
        try:
            from digitorn.core.runtime.modes.background import (
                get_current_activation_recorder,
            )
        except Exception:
            return

        recorder = get_current_activation_recorder()
        if recorder is None:
            return

        target = ""
        for key in ("to", "recipient", "recipients", "url", "channel", "webhook"):
            val = config.get(key) if isinstance(config, dict) else None
            if val:
                target = str(val)[:200]
                break
        if not target:
            target = getattr(instance, "CHANNEL_ID", "") or "unknown"

        channel_name = (
            getattr(instance, "_instance_name", None)
            or getattr(instance, "CHANNEL_ID", None)
            or "unknown"
        )

        try:
            await recorder.record_channel_sent(
                str(channel_name),
                target,
                success=success,
                error=(result.error if not success else None),
            )
        except Exception as exc:
            logger.debug("channel_sent record failed: %s", exc)


    async def health(self, instance_name: str) -> ChannelHealth | None:
        """Get health status of a specific channel instance."""
        instance = self._instances.get(instance_name)
        if instance is None:
            return None
        return await instance.health_check()

    async def health_all(self) -> dict[str, ChannelHealth]:
        """Get health status of all channel instances."""
        result: dict[str, ChannelHealth] = {}
        for name, instance in self._instances.items():
            try:
                result[name] = await instance.health_check()
            except Exception as exc:
                result[name] = ChannelHealth(
                    status="down", last_error=str(exc),
                )
        return result


    async def validate_instance(self, instance_name: str) -> list[str]:
        """Validate a channel instance's config. Returns error list."""
        instance = self._instances.get(instance_name)
        if instance is None:
            return [f"Instance '{instance_name}' not found"]
        return await instance.validate_config()


    def register(self, channel: BaseOutputChannel) -> None:
        """Legacy: register a channel instance by its CHANNEL_ID."""
        self.register_instance(channel.CHANNEL_ID, channel)
        cls = type(channel)
        if cls.CHANNEL_ID and cls.CHANNEL_ID not in self._types:
            self._types[cls.CHANNEL_ID] = cls

    def get(self, channel_id: str) -> BaseOutputChannel | None:
        """Legacy: get a channel by CHANNEL_ID."""
        return self.get_instance(channel_id)

    def list_channels(self) -> list[str]:
        """Legacy: list all instance names."""
        return list(self._instances.keys())
