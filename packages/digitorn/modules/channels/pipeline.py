"""Activation pipeline - the core event processing flow.

When an inbound event arrives from any adapter, the pipeline processes it
through these stages:

1. **Filter**   - Drop events that don't match YAML conditions.
2. **Prepare**  - Call tools via ServiceBus to enrich event context.
3. **Route**    - Pick the target agent based on enriched data.
4. **Build**    - Construct system prompt + user message for agent_turn.
5. **Activate** - Run agent_turn with the resolved context.
6. **Reply**    - If ``reply: auto``, send agent response back on channel.

Security: prepare steps use ServiceBus (audited, permissioned).
Templates use safe_render (no eval). Secrets filtered from outbound.
"""

from __future__ import annotations

import json
import logging
import time
from typing import TYPE_CHECKING, Any

from digitorn.modules.channels.adapter import InboundEvent
from digitorn.modules.channels.security import filter_secrets
from digitorn.modules.channels.template import render, resolve_dotpath

if TYPE_CHECKING:
    from digitorn.modules.channels.module import ActiveProvider, ChannelsModuleConfig, ChannelsModule
    from digitorn.modules.channels.session_manager import ChannelSessionManager

logger = logging.getLogger(__name__)


class ActivationPipeline:
    """Processes inbound events through the activation stages."""

    def __init__(
        self,
        module: ChannelsModule,
        session_mgr: ChannelSessionManager,
        config: ChannelsModuleConfig,
    ) -> None:
        self._module = module
        self._session_mgr = session_mgr
        self._config = config

    async def process_event(
        self,
        event: InboundEvent,
        provider: ActiveProvider,
    ) -> None:
        """Full activation pipeline for one inbound event."""
        activation = provider.config.activation
        t_start = time.monotonic()

        # Build variables dict accessible in templates
        variables: dict[str, Any] = {
            "event": {
                "event_id": event.event_id,
                "provider": event.provider_id,
                "adapter": event.adapter_type,
                "source": event.source,
                "message": event.message,
                "payload": event.payload,
                "data": event.payload,  # alias for convenience
                "metadata": event.metadata,
                "timestamp": event.timestamp,
            },
        }

        # ── 1. Filter ────────────────────────────────────────────
        if activation.filter:
            for cond in activation.filter:
                if not self._check_filter(cond, variables):
                    logger.debug(
                        "channel_event_filtered provider=%s event=%s field=%s",
                        event.provider_id, event.event_id, cond.field,
                    )
                    return

        # ── 2. Prepare ───────────────────────────────────────────
        for step in activation.prepare:
            result = await self._run_prepare_step(step, variables)
            if result is not None:
                variables[step.as_field] = result

        # ── 3. Route ─────────────────────────────────────────────
        agent_id = activation.agent or self._config.default_agent
        if activation.route:
            routed = self._resolve_route(activation.route, variables)
            if routed:
                agent_id = routed

        # ── 4. Build messages ────────────────────────────────────
        system_prompt, user_message = self._build_messages(
            event, activation, variables,
        )

        # ── 5. Resolve session ───────────────────────────────────
        session_id = self._session_mgr.resolve_session_id(
            event, activation.session, variables,
        )

        # ── 5b. Resolve the calling user's identity ──────────────
        # We need this so the runtime credential resolver can
        # substitute per-user secrets (per_user / per_app_per_user
        # scopes) inside the rendered templates. Extracted from the
        # event payload / metadata using known channel patterns
        # (telegram chat_id, slack user, webhook X-User-Id header,
        # email From). Falls back to "anonymous" when no user can
        # be inferred - in that case the resolver still hits
        # system_wide and per_app_shared scopes.
        user_id = _extract_user_id_from_event(event)

        # ── 6. Activate agent ────────────────────────────────────
        agent_response = await self._activate_agent(
            agent_id=agent_id,
            system_prompt=system_prompt,
            user_message=user_message,
            session_id=session_id,
            event=event,
            provider=provider,
            variables=variables,
            activation=activation,
            user_id=user_id,
        )

        # ── 7. Reply auto ───────────────────────────────────────
        if activation.reply == "auto" and agent_response and event.reply_context:
            text = agent_response
            if self._config.secret_filter_enabled:
                text = filter_secrets(text)
            try:
                result = await provider.adapter.send_reply(
                    event.reply_context, text,
                )
                if not result.success:
                    logger.warning(
                        "channel_reply_failed provider=%s error=%s",
                        event.provider_id, result.error,
                    )
            except Exception as exc:
                logger.error(
                    "channel_reply_error provider=%s error=%s",
                    event.provider_id, exc,
                )

        elapsed = time.monotonic() - t_start
        logger.info(
            "channel_activation_done provider=%s event=%s agent=%s "
            "session=%s elapsed=%.2fs reply=%s",
            event.provider_id, event.event_id, agent_id,
            session_id, elapsed, activation.reply,
        )

    # ── Filter ───────────────────────────────────────────────────

    def _check_filter(
        self,
        cond: Any,  # FilterCondition
        variables: dict[str, Any],
    ) -> bool:
        """Return True if event passes the filter condition."""
        value = resolve_dotpath(cond.field, variables)

        if cond.equals is not None:
            return str(value) == str(cond.equals)
        if cond.not_equals is not None:
            return str(value) != str(cond.not_equals)
        if cond.contains is not None:
            return cond.contains in str(value or "")
        if cond.gt is not None:
            try:
                return float(value) > cond.gt
            except (TypeError, ValueError):
                return False
        if cond.lt is not None:
            try:
                return float(value) < cond.lt
            except (TypeError, ValueError):
                return False

        # No condition specified - pass through
        return True

    # ── Prepare ──────────────────────────────────────────────────

    async def _run_prepare_step(
        self,
        step: Any,  # PrepareStep
        variables: dict[str, Any],
    ) -> Any:
        """Execute a prepare step via ServiceBus."""
        ctx = getattr(self._module, "_ctx", None)
        if not ctx or not ctx.service_bus:
            logger.warning("channel_prepare_no_service_bus action=%s", step.action)
            return None

        # Render template params
        rendered_params: dict[str, Any] = {}
        for key, val in step.params.items():
            if isinstance(val, str):
                rendered_params[key] = render(val, variables)
            else:
                rendered_params[key] = val

        # Parse module.action format
        parts = step.action.split(".", 1)
        if len(parts) != 2:
            logger.warning("channel_prepare_invalid_action action=%s", step.action)
            return None

        service_name, method_name = parts
        try:
            result = await ctx.service_bus.call(
                service_name, method_name, rendered_params,
            )
            # Extract data from ActionResult if applicable
            if hasattr(result, "data"):
                return result.data
            if hasattr(result, "to_dict"):
                return result.to_dict()
            return result
        except Exception as exc:
            logger.warning(
                "channel_prepare_failed action=%s error=%s",
                step.action, exc,
            )
            return None

    # ── Route ────────────────────────────────────────────────────

    def _resolve_route(
        self,
        route: Any,  # RouteConfig
        variables: dict[str, Any],
    ) -> str:
        """Resolve dynamic route to agent ID."""
        value = resolve_dotpath(route.field, variables)
        for rule in route.rules:
            if rule.default:
                return rule.agent
            if rule.match is not None and str(value) == str(rule.match):
                return rule.agent
        return ""

    # ── Build messages ───────────────────────────────────────────

    def _build_messages(
        self,
        event: InboundEvent,
        activation: Any,  # ActivationConfig
        variables: dict[str, Any],
    ) -> tuple[str, str]:
        """Build system prompt addition and user message."""
        # Extra context for system prompt
        context_extra = ""
        if activation.context:
            context_extra = render(activation.context, variables)

        # User message
        if activation.message:
            user_msg = render(activation.message, variables)
        elif event.message:
            user_msg = event.message
        elif event.payload:
            user_msg = (
                f"Inbound event from '{event.provider_id}' ({event.adapter_type}).\n\n"
                f"```json\n{json.dumps(event.payload, indent=2, default=str, ensure_ascii=False)}\n```"
            )
        else:
            user_msg = f"Scheduled activation: {event.provider_id}"

        return context_extra, user_msg

    # ── Activate ─────────────────────────────────────────────────

    async def _activate_agent(
        self,
        agent_id: str,
        system_prompt: str,
        user_message: str,
        session_id: str,
        event: InboundEvent,
        provider: ActiveProvider,
        variables: dict[str, Any],
        activation: Any,
        user_id: str = "anonymous",
    ) -> str | None:
        """Run agent_turn with the resolved context. Returns agent response text."""
        app = self._module._runtime_app
        if not app:
            logger.error("channel_no_runtime_app - cannot activate agent")
            return None

        # Resolve agent context
        if agent_id and agent_id in app.contexts:
            ctx = app.contexts[agent_id]
        else:
            ctx = app.entry_context

        # Build full system prompt
        full_system = ctx.system_prompt
        if system_prompt:
            full_system += f"\n\n---\n{system_prompt}"

        # Handle session mode
        if activation.session == "shared" or (
            activation.session and activation.session not in ("per_event", "")
        ):
            # Shared session: append to existing history
            lock = self._session_mgr.get_lock(session_id)
            async with lock:
                history = self._session_mgr.get_history(session_id)
                if not history:
                    history.append({"role": "system", "content": full_system})
                history.append({"role": "user", "content": user_message})
                messages = list(history)
        else:
            # Per-event: fresh messages
            messages = [
                {"role": "system", "content": full_system},
                {"role": "user", "content": user_message},
            ]

        # ── Dashboard telemetry: create an Activation row + recorder ──
        # The channels module is a separate entry point from
        # background.run_background, so it needs its own wiring to the
        # activation_events table. Without this, channel-triggered
        # agent runs are invisible in the Flutter background dashboard
        # (no row in /activations, no events in the timeline drawer,
        # no artifacts), which is exactly the gap the user hit during
        # the V2 audit.
        activation_id: str | None = None
        activation_store = None
        _recorder_token = None
        wrapped_on_tool_call = None
        wrapped_on_thinking = None
        _app_id = getattr(ctx, "app_id", "") or getattr(
            self._module, "_app_id", "",
        ) or "default"

        try:
            from digitorn.core.app.activation_store import ActivationStore
            from digitorn.core.database import get_session_factory
            from digitorn.core.runtime.modes.background import (
                _CURRENT_ACTIVATION_RECORDER,
                _ActivationEventRecorder,
            )

            activation_store = ActivationStore(get_session_factory())
            activation_id = await activation_store.create(
                app_id=_app_id,
                trigger_id=f"channel:{event.provider_id}",
                trigger_type="channel",
                message=user_message,
                trigger_payload={
                    "event_id": event.event_id,
                    "source": event.source,
                    "provider": event.provider_id,
                },
            )
        except Exception as exc:
            logger.debug(
                "channel_activation_store_create_failed provider=%s event=%s: %s",
                event.provider_id, event.event_id, exc,
            )
            activation_id = None

        if activation_id is not None and activation_store is not None:
            rec = _ActivationEventRecorder(activation_store, activation_id)
            wrapped_on_tool_call = rec.wrap_on_tool_call(None)
            wrapped_on_thinking = rec.wrap_on_thinking(None)
            # Bind the recorder via the task-local contextvar so the
            # channel registry's _deliver_with_retry can also push
            # channel_sent events into the same activation's timeline.
            _recorder_token = _CURRENT_ACTIVATION_RECORDER.set(rec)
            try:
                ctx.activation_recorder = rec  # type: ignore[attr-defined]
            except Exception:
                pass

        # ── Runtime per-user secret resolution ─────────────────────
        # The compiler left {{secret.X}} references for per_user /
        # per_app_per_user scopes as passthroughs (they need user
        # context to resolve). Now that we know who's behind this
        # channel event (via user_id extracted from the payload at
        # process_event step 5b), walk the messages list and
        # substitute those passthroughs via the CredentialStore.
        #
        # Falls back to a no-op when no credential_store is wired
        # in - preserves the existing behaviour for tests and
        # standalone scripts.
        try:
            credential_store = _get_credential_store_for_channel(self._module)
            if credential_store is not None:
                from digitorn.core.credentials import (
                    resolve_runtime_secrets_in_value,
                )
                _app_id_for_resolve = (
                    getattr(ctx, "app_id", "")
                    or getattr(self._module, "_app_id", "")
                    or "default"
                )
                messages = await resolve_runtime_secrets_in_value(
                    messages,
                    store=credential_store,
                    user_id=user_id or "anonymous",
                    app_id=_app_id_for_resolve,
                )
        except Exception as exc:
            logger.warning(
                "channel_runtime_secret_resolve_failed provider=%s "
                "user=%s: %s - message may contain unresolved placeholders",
                event.provider_id, user_id, exc,
            )

        # Run agent_turn - send only the final response (no intermediate streaming)
        from digitorn.core.runtime.agent_loop import agent_turn

        result = None
        _error_msg: str | None = None
        try:
            result = await agent_turn(
                ctx,
                messages,
                max_turns=self._config.max_turns,
                timeout=self._config.timeout,
                on_tool_call=wrapped_on_tool_call,
                on_thinking=wrapped_on_thinking,
                hook_runner=self._module._hook_runner,
            )
        except Exception as exc:
            _error_msg = str(exc)
            logger.exception(
                "channel_agent_turn_failed provider=%s event=%s",
                event.provider_id, event.event_id,
            )
        finally:
            # Always reset the contextvar so the next channel event on
            # the same task gets a fresh recorder.
            if _recorder_token is not None:
                try:
                    _CURRENT_ACTIVATION_RECORDER.reset(_recorder_token)
                except Exception:
                    pass
            try:
                ctx.activation_recorder = None  # type: ignore[attr-defined]
            except Exception:
                pass

        # Persist the activation outcome so the dashboard's status +
        # recent-runs list actually reflect the channel-triggered run.
        if activation_id is not None and activation_store is not None:
            try:
                await activation_store.complete(
                    activation_id,
                    response=(result.content if result else "") or "",
                    tool_calls_count=(result.tool_calls_count if result else 0),
                    turns_used=(result.turns_used if result else 0),
                    prompt_tokens=getattr(result, "prompt_tokens", 0) if result else 0,
                    completion_tokens=getattr(result, "completion_tokens", 0) if result else 0,
                    error=_error_msg or (getattr(result, "error", None) if result else None),
                )
            except Exception as exc:
                logger.debug(
                    "channel_activation_store_complete_failed id=%s: %s",
                    activation_id, exc,
                )

        # Re-raise the original error after we've persisted telemetry so
        # the channel module's own error handling (circuit breaker,
        # retry) still sees it.
        if _error_msg is not None:
            raise RuntimeError(_error_msg)

        # Extract only the final clean response
        response_text = result.content if result else None

        # Update shared session history with agent response
        if response_text and activation.session not in ("per_event", ""):
            lock = self._session_mgr.get_lock(session_id)
            async with lock:
                self._session_mgr.append_to_history(
                    session_id, "assistant", response_text,
                )
                # Persist to DB for restart resilience
                await self._session_mgr.persist_session(session_id)

        return response_text


