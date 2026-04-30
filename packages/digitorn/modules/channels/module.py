"""Unified bidirectional channels module.

Provides inbound event reception (webhooks, cron, email, file watch, RSS, queue)
and outbound message delivery through the same or different channels. Everything
is configured via YAML in the ``providers:`` block.

Security is enforced at every layer:
- Inbound: payload size limits, HMAC/API-key auth, content-type whitelist,
  payload sanitization, per-source rate limiting.
- Outbound: SSRF protection, secret filtering, header masking.
- Templates: no eval/exec, no runtime secret access, single-pass only.
- Isolation: each adapter gets its own config copy, no cross-adapter access.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, Field

from digitorn.modules.base import ActionResult, BaseModule, Platform
from digitorn.modules.channels.adapter import (
    BaseChannelAdapter,
    InboundEvent,
)
from digitorn.modules.channels.adapters import get_adapter_class, list_adapter_types
from digitorn.modules.channels.params import (
    BroadcastParams,
    ListProvidersParams,
    PauseProviderParams,
    ProviderHistoryParams,
    ProviderStatusParams,
    ReplyParams,
    ResumeProviderParams,
    SendMessageParams,
    SimulateEventParams,
    StatsParams,
    TestSendParams,
)
from digitorn.modules.channels.security import (
    filter_secrets,
    generate_webhook_token,
    sanitize_payload,
)
from digitorn.modules.channels.session_manager import ChannelSessionManager
from digitorn.modules.decorators import action
from digitorn.modules.manifest import ConstraintSpec, ModuleManifest

logger = logging.getLogger(__name__)


# ── Config model (compile-time validation via CONFIG_MODEL) ──────


class ChannelsConfig(BaseModel):
    """Pydantic config for the channels module (validated at compile time).

    ``providers`` stays permissive (``dict[str, dict]``) because each adapter
    has its own shape and the compiler's template pass temporarily strips
    ``activation`` blocks before variable resolution.
    """

    model_config = {"extra": "forbid"}

    workspace: str = Field(default="", description="Auto-injected by the daemon.")
    providers: dict[str, dict[str, Any]] = Field(
        default_factory=dict,
        description="Named provider instances - each is a free-form adapter config.",
    )
    default_agent: str = Field(default="")
    max_turns: int = Field(default=30, ge=1, le=200)
    timeout: float = Field(default=120.0, ge=5.0, le=3600.0)
    history_limit: int = Field(default=200, ge=0, le=10000)
    secret_filter_enabled: bool = Field(default=True)


# ── Config models ────────────────────────────────────────────────────


class PrepareStep(BaseModel):
    """Pre-activation step: call a tool via service_bus."""

    action: str = Field(..., description="Module action to call (e.g. 'database.fetch_results').")
    params: dict[str, Any] = Field(default_factory=dict, description="Params with {{event.*}} templates.")
    as_field: str = Field(..., alias="as", description="Store result under this name in event context.")


class FilterCondition(BaseModel):
    """Drop events that don't match."""

    field: str = Field(..., description="Dot path to check (e.g. 'event.payload.status').")
    equals: Any | None = None
    not_equals: Any | None = None
    contains: str | None = None
    gt: float | None = None
    lt: float | None = None


class RouteRule(BaseModel):
    """Route to an agent based on a field value."""

    match: str | None = None
    default: bool = False
    agent: str = Field(default="", description="Target agent ID.")


class RouteConfig(BaseModel):
    """Dynamic routing based on event data."""

    field: str = Field(..., description="Dot path to match on.")
    rules: list[RouteRule] = Field(default_factory=list)


