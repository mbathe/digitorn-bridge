"""ContextBuilderModule - Tool Discovery Engine + Primitive Capabilities.

System module that manages tool discovery (5 meta-tools) and exposes
universal primitive capabilities for parallel execution, background task
management, persistent watchers, and scheduling.

Architecture: thin facade composing 3 action mixins:
    - MetaToolsMixin      - search/get/execute/list/browse + run_parallel
    - BackgroundActionsMixin - background_run/status/result/cancel/list/wait
    - WatcherActionsMixin - watch_start/stop/pause/resume/status/list/history

Scheduling lives in the dedicated cron_native module (schedule + cancel_schedule).
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, Field

from digitorn.modules.base import ActionResult, BaseModule, Platform
from digitorn.modules.context_builder.actions_background import (
    BackgroundActionsMixin,
    UniversalBackgroundTask,
)
from digitorn.modules.context_builder.actions_meta import MetaToolsMixin
from digitorn.modules.context_builder.actions_watchers import (
    Watcher,
    WatcherActionsMixin,
)
from digitorn.modules.context_builder.builder import build_index
from digitorn.modules.context_builder.types import ToolIndex
from digitorn.modules.context_builder.params import (  # noqa: F401 - backward compat
    AskUserParams,
    BackgroundListParams,
    BackgroundRunParams,
    BackgroundTaskIdParams,
    BackgroundWaitParams,
    BrowseCategoryParams,
    CallAppParams,
    ExecuteToolParams,
    GetToolParams,
    ListCategoriesParams,
    RunParallelParams,
    SearchToolsParams,
    SendNotificationParams,
    UseSkillParams,
    WatcherIdParams,
    WatchHistoryParams,
    WatchListParams,
    WatchStartParams,
)
from digitorn.modules.manifest import ModuleManifest

logger = logging.getLogger(__name__)


# ── Config model (compile-time validation via CONFIG_MODEL) ──────


class ContextBuilderConfig(BaseModel):
    """Pydantic config for the context_builder module (validated at compile time)."""

    model_config = {"extra": "forbid"}

    workspace: str = Field(default="", description="Auto-injected by the daemon.")


class ContextBuilderModule(
    MetaToolsMixin,
    BackgroundActionsMixin,
    WatcherActionsMixin,
    BaseModule,
):
    """Tool Discovery Engine + Primitive Capabilities + Persistent Watchers.

    Manages a pre-computed ToolIndex and exposes:
    - meta-tools for agent-driven tool discovery and execution
    - primitive capabilities for parallel execution and background tasks
    - watcher actions for persistent periodic monitoring
    """

    MODULE_ID = "context_builder"
    VERSION = "2.0.0"
    SUPPORTED_PLATFORMS = [Platform.ALL]
    MODULE_TYPE = "system"
    CONFIG_MODEL = ContextBuilderConfig

    def __init__(self) -> None:
        super().__init__()
        self._index: ToolIndex = ToolIndex()
        # Per-session background tasks (key = session_id)
        self._background_tasks: dict[str, dict[str, UniversalBackgroundTask]] = {}
        self._watchers: dict[str, Watcher] = {}
        # Per-session notification queues (key = session_id, "_standalone" for CLI)
        self._bg_notifications: dict[str, asyncio.Queue[dict[str, Any]]] = {}
        self._job_store: Any | None = None
        self._scheduler: Any | None = None
        self._app_id: str = "default"
        self._skills: list[dict[str, str]] = []
        self._session_id: str | None = None
        self._channel_registry: Any | None = None
        self._exec_context: Any | None = None
        self._usage_counts: dict[str, int] = {}
        # Optional relay: called on every push_module_notification so the
        # event bus can emit bg_task_update SSE events to the frontend.
        # Signature: (session_id: str, notification: dict) -> None
        self._on_notification_relay: Any | None = None

    # ── Prompt injection ─────────────────────────────────

    # Each mixin provides its own _prompt_sections_xxx() method.
    # This collector gathers them all, sorted by priority.
    _MIXIN_SECTION_METHODS = (
        "_prompt_sections_meta",
        "_prompt_sections_background",
        "_prompt_sections_watchers",
        "_prompt_sections_scheduler",
    )

    def get_prompt_sections(self) -> list[dict[str, Any]]:
        sections: list[dict[str, Any]] = []
        for method_name in self._MIXIN_SECTION_METHODS:
            method = getattr(self, method_name, None)
            if method is not None:
                try:
                    sections.extend(method())
                except Exception:
                    logger.debug(
                        "failed to collect prompt sections from %s",
                        method_name, exc_info=True,
                    )
        return sections

    # ── Index management ─────────────────────────────────

    def set_index(self, index: ToolIndex) -> None:
        self._index = index

    def build_and_set_index(
        self,
        modules: dict[str, Any],
        security_profile: Any | None = None,
        skip_embeddings: bool = False,
    ) -> ToolIndex:
        self._index = build_index(modules, security_profile, skip_embeddings=skip_embeddings)
        return self._index

    _mcp_refresh_cooldown: float = 60.0
    _last_mcp_refresh: float = 0.0

    async def _try_refresh_mcp_index(self) -> bool:
        import time as _time
        now = _time.monotonic()
        if now - self._last_mcp_refresh < self._mcp_refresh_cooldown:
            return False

        mcp_module = None
        for tool in self._index.tools.values():
            if "mcp" in tool.tags:
                mcp_module = tool.module
                break

        if mcp_module is None:
            return False

        old_count = self._index.total_tools
        # Track stale FQNs to detect adds/removes/renames (not just count changes)
        old_mcp_fqns = {fqn for fqn, t in self._index.tools.items() if "mcp" in t.tags}
        try:
            # Remove stale MCP tools before re-indexing
            for fqn in old_mcp_fqns:
                self._index.tools.pop(fqn, None)

            from digitorn.modules.context_builder.builder import _index_mcp_servers
            _index_mcp_servers(self._index, mcp_module, security_profile=None)
            self._last_mcp_refresh = now
            new_count = self._index.total_tools
            new_mcp_fqns = {fqn for fqn, t in self._index.tools.items() if "mcp" in t.tags}

            # CB21: rebuild semantic index whenever the MCP tool SET changes,
            # not just when the count grows. Add, remove, AND rename trigger
            # rebuild - otherwise the semantic index has ghost embeddings for
            # disconnected tools, returning phantom search results.
            tools_changed = old_mcp_fqns != new_mcp_fqns
            if tools_changed:
                added = len(new_mcp_fqns - old_mcp_fqns)
                removed = len(old_mcp_fqns - new_mcp_fqns)
                logger.info(
                    "context_builder: MCP index refreshed (added=%d removed=%d)",
                    added, removed,
                )
                # Audit#3 BUG#2: rebuild semantic index in background to avoid
                # blocking the agent loop. Embedding all tools can take 500ms+
                # for large MCP setups (5000+ tools).
                self._schedule_semantic_rebuild()
            return tools_changed
        except Exception as exc:
            logger.debug("context_builder: MCP index refresh failed: %s", exc)
            return False

    def _schedule_semantic_rebuild(self) -> None:
        """Schedule a semantic index rebuild in the background.

        Audit#3 BUG#2: avoids blocking the agent loop on large embedding
        operations. Uses a single-flight pattern - if a rebuild is already
        in progress, this is a no-op.
        """
        # Single-flight: skip if a rebuild is already pending or running
        existing = getattr(self, "_semantic_rebuild_task", None)
        if existing is not None and not existing.done():
            return

        async def _do_rebuild() -> None:
            try:
                # Run the CPU-bound embed work in a thread to not block the loop
                from digitorn.modules.context_builder.embeddings import SemanticIndex
                new_sem = await asyncio.to_thread(SemanticIndex.build, self._index)
                if new_sem is not None:
                    self._index.semantic_index = new_sem
                    logger.info("context_builder: semantic index rebuilt in background")
            except Exception as exc:
                logger.warning(
                    "context_builder: background semantic rebuild failed: %s", exc,
                )

        try:
            self._semantic_rebuild_task = asyncio.create_task(_do_rebuild())
        except RuntimeError:
            # No running event loop - fall back to sync rebuild
            try:
                from digitorn.modules.context_builder.embeddings import SemanticIndex
                new_sem = SemanticIndex.build(self._index)
                if new_sem is not None:
                    self._index.semantic_index = new_sem
            except Exception as exc:
                logger.warning("context_builder: sync semantic rebuild failed: %s", exc)

    # ── Job/watcher persistence ──────────────────────────

    def set_job_store(self, job_store: Any) -> None:
        self._job_store = job_store

    async def restore_watchers(self, app_id: str) -> int:
        if self._job_store is None:
            return 0

        from digitorn.core.app.job_store import PersistedWatcher

        persisted: list[PersistedWatcher] = self._job_store.list_watchers(app_id)
        restored = 0

        for pw in persisted:
            tool = self._index.tools.get(pw.tool_name)
            if tool is None or not hasattr(tool.module, "execute"):
                logger.warning(
                    "watcher_restore_skip id=%s tool=%s (tool missing or module invalid)",
                    pw.watcher_id, pw.tool_name,
                )
                self._job_store.delete_watcher(app_id, pw.watcher_id)
                continue

            watcher = Watcher(
                watcher_id=pw.watcher_id,
                tool_name=pw.tool_name,
                params=dict(pw.params),
                interval=pw.interval,
                label=pw.label,
                notify_when=pw.notify_when,
                notify_config=dict(pw.notify_config),
                max_checks=pw.max_checks,
                status=pw.status,
                check_count=pw.check_count,
                notify_count=pw.notify_count,
                created_at=(
                    datetime.fromtimestamp(
                        pw.created_at, tz=timezone.utc
                    ).isoformat()
                ),
            )
            self._watchers[pw.watcher_id] = watcher

            if pw.status == "running":
                watcher._asyncio_task = asyncio.create_task(
                    self._watcher_loop(watcher)
                )

            restored += 1
            logger.info(
                "watcher_restored id=%s tool=%s status=%s checks=%d",
                pw.watcher_id, pw.tool_name, pw.status, pw.check_count,
            )

        return restored

    @property
    def index(self) -> ToolIndex:
        return self._index

    # ── Notification queue ───────────────────────────────

    def _notification_session_id(self) -> str:
        """Resolve current session id for notification routing."""
        ctx = self._context_var.get()
        if ctx is not None and ctx.session_id:
            return ctx.session_id
        return self._session_id or "_standalone"

    def _get_notification_queue(self, session_id: str | None = None) -> asyncio.Queue[dict[str, Any]]:
        sid = session_id or self._notification_session_id()
        # Bounded queue (1000 max) to prevent unbounded memory growth if a session
        # never drains. push_module_notification handles QueueFull gracefully.
        return self._bg_notifications.setdefault(sid, asyncio.Queue(maxsize=1000))

    def drain_bg_notifications(self, session_id: str | None = None) -> list[dict[str, Any]]:
        sid = session_id or self._notification_session_id()
        queue = self._bg_notifications.get(sid)
        if queue is None:
            return []
        notifications: list[dict[str, Any]] = []
        while not queue.empty():
            try:
                notifications.append(queue.get_nowait())
            except asyncio.QueueEmpty:
                break
        return notifications

    def has_active_bg_tasks(self) -> bool:
        try:
            for session_tasks in list(self._background_tasks.values()):
                for t in list(session_tasks.values()):
                    task = getattr(t, "_asyncio_task", None)
                    if task is not None and not task.done():
                        return True
        except (RuntimeError, AttributeError):
            pass  # Dict changed during iteration - safe to skip

        # Check if ANY session has pending notifications
        if any(not q.empty() for q in self._bg_notifications.values()):
            return True

        if self._index is not None:
            seen: set[int] = set()
            for tool_entry in self._index.tools.values():
                mod = tool_entry.module
                mod_id = id(mod)
                if mod_id in seen:
                    continue
                seen.add(mod_id)
                downloads = getattr(mod, "_downloads", None)
                if downloads:
                    if any(d.status == "downloading" for d in downloads.values()):
                        return True
                tasks = getattr(mod, "_tasks", None)
                if tasks:
                    if any(t.is_running for t in tasks.values()):
                        return True

        if any(w.status in ("running", "paused") for w in self._watchers.values()):
            return True

        return False

    def push_module_notification(self, notification: dict[str, Any]) -> None:
        # Route to the correct session queue; session_id comes from the notification
        # (set by runner.py / background tasks) or falls back to current context
        sid = notification.get("session_id") or self._notification_session_id()
        queue = self._get_notification_queue(sid)
        try:
            queue.put_nowait(notification)
        except asyncio.QueueFull:
            logger.warning(
                "bg_notification_queue_full (module notification) tool=%s session=%s",
                notification.get("tool_name", "?"), sid,
            )
        # Relay to event bus for real-time frontend updates (SSE/Socket.IO)
        if self._on_notification_relay is not None:
            try:
                self._on_notification_relay(sid, notification)
            except Exception:
                pass  # Never block the notification pipeline

    def cleanup_session_queue(self, session_id: str) -> None:
        """Remove a session's notification queue."""
        self._bg_notifications.pop(session_id, None)

    # ── Background tasks (session-scoped) ─────────────────

    def _bg_session_id(self) -> str:
        """Resolve session id for background task scoping."""
        ctx = self._context_var.get()
        if ctx is not None and ctx.session_id:
            return ctx.session_id
        return self._session_id or "_standalone"

    def _session_bg_tasks(self, session_id: str | None = None) -> dict[str, UniversalBackgroundTask]:
        sid = session_id or self._bg_session_id()
        return self._background_tasks.setdefault(sid, {})

    async def cleanup_session_bg_tasks(self, session_id: str) -> None:
        """Cancel and remove all background tasks for a session."""
        tasks = self._background_tasks.pop(session_id, {})
        for task in tasks.values():
            if not task._asyncio_task.done():
                task._asyncio_task.cancel()

    # ── Watcher persistence helpers ──────────────────────

    def _persist_watcher(self, watcher: Watcher, app_id: str | None = None) -> None:
        if self._job_store is None:
            return
        from digitorn.core.app.job_store import PersistedWatcher

        aid = app_id or getattr(self, "_app_id", "default")

        pw = PersistedWatcher(
            watcher_id=watcher.watcher_id,
            app_id=aid,
            tool_name=watcher.tool_name,
            params=dict(watcher.params),
            interval=watcher.interval,
            label=watcher.label,
            notify_when=watcher.notify_when,
            notify_config=dict(watcher.notify_config),
            max_checks=watcher.max_checks,
            session_id=self._session_id,
            status=watcher.status,
            check_count=watcher.check_count,
            notify_count=watcher.notify_count,
            created_at=(
                datetime.fromisoformat(watcher.created_at).timestamp()
                if isinstance(watcher.created_at, str)
                else watcher.created_at
            ),
        )
        self._job_store.put_watcher(pw)

    def _unpersist_watcher(self, watcher_id: str, app_id: str | None = None) -> None:
        if self._job_store is None:
            return
        aid = app_id or getattr(self, "_app_id", "default")
        self._job_store.delete_watcher(aid, watcher_id)

    # ── Lifecycle ────────────────────────────────────────

    async def on_start(self) -> None:
        pass

    async def on_stop(self) -> None:
        for session_tasks in self._background_tasks.values():
            for task in list(session_tasks.values()):
                if not task._asyncio_task.done():
                    task._asyncio_task.cancel()
                    task.error = "Module stopped"
                    task.finished_at = datetime.now(timezone.utc).isoformat()
        self._background_tasks.clear()

        for watcher in list(self._watchers.values()):
            if watcher.status in ("running", "paused"):
                self._persist_watcher(watcher)
            watcher.status = "stopped"
            if watcher._asyncio_task and not watcher._asyncio_task.done():
                watcher._asyncio_task.cancel()
        self._watchers.clear()

        self._index = ToolIndex()

    def get_manifest(self) -> ModuleManifest:
        return ModuleManifest.from_module(self).model_copy(
            update={
                "description": (
                    "Tool Discovery Engine + Primitive Capabilities - agents "
                    "search, browse, and execute tools with O(1) lookups. "
                    "Parallel execution, background tasks, and watchers."
                ),
                "author": "Digitorn Team",
                "tags": ["core", "system", "discovery", "tools"],
            }
        )