# ────────────────────────────────────────────────────────────────────
# Helpers - runtime per-user secret resolution
# ────────────────────────────────────────────────────────────────────


def _extract_user_id_from_event(event: InboundEvent) -> str:
    """Best-effort extraction of a Digitorn user id from an inbound event.

    Different channel adapters carry user identity in different
    fields. We try a sequence of common patterns and fall back to
    ``"anonymous"`` if none match.

    Patterns checked in order:

    1. ``event.payload.user_id`` - explicit Digitorn user id
       (e.g. set by an upstream auth proxy)
    2. ``event.metadata.headers["X-User-Id"]`` - webhook callers
       authenticated by a proxy or API gateway
    3. ``event.payload.from`` - Telegram-style sender block
       (the chat_id used as a stable per-user anchor)
    4. ``event.payload.user`` - Slack message ``user`` field
    5. ``event.payload.author.id`` - Discord message author id
    6. ``event.source`` - fallback to the adapter-set source
       string (telegram chat_id, email From address, etc.)

    The returned value is used by the credentials runtime resolver
    to look up per_user / per_app_per_user scoped secrets. If it
    doesn't match a real Digitorn user_id, the resolver simply
    falls through to the system_wide / per_app_shared scopes -
    nothing breaks, the user just sees the literal ``{{secret.X}}``
    template if no shared credential exists either.
    """
    payload = event.payload or {}
    metadata = event.metadata or {}

    # 1. Explicit user_id field
    if isinstance(payload, dict):
        explicit = payload.get("user_id") or payload.get("digitorn_user_id")
        if explicit:
            return str(explicit)

    # 2. Webhook header (proxies and API gateways inject this)
    headers = (metadata.get("headers") if isinstance(metadata, dict) else None) or {}
    if isinstance(headers, dict):
        for key in ("X-User-Id", "x-user-id", "X-Digitorn-User-Id"):
            if headers.get(key):
                return str(headers[key])

    # 3. Telegram: payload.from.id (sender) - chat_id is per-user anchor
    if isinstance(payload, dict):
        sender = payload.get("from")
        if isinstance(sender, dict) and sender.get("id"):
            return f"tg-{sender['id']}"
        # Telegram chat_id at the top level
        if payload.get("chat_id"):
            return f"tg-{payload['chat_id']}"

    # 4. Slack: payload.user
    if isinstance(payload, dict) and payload.get("user"):
        user = payload["user"]
        if isinstance(user, str):
            return f"slack-{user}"
        if isinstance(user, dict) and user.get("id"):
            return f"slack-{user['id']}"

    # 5. Discord: payload.author.id
    if isinstance(payload, dict):
        author = payload.get("author")
        if isinstance(author, dict) and author.get("id"):
            return f"discord-{author['id']}"

    # 6. Last resort: the adapter-set source string
    if event.source:
        return f"{event.adapter_type}-{event.source}"

    return "anonymous"


def _get_credential_store_for_channel(module: Any) -> Any:
    """Best-effort lookup of the daemon's CredentialStore from the channels module.

    The store is attached to the FastAPI ``app.state`` at daemon
    startup. Channel modules don't get a direct handle to ``app.state``
    but they DO have a reference to the AppManager via
    ``module._runtime_app.manager`` - and the manager carries
    ``_credential_store``. Walks that chain and returns ``None`` if
    anything is missing (which makes the runtime resolver a no-op
    in tests and standalone scripts).
    """
    runtime_app = getattr(module, "_runtime_app", None)
    if runtime_app is None:
        return None
    manager = (
        getattr(runtime_app, "manager", None)
        or getattr(runtime_app, "_manager", None)
    )
    if manager is None:
        return None
    return getattr(manager, "_credential_store", None)
