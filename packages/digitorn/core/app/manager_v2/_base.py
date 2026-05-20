"""_BaseMixin - owns `__init__` and shared state."""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from digitorn.core.app.compiler import AppYAMLCompiler
from digitorn.core.runtime.session_store.job_store import FileJobStore
from digitorn.core.app.channels import ChannelRegistry
from digitorn.core.app.channels.llm import LLMNotificationChannel
from digitorn.core.app.channels.gmail import GmailChannel
from digitorn.core.app.channels.log import LogChannel
from digitorn.core.app.channels.webhook import WebhookChannel
from digitorn.core.app.scheduler import SchedulerService
from digitorn.core.app.users import UserStore
from digitorn.core.runtime.session_store.bridge import get_default_bridge
from digitorn.core.runtime.session_store.legacy_adapter import (
    LegacySessionStoreAdapter,
)

from ._models import DeployedApp, TurnState

if TYPE_CHECKING:
    from digitorn.core.app.runtime import AppRuntimeStore
    from digitorn.modules.registry import ModuleRegistry
    from digitorn.modules.service_bus import ServiceBus

logger = logging.getLogger(__name__)


class _BaseMixin:
    """Shared state + `__init__` for the composed AppManager."""

    # Static attribute hints - keep static analysers (and humans) honest
    # about what each mixin can rely on. Initialised in `__init__`.
    _deployed: dict[str, DeployedApp]
    _turn_state: dict[str, TurnState]
    _active_sessions: set[str]
    _session_tasks: dict[str, asyncio.Task]
    _turn_heartbeat_tasks: dict[str, asyncio.Task]
    _bg_start_tasks: set[asyncio.Task]
    _deploy_errors: dict[str, dict[str, Any]]
    event_bus: Any

    def __init__(
        self,
        registry: ModuleRegistry,
        service_bus: ServiceBus | None = None,
        runtime_store: AppRuntimeStore | None = None,
        *,
        stop_on_error: bool = False,
        event_bus: Any | None = None,
    ) -> None:
        self._registry = registry
        self._service_bus = service_bus
        self._runtime_store = runtime_store
        self._stop_on_error = stop_on_error
        self._compiler = AppYAMLCompiler(registry)
        self._deployed: dict[str, DeployedApp] = {}
        self._deploy_lock = asyncio.Lock()
        self._deploy_errors: dict[str, dict[str, Any]] = {}
        self._bg_start_tasks: set[asyncio.Task] = set()
        bridge = get_default_bridge()
        if bridge is None:
            raise RuntimeError(
                "session_store_bridge_not_initialised -- call "
                "init_session_store() before constructing AppManager. "
                "The legacy KV-backed SessionStore has been removed; "
                "the daemon refuses to start without the new store."
            )
        logger.info(
            "session_store_using_adapter mode=%s -- "
            "legacy API served from new InMemorySessionStore",
            bridge.mode.value,
        )
        self._session_store = LegacySessionStoreAdapter(bridge.store)
        recovered = self._session_store.recover_orphans()
        if recovered:
            logger.info("recovered_orphan_sessions count=%d", recovered)
        self._job_store = FileJobStore(
            root=Path.home() / ".digitorn" / "jobs",
        )
        # Quota enforcement is owned by the digitorn LLM gateway. The
        # daemon does not maintain any quota state.
        self._channel_registry = ChannelRegistry()
        self._channel_registry.register_type(LLMNotificationChannel)
        self._channel_registry.register_type(WebhookChannel)
        self._channel_registry.register_type(LogChannel)
        self._channel_registry.register_type(GmailChannel)
        self._channel_registry.discover_plugins()
        self._llm_channel = LLMNotificationChannel(job_store=self._job_store)
        self._channel_registry.register_instance(
            "llm_notification", self._llm_channel,
        )
        self._scheduler = SchedulerService(self._job_store, self._channel_registry)
        if event_bus is None:
            from digitorn.core.events.event_buffer import EventBuffer
            from digitorn.core.events.session_bus import SocketIOBus
            event_bus = SocketIOBus(sio=None, buffer=EventBuffer())
        self.event_bus = event_bus
        self._notif_poller_task: asyncio.Task | None = None
        self._active_sessions: set[str] = set()  # "app_id:session_id" keys with turn in progress
        self._session_tasks: dict[str, asyncio.Task] = {}  # "app_id:session_id" → running agent_turn task

        self._turn_state: dict[str, TurnState] = {}
        self._turn_state_lock = asyncio.Lock()
        # Heartbeat task per active turn - cancelled on message_done.
        self._turn_heartbeat_tasks: dict[str, asyncio.Task] = {}
        self._user_store = UserStore()
        from digitorn.core.app.secrets import SecretStore
        self._secret_store = SecretStore()


    def has_active_bg_tasks(self, app_id: str) -> bool:
        """Check if a deployed app has any active background tasks."""
        deployed = self._deployed.get(app_id)
        if deployed is None:
            return False
        cb = deployed.context_builder
        if cb is None or not hasattr(cb, "has_active_bg_tasks"):
            return False
        return cb.has_active_bg_tasks()


    def _make_approval_publisher(self, app_id: str) -> Any:
        """Build an approval callback that republishes to the session bus."""
        async def _publish(request: Any) -> None:
            try:
                from digitorn.core.events.envelope import (
                    SessionEvent, OpType, OpState,
                )
                uid = request.user_id or "local"
                sid = getattr(request, "session_id", "") or ""
                payload = request.to_dict()
                op_id = getattr(request, "request_id", "") or "op-approval"
                payload["op_id"] = op_id
                if not sid:
                    logger.warning(
                        "approval_request_missing_session_id app=%s op=%s "
                        "- skipping bus emit",
                        app_id, op_id,
                    )
                else:
                    await self.event_bus.emit(SessionEvent.build(
                        type="approval_request",
                        app_id=app_id,
                        session_id=sid,
                        user_id=uid,
                        op_id=op_id,
                        op_type=OpType.APPROVAL,
                        op_state=OpState.WAITING_APPROVAL,
                        payload=payload,
                    ))
            except Exception as exc:
                logger.warning(
                    "approval_publish_failed app=%s: %s", app_id, exc,
                )
        return _publish

    def _approval_resolve_publisher(self, app_id: str):
        """Return a callback that publishes `approval_resolved` on SSE."""
        async def _publish_resolved(request: Any, approved: bool, reason: str) -> None:
            try:
                from digitorn.core.events.envelope import (
                    SessionEvent, OpType, OpState,
                )
                uid = request.user_id or "local"
                sid = getattr(request, "session_id", "") or ""
                payload = dict(request.to_dict())
                payload["approved"] = bool(approved)
                payload["reason"] = reason
                op_id = getattr(request, "request_id", "") or "op-approval"
                payload["op_id"] = op_id
                if reason == "pending_heartbeat":
                    op_state = OpState.WAITING_APPROVAL
                    ev_type = "approval_progress"
                elif approved:
                    op_state = OpState.COMPLETED
                    ev_type = "approval_resolved"
                else:
                    reason_l = (reason or "").lower()
                    if "time" in reason_l and "out" in reason_l:
                        op_state = OpState.TIMEOUT
                    else:
                        op_state = OpState.CANCELLED
                    ev_type = "approval_resolved"
                if not sid:
                    logger.warning(
                        "approval_resolved_missing_session_id app=%s op=%s "
                        "- skipping bus emit (no session to publish to)",
                        app_id, op_id,
                    )
                else:
                    await self.event_bus.emit(SessionEvent.build(
                        type=ev_type,
                        app_id=app_id,
                        session_id=sid,
                        user_id=uid,
                        op_id=op_id,
                        op_type=OpType.APPROVAL,
                        op_state=op_state,
                        payload=payload,
                    ))
            except Exception as exc:
                logger.warning(
                    "approval_resolved_publish_failed app=%s: %s", app_id, exc,
                )
        return _publish_resolved


    async def start_notification_poller(self, interval: float = 1.0) -> None:
        """Start the background-notification drain loop."""
        if getattr(self, "_notif_poller_task", None) is not None:
            return

        async def _loop() -> None:
            logger.info("notification_poller_started interval=%ss", interval)
            while True:
                try:
                    await asyncio.sleep(interval)
                    for deployed_key, deployed in list(self._deployed.items()):
                        app_id = deployed.app_id
                        cb = deployed.context_builder
                        if cb is None or not hasattr(cb, "_bg_notifications"):
                            continue
                        pending: list[tuple[str, str]] = []
                        for sid, q in list(cb._bg_notifications.items()):
                            if sid == "_standalone" or q.empty():
                                continue
                            uid = "local"
                            try:
                                inner = getattr(q, "_queue", None)
                                if inner:
                                    first = inner[0]
                                    if isinstance(first, dict):
                                        uid = first.get("user_id") or "local"
                            except Exception as exc:
                                logger.debug("_base best-effort block failed: %s", exc)
                            pending.append((sid, uid))
                        for sid, uid in pending:
                            try:
                                await self.check_notifications(
                                    app_id, sid, user_id=uid,
                                )
                            except Exception as exc:
                                logger.warning(
                                    "notification_poller_check_failed "
                                    "app=%s session=%s: %s",
                                    app_id, sid, exc,
                                )
                except asyncio.CancelledError:
                    break
                except Exception as exc:
                    logger.warning("notification_poller_tick_error: %s", exc)
            logger.info("notification_poller_stopped")

        self._notif_poller_task = asyncio.create_task(_loop())

    async def stop_notification_poller(self) -> None:
        task = getattr(self, "_notif_poller_task", None)
        if task is None:
            return
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass
        self._notif_poller_task = None


    async def list_secrets(self, app_id: str) -> list[str]:
        """List secret keys for an app."""
        return await self._secret_store.list_secrets(app_id)

    async def get_secret(self, app_id: str, key: str) -> str | None:
        """Retrieve a single secret value."""
        return await self._secret_store.get_secret(app_id, key)

    async def set_secret(self, app_id: str, key: str, value: str) -> None:
        """Store (or overwrite) a secret."""
        await self._secret_store.set_secret(app_id, key, value)

    async def delete_secret(self, app_id: str, key: str) -> bool:
        """Delete a secret. Returns True if it existed."""
        return await self._secret_store.delete_secret(app_id, key)


    @property
    def job_store(self) -> JobStore:
        """Public access to the job store (used by the API for buffer draining)."""
        return self._job_store