class ActivationConfig(BaseModel):
    """How to start the app when an event arrives."""

    agent: str = Field(default="", description="Target agent. Empty = entry agent.")
    session: str = Field(default="per_event", description="Session strategy: per_event, shared, or template.")
    message: str = Field(default="", description="User message template with {{var}} placeholders.")
    context: str = Field(default="", description="Extra system prompt context template.")
    expose_data: bool = Field(default=False, description="Expose event data to the agent context.")
    filter: list[FilterCondition] = Field(default_factory=list, description="Drop events not matching.")
    prepare: list[PrepareStep] = Field(default_factory=list, description="Pre-activation tool calls.")
    route: RouteConfig | None = Field(default=None, description="Dynamic agent routing.")
    reply: str = Field(default="none", description="Reply mode: 'auto', 'none', 'explicit'.")


class ProviderConfig(BaseModel):
    """Configuration for a single channel provider instance."""

    adapter: str = Field(..., description="Adapter type name (webhook, cron, email, ...).")
    config: dict[str, Any] = Field(default_factory=dict, description="Adapter-specific config.")
    activation: ActivationConfig = Field(default_factory=ActivationConfig, description="Activation pipeline config.")
    enabled: bool = Field(default=True, description="Whether this provider is active.")
    max_concurrent: int = Field(default=5, ge=1, le=100, description="Max concurrent activations.")


class ChannelsModuleConfig(BaseModel):
    """Top-level channels module config (from YAML)."""

    providers: dict[str, ProviderConfig] = Field(default_factory=dict, description="Named provider instances.")
    default_agent: str = Field(default="", description="Default agent for activations.")
    max_turns: int = Field(default=30, ge=1, le=200, description="Max agent turns per activation.")
    timeout: float = Field(default=120.0, ge=5.0, le=3600.0, description="Timeout per activation (seconds).")
    history_limit: int = Field(default=200, ge=0, le=10000, description="Max events to keep in history.")
    secret_filter_enabled: bool = Field(default=True, description="Filter secrets from outbound messages.")


# ── Active provider state ────────────────────────────────────────────


@dataclass
class ActiveProvider:
    """Runtime state for an active provider instance."""

    name: str
    config: ProviderConfig
    adapter: BaseChannelAdapter
    listener_task: asyncio.Task[None] | None = None
    semaphore: asyncio.Semaphore | None = None
    webhook_token: str = ""
    status: str = "active"  # active, paused, stopped, error
    events_received: int = 0
    events_sent: int = 0
    last_event_at: float = 0.0
    last_error: str = ""
    activation_tasks: dict[str, asyncio.Task[Any]] = field(default_factory=dict)


# ── Event history entry ──────────────────────────────────────────────


@dataclass
class EventRecord:
    """Minimal record for event history."""

    event_id: str
    provider: str
    direction: str  # "inbound" or "outbound"
    source: str
    timestamp: float
    success: bool
    error: str = ""


# ── Module ───────────────────────────────────────────────────────────


class ChannelsModule(BaseModule):
    """Unified bidirectional channels module."""

    MODULE_ID = "channels"
    VERSION = "1.0.0"
    SUPPORTED_PLATFORMS = [Platform.ALL]
    CONFIG_MODEL = ChannelsConfig

    CONSTRAINTS = [
        ConstraintSpec(
            name="allowed_adapters",
            type="string_list",
            description="Restrict which adapter types can be used.",
            scope="module",
        ),
        ConstraintSpec(
            name="max_providers",
            type="integer",
            description="Maximum number of provider instances.",
            scope="module",
            default=20,
        ),
    ]

    # Declarative credential slot - one slot covers Telegram bots,
    # Slack apps, Discord apps, SMTP/IMAP, Twilio. Each adapter reads
    # the resolved fields under `config.providers.<id>.<field>`. The
    # injector spreads the fields by handler:
    #   - bearer_token  -> token
    #   - basic_auth    -> user / password
    #   - multi_field   -> raw key/value
    @classmethod
    def _channel_slots(cls) -> list[Any]:
        from digitorn.core.credentials.slot import CredentialSlot
        return [
            CredentialSlot(
                id="channel_credential",
                label="Channel adapter authentication",
                handler_types=[
                    "api_key", "bearer_token", "basic_auth",
                    "multi_field", "oauth2", "oauth2_pkce",
                ],
                providers=[
                    "telegram", "slack", "slack_oauth", "discord",
                    "discord_oauth", "twilio", "smtp", "imap",
                ],
                scopes_preferred=["per_app_shared", "per_user"],
                scopes_allowed=None,
                inject={
                    # Adapter configs key auth fields differently.
                    # The injector lays them all under a `auth.*`
                    # subtree; adapters read whatever applies.
                    "token":          "{block}.config.auth.token",
                    "bot_token":      "{block}.config.auth.bot_token",
                    "api_key":        "{block}.config.auth.api_key",
                    "secret_key":     "{block}.config.auth.secret_key",
                    "account_sid":    "{block}.config.auth.account_sid",
                    "auth_token":     "{block}.config.auth.auth_token",
                    "user":           "{block}.config.auth.user",
                    "password":       "{block}.config.auth.password",
                    "host":           "{block}.config.auth.host",
                    "port":           "{block}.config.auth.port",
                    "signing_secret": "{block}.config.auth.signing_secret",
                    "app_token":      "{block}.config.auth.app_token",
                },
                required=False,
                help=(
                    "Optional auth credential for the channel adapter "
                    "(Telegram bot, Slack app, SMTP, etc.). When unset, "
                    "the adapter uses values from the YAML config."
                ),
            ),
        ]

    @classmethod
    def _build_credential_slots(cls) -> list[Any]:
        return cls._channel_slots()

    @property
    def credential_slots(self) -> list[Any]:
        return self.__class__._channel_slots()

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._app_id: str = "default"
        self._providers: dict[str, ActiveProvider] = {}
        self._session_mgr = ChannelSessionManager(app_id=self._app_id)
        self._event_history: deque[EventRecord] = deque(maxlen=200)
        self._pipeline: Any = None  # Set in on_start
        self._config_obj: ChannelsModuleConfig | None = None
        # Injected by bootstrap
        self._runtime_app: Any = None
        self._hook_runner: Any = None

    def get_prompt_sections(self) -> list[dict[str, Any]]:
        return []  # Actions are self-descriptive via schema

    def get_manifest(self) -> ModuleManifest:
        return ModuleManifest.from_module(self).model_copy(
            update={
                "description": (
                    "Unified bidirectional channels - receive events from webhooks, "
                    "cron, email, file watchers, RSS, queues and respond through "
                    "the same or different channels."
                ),
                "author": "Digitorn Team",
                "tags": ["core", "channels", "io", "triggers"],
            }
        )

    # ── Lifecycle ────────────────────────────────────────────────
    #
    # Three phases:
    #   1. on_start / on_config_update (deploy time)
    #      → parse config, create adapters, validate - NO listeners yet
    #   2. start_listeners (run time - called by run_background)
    #      → launch inbound listener tasks (cron loops, file watchers, etc.)
    #      → at this point _runtime_app is wired, so agent_turn works
    #   3. on_stop (shutdown)
    #      → cancel listeners, close adapters, cleanup

    async def on_start(self) -> None:
        self._app_id = getattr(self, "_app_id_override", "default")
        logger.debug("channels_on_start app=%s", self._app_id)

    async def on_config_update(self, config: dict[str, Any]) -> None:
        """Called at deploy time. Parses config and creates adapters.

        Adapters are initialized (on_start) but listeners are NOT started.
        Listeners start in ``start_listeners()`` when the app is actually run.
        """
        if isinstance(config, ChannelsModuleConfig):
            self._config_obj = config
        elif isinstance(config, dict):
            self._config_obj = ChannelsModuleConfig(**config)
        else:
            self._config_obj = ChannelsModuleConfig()

        self._event_history = deque(maxlen=self._config_obj.history_limit or 200)

        from digitorn.modules.channels.pipeline import ActivationPipeline

        self._pipeline = ActivationPipeline(
            module=self,
            session_mgr=self._session_mgr,
            config=self._config_obj,
        )

        # Stop existing providers on hot-reload
        for name in list(self._providers):
            await self._stop_provider(name)
        self._providers.clear()

        # Create and initialize adapters (but don't start listeners)
        for name, prov_conf in self._config_obj.providers.items():
            if not prov_conf.enabled:
                logger.info("channel_provider_disabled name=%s", name)
                continue
            try:
                await self._init_provider(name, prov_conf)
            except Exception as exc:
                logger.error(
                    "channel_provider_init_failed name=%s error=%s",
                    name, exc, exc_info=True,
                )

        logger.info(
            "channels_configured app=%s providers=%d",
            self._app_id, len(self._providers),
        )

    async def start_listeners(self) -> None:
        """Start inbound listeners on all providers.

        Called by ``run_background()`` after RuntimeApp has wired
        ``_runtime_app`` and ``_hook_runner`` into this module.
        At this point agent_turn activation works.
        """
        # Restore shared sessions from DB (survives daemon restart)
        restored = await self._session_mgr.restore_active_sessions()
        if restored:
            logger.info("channels_sessions_restored count=%d app=%s", restored, self._app_id)

        for name, prov in self._providers.items():
            if prov.adapter.SUPPORTS_INBOUND and prov.listener_task is None:
                prov.listener_task = asyncio.create_task(
                    prov.adapter.start_listener(
                        lambda evt, _name=name: self._on_inbound_event(_name, evt)
                    ),
                    name=f"channel:{name}:listener",
                )
            # All providers are now active (outbound-only were ready, now active)
            prov.status = "active"

        active = [n for n, p in self._providers.items() if p.status == "active"]
        logger.info(
            "channels_listeners_started app=%s active=%s",
            self._app_id, active,
        )

    async def on_stop(self) -> None:
        for name, prov in list(self._providers.items()):
            await self._stop_provider(name)
        self._providers.clear()
        await self._session_mgr.cleanup()
        logger.info("channels_stopped app=%s", self._app_id)

    # ── Provider lifecycle ───────────────────────────────────────

    async def _init_provider(self, name: str, conf: ProviderConfig) -> None:
        """Create and initialize an adapter. Does NOT start the listener.

        Called at deploy time (on_config_update). The adapter is ready to
        send outbound messages, but inbound listeners start later in
        start_listeners().
        """
        adapter_cls = get_adapter_class(conf.adapter)

        # Each adapter gets its own config copy (isolation)
        adapter_config = dict(conf.config)
        adapter = adapter_cls(channel_config=adapter_config)
        await adapter.on_start()

        webhook_token = ""
        if conf.adapter == "webhook" and adapter.SUPPORTS_INBOUND:
            webhook_token = generate_webhook_token()

        active = ActiveProvider(
            name=name,
            config=conf,
            adapter=adapter,
            semaphore=asyncio.Semaphore(conf.max_concurrent),
            webhook_token=webhook_token,
            status="ready",  # Not "active" until listeners start
        )

        self._providers[name] = active
        logger.info(
            "channel_provider_ready name=%s adapter=%s inbound=%s outbound=%s",
            name, conf.adapter, adapter.SUPPORTS_INBOUND, adapter.SUPPORTS_OUTBOUND,
        )

    async def _stop_provider(self, name: str) -> None:
        """Stop a provider and cleanup."""
        prov = self._providers.get(name)
        if not prov:
            return

        # Cancel listener
        if prov.listener_task and not prov.listener_task.done():
            prov.listener_task.cancel()
            try:
                await prov.listener_task
            except asyncio.CancelledError:
                pass
            except Exception as exc:
                logger.warning("channel_listener_stop_error name=%s error=%s", name, exc)

        # Cancel active activations and await them
        pending = [t for t in prov.activation_tasks.values() if not t.done()]
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        prov.activation_tasks.clear()

        # Stop adapter
        try:
            await prov.adapter.stop_listener()
            await prov.adapter.on_stop()
        except Exception as exc:
            logger.warning("channel_adapter_stop_error name=%s error=%s", name, exc)

        prov.status = "stopped"

    # ── Inbound event handler ────────────────────────────────────

    async def _on_inbound_event(self, provider_name: str, event: InboundEvent) -> None:
        """Handle an inbound event from any adapter."""
        prov = self._providers.get(provider_name)
        if not prov or prov.status != "active":
            return

        prov.events_received += 1
        prov.last_event_at = time.time()

        # Record in history
        self._event_history.append(EventRecord(
            event_id=event.event_id,
            provider=provider_name,
            direction="inbound",
            source=event.source,
            timestamp=event.timestamp,
            success=True,
        ))

        # Persist the event to DB so it survives daemon crash
        await self._persist_event(event, provider_name)

        # Run activation in a bounded task
        if prov.semaphore is None:
            prov.semaphore = asyncio.Semaphore(prov.config.max_concurrent)

        async def _run_activation() -> None:
            async with prov.semaphore:
                try:
                    await self._pipeline.process_event(event, prov)
                except Exception as exc:
                    prov.last_error = str(exc)
                    logger.error(
                        "channel_activation_error provider=%s event=%s error=%s",
                        provider_name, event.event_id, exc, exc_info=True,
                    )
                finally:
                    prov.activation_tasks.pop(event.event_id, None)
                    await self._mark_event_processed(event.event_id)

        task = asyncio.create_task(
            _run_activation(),
            name=f"channel:{provider_name}:activate:{event.event_id}",
        )
        prov.activation_tasks[event.event_id] = task

    # ── Event persistence (queue survives daemon restart) ─────────

    async def _persist_event(self, event: InboundEvent, provider_name: str) -> None:
        """Persist an inbound event to DB before processing."""
        try:
            from digitorn.core.database import _engine, get_session_factory
            if _engine is None:
                return
            from digitorn.core.models import ActionExecution
            async with get_session_factory()() as db:
                db.add(ActionExecution(
                    app_id=self._app_id,
                    module_id="channels",
                    action=f"inbound:{provider_name}",
                    params={
                        "event_id": event.event_id,
                        "provider": provider_name,
                        "adapter": event.adapter_type,
                        "source": event.source,
                        "message": event.message[:500] if event.message else "",
                    },
                    status="started",
                    correlation_id=event.event_id,
                ))
                await db.commit()
        except Exception:
            logger.debug("event_persist_skipped", exc_info=True)

    async def _mark_event_processed(self, event_id: str) -> None:
        """Mark an event as processed in DB."""
        try:
            from digitorn.core.database import _engine, get_session_factory
            if _engine is None:
                return
            from digitorn.core.models import ActionExecution
            from sqlalchemy import update
            from datetime import datetime, timezone
            async with get_session_factory()() as db:
                await db.execute(
                    update(ActionExecution)
                    .where(ActionExecution.correlation_id == event_id)
                    .values(status="completed", completed_at=datetime.now(timezone.utc))
                )
                await db.commit()
        except Exception:
            logger.debug("event_mark_processed_skipped", exc_info=True)

    # ── Actions ──────────────────────────────────────────────────

    @action(
        description="Send a message through a specific channel provider.",
        params_model=SendMessageParams,
        risk_level="medium",
        side_effects=["network_io"],
        aliases=["envoyer_message", "send_on_channel"],
        tags=["channels", "send"],
        cli_label="Send",
        cli_param="channel",
    )
    async def send_message(self, params: SendMessageParams) -> ActionResult:
        prov = self._providers.get(params.provider)
        if not prov:
            return ActionResult(
                success=False,
                error=f"Unknown provider: '{params.provider}'. "
                f"Available: {list(self._providers.keys())}",
            )

        if not prov.adapter.SUPPORTS_OUTBOUND:
            return ActionResult(
                success=False,
                error=f"Provider '{params.provider}' does not support outbound messages.",
            )

        from digitorn.core.app.channels.base import ChannelPayload

        text = params.text
        if self._config_obj and self._config_obj.secret_filter_enabled:
            text = filter_secrets(text)

        payload = ChannelPayload(
            message=text,
            title=params.subject,
            metadata=params.metadata,
            thread_id=params.thread_id or None,
        )

        config: dict[str, Any] = {}
        if params.recipient:
            config["recipient"] = params.recipient

        result = await prov.adapter.deliver(
            app_id=self._app_id,
            payload=payload,
            config=config,
        )

        prov.events_sent += 1
        self._event_history.append(EventRecord(
            event_id=result.delivery_id or "",
            provider=params.provider,
            direction="outbound",
            source=params.recipient or "default",
            timestamp=time.time(),
            success=result.success,
            error=result.error or "",
        ))

        return ActionResult(
            success=result.success,
            data=result.to_dict(),
            error=result.error,
        )

    @action(
        description="Reply to the current inbound event on its originating channel.",
        params_model=ReplyParams,
        risk_level="medium",
        side_effects=["network_io"],
        aliases=["repondre", "reply_channel"],
        tags=["channels", "reply"],
        cli_label="Reply",
        cli_param="channel",
    )
    async def reply(self, params: ReplyParams) -> ActionResult:
        # Reply context is stored per-session by the pipeline
        ctx = self._context
        if not ctx:
            return ActionResult(success=False, error="No execution context available.")

        reply_ctx = ctx.metadata.get("_channel_reply_context")
        provider_name = ctx.metadata.get("_channel_provider")
        if not reply_ctx or not provider_name:
            return ActionResult(
                success=False,
                error="No inbound event context - reply is only available "
                "during a channel-triggered activation.",
            )

        prov = self._providers.get(provider_name)
        if not prov:
            return ActionResult(success=False, error=f"Provider '{provider_name}' not found.")

        text = params.text
        if self._config_obj and self._config_obj.secret_filter_enabled:
            text = filter_secrets(text)

        result = await prov.adapter.send_reply(reply_ctx, text)

        prov.events_sent += 1
        return ActionResult(
            success=result.success,
            data=result.to_dict(),
            error=result.error,
        )

    @action(
        description="Broadcast a message to multiple channel providers.",
        params_model=BroadcastParams,
        risk_level="high",
        side_effects=["network_io"],
        aliases=["diffuser", "broadcast_message"],
        tags=["channels", "send"],
        cli_label="Broadcast",
        cli_param="message",
    )
    async def broadcast(self, params: BroadcastParams) -> ActionResult:
        results = []
        for provider_name in params.providers:
            r = await self.send_message(SendMessageParams(
                provider=provider_name,
                text=params.text,
                subject=params.subject,
                metadata=params.metadata,
            ))
            results.append({"provider": provider_name, **r.to_dict()})

        successes = sum(1 for r in results if r.get("success"))
        return ActionResult(
            success=successes > 0,
            data={
                "results": results,
                "sent": successes,
                "failed": len(results) - successes,
            },
        )

    @action(
        description="List all configured channel providers and their status.",
        params_model=ListProvidersParams,
        risk_level="low",
        aliases=["lister_canaux", "list_channels"],
        tags=["channels", "info"],
        cli_label="Providers",
    )
    async def list_providers(self, params: ListProvidersParams) -> ActionResult:
        providers = []
        for name, prov in self._providers.items():
            info: dict[str, Any] = {
                "name": name,
                "adapter": prov.config.adapter,
                "inbound": prov.adapter.SUPPORTS_INBOUND,
                "outbound": prov.adapter.SUPPORTS_OUTBOUND,
                "enabled": prov.config.enabled,
            }
            if params.include_status:
                info.update({
                    "status": prov.status,
                    "events_received": prov.events_received,
                    "events_sent": prov.events_sent,
                    "active_activations": len(prov.activation_tasks),
                    "last_error": prov.last_error or None,
                })
            providers.append(info)

        # Return the catalog of adapter types the daemon CAN run
        # alongside the list of currently-configured instances. Without
        # this, BUG-056 manifested as an empty response that made the
        # feature look unimplemented - when in fact every adapter
        # listed below is shipped, just not pre-provisioned. The agent
        # (or a UI) can now discover "what can I configure?" without
        # reading the channels README.
        from digitorn.modules.channels.adapters import list_adapter_types
        available = list_adapter_types()

        return ActionResult(success=True, data={
            "providers": providers,
            "configured_count": len(providers),
            "available_adapters": available,
            "hint": (
                "Add `modules.channels.config.providers[]` in your app.yaml "
                "to configure one of the available adapters."
            ) if not providers else "",
        })

    @action(
        description="Get detailed status of a specific provider.",
        params_model=ProviderStatusParams,
        risk_level="low",
        tags=["channels", "info"],
        cli_label="Provider status",
        cli_param="provider_id",
    )
    async def provider_status(self, params: ProviderStatusParams) -> ActionResult:
        prov = self._providers.get(params.provider)
        if not prov:
            return ActionResult(success=False, error=f"Unknown provider: '{params.provider}'.")

        caps = prov.adapter.adapter_capabilities()
        return ActionResult(success=True, data={
            "name": prov.name,
            "adapter": prov.config.adapter,
            "status": prov.status,
            "capabilities": {
                "inbound": caps.supports_inbound,
                "outbound": caps.supports_outbound,
                "rich_text": caps.supports_rich_text,
                "attachments": caps.supports_attachments,
                "threading": caps.supports_threading,
                "max_message_length": caps.max_message_length,
            },
            "events_received": prov.events_received,
            "events_sent": prov.events_sent,
            "active_activations": len(prov.activation_tasks),
            "last_event_at": prov.last_event_at or None,
            "last_error": prov.last_error or None,
        })

    @action(
        description="Pause a provider's inbound listener.",
        params_model=PauseProviderParams,
        risk_level="medium",
        side_effects=["state_mutation"],
        aliases=["pause_canal"],
        tags=["channels", "admin"],
        cli_label="Pause provider",
        cli_param="provider_id",
    )
    async def pause_provider(self, params: PauseProviderParams) -> ActionResult:
        prov = self._providers.get(params.provider)
        if not prov:
            return ActionResult(success=False, error=f"Unknown provider: '{params.provider}'.")
        if prov.status == "paused":
            return ActionResult(success=True, data={"message": "Already paused."})

        if prov.listener_task and not prov.listener_task.done():
            prov.listener_task.cancel()
        await prov.adapter.stop_listener()
        prov.status = "paused"
        logger.info("channel_provider_paused name=%s", params.provider)
        return ActionResult(success=True, data={"message": f"Provider '{params.provider}' paused."})

    @action(
        description="Resume a paused provider's inbound listener.",
        params_model=ResumeProviderParams,
        risk_level="medium",
        side_effects=["state_mutation"],
        aliases=["reprendre_canal"],
        tags=["channels", "admin"],
        cli_label="Resume provider",
        cli_param="provider_id",
    )
    async def resume_provider(self, params: ResumeProviderParams) -> ActionResult:
        prov = self._providers.get(params.provider)
        if not prov:
            return ActionResult(success=False, error=f"Unknown provider: '{params.provider}'.")
        if prov.status == "active":
            return ActionResult(success=True, data={"message": "Already active."})

        if prov.adapter.SUPPORTS_INBOUND:
            prov.listener_task = asyncio.create_task(
                prov.adapter.start_listener(
                    lambda evt, _n=params.provider: self._on_inbound_event(_n, evt)
                ),
                name=f"channel:{params.provider}:listener",
            )
        prov.status = "active"
        logger.info("channel_provider_resumed name=%s", params.provider)
        return ActionResult(success=True, data={"message": f"Provider '{params.provider}' resumed."})

    @action(
        description="Get recent event history for channels.",
        params_model=ProviderHistoryParams,
        risk_level="low",
        aliases=["historique_canaux"],
        tags=["channels", "info"],
        cli_label="Provider history",
        cli_param="provider_id",
    )
    async def provider_history(self, params: ProviderHistoryParams) -> ActionResult:
        events = list(self._event_history)

        if params.provider:
            events = [e for e in events if e.provider == params.provider]
        if params.direction != "all":
            events = [e for e in events if e.direction == params.direction]

        events = events[-params.limit:]
        return ActionResult(success=True, data={
            "events": [
                {
                    "event_id": e.event_id,
                    "provider": e.provider,
                    "direction": e.direction,
                    "source": e.source,
                    "timestamp": e.timestamp,
                    "success": e.success,
                    "error": e.error or None,
                }
                for e in events
            ],
            "total": len(events),
        })

    @action(
        description="Get aggregate statistics for all channel providers.",
        params_model=StatsParams,
        risk_level="low",
        aliases=["stats_canaux"],
        tags=["channels", "info"],
        cli_label="Channel stats",
    )
    async def stats(self, params: StatsParams) -> ActionResult:
        total_in = sum(p.events_received for p in self._providers.values())
        total_out = sum(p.events_sent for p in self._providers.values())
        return ActionResult(success=True, data={
            "providers_count": len(self._providers),
            "active_count": sum(1 for p in self._providers.values() if p.status == "active"),
            "total_events_received": total_in,
            "total_events_sent": total_out,
            "active_sessions": self._session_mgr.active_session_count(),
            "history_size": len(self._event_history),
        })

    @action(
        description="Simulate an inbound event for testing purposes.",
        params_model=SimulateEventParams,
        risk_level="medium",
        side_effects=["state_mutation"],
        tags=["channels", "debug"],
        cli_label="Simulate event",
        cli_param="provider_id",
    )
    async def simulate_event(self, params: SimulateEventParams) -> ActionResult:
        prov = self._providers.get(params.provider)
        if not prov:
            return ActionResult(success=False, error=f"Unknown provider: '{params.provider}'.")

        from digitorn.modules.channels.adapter import make_event_id

        event = InboundEvent(
            event_id=make_event_id(),
            provider_id=params.provider,
            adapter_type=prov.config.adapter,
            source=params.source,
            message=params.message,
            payload=sanitize_payload(params.payload),
        )

        await self._on_inbound_event(params.provider, event)
        return ActionResult(success=True, data={
            "event_id": event.event_id,
            "message": f"Simulated event sent to provider '{params.provider}'.",
        })

    @action(
        description="Send a test message to verify outbound connectivity.",
        params_model=TestSendParams,
        risk_level="medium",
        side_effects=["network_io"],
        tags=["channels", "debug"],
        cli_label="Test send",
        cli_param="channel",
    )
    async def test_send(self, params: TestSendParams) -> ActionResult:
        return await self.send_message(SendMessageParams(
            provider=params.provider,
            text=params.text,
        ))

    # ── State ────────────────────────────────────────────────────

    def state_snapshot(self) -> dict[str, Any]:
        return {
            "app_id": self._app_id,
            "providers": {
                name: {
                    "status": prov.status,
                    "events_received": prov.events_received,
                    "events_sent": prov.events_sent,
                }
                for name, prov in self._providers.items()
            },
        }

    async def restore_state(self, state: dict[str, Any]) -> None:
        self._app_id = state.get("app_id", "default")
