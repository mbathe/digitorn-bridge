"""Preview Module - universal live canvas for Digitorn apps.

Agents push state and events to a per-session preview stream that the
app's ``web/`` UI reads via Socket.IO (namespace ``/events``, room
``session:{session_id}``). This gives any app (digitorn-builder,
future workflow editors, multi-agent orchestrators, …) an n8n-style
live canvas **without writing a single line of frontend code** when
the app uses the default React SDK.

The module is stateless across the process in the sense that all
state lives inside a per-session ``PreviewSessionState``. Events are
published via the Socket.IO bus injected by the bootstrap.

Actions (all broadcast live to any connected browser for the session):

    preview.set_state(key, value)    update a scalar value in the state map
    preview.patch_state(patch)       merge a dict into the state map
    preview.get_state()              read the current state map
    preview.clear()                  reset everything

    preview.push_node(node)          add or replace a canvas node
    preview.update_node(id, updates) partial update of an existing node
    preview.highlight_node(id, status)  set status: idle|running|done|error
    preview.remove_node(id)

    preview.push_edge(edge)
    preview.remove_edge(id)

    preview.emit(event_type, data)   free-form event pushed to the stream

Each mutation publishes one ``preview:*`` event on the session room
(via ``SessionBus.emit`` - same envelope.seq + history_log persistence
as every other session event) and schedules a debounced flush of the
in-memory state to ``state.json`` under
``{workspace}/.digitorn/sessions/{sid}/`` (the single source of truth
for the next reconnect).
"""

from __future__ import annotations

import asyncio
import logging
import re
from contextvars import ContextVar
from typing import Any


# Per-asyncio-task active session pointer. Replaces the previous
# instance-level ``self._active_session_id`` which was a CRITICAL race
# condition: the preview module is ``isolation=shared``, so a single
# instance serves every session in the daemon. With a flat instance
# variable, two concurrent agent turns from different sessions would
# race on the same memory cell - one turn could ``set_active_session``,
# yield at the next ``await``, and a second turn would overwrite the
# pointer; when the first turn resumed, its tool calls would land in
# the WRONG session's resources, silently leaking files / nodes /
# slides across the user / session boundary.
#
# ``ContextVar`` solves this because every asyncio task carries its
# own copy of the active context - reads and writes are local to the
# task, and ``asyncio.create_task(...)`` snapshots the parent context
# at creation so spawned agent tasks inherit the right session
# without explicit propagation. Same pattern Flask / FastAPI use for
# the per-request state.
_ACTIVE_SESSION_VAR: ContextVar[str | None] = ContextVar(
    "preview_active_session", default=None,
)
_ACTIVE_USER_VAR: ContextVar[str | None] = ContextVar(
    "preview_active_user", default=None,
)

from pydantic import BaseModel, Field

from digitorn.modules.base import ActionResult, BaseModule
from digitorn.modules.decorators import action
from digitorn.modules.manifest import ModuleManifest
from digitorn.modules.preview.store import (
    PreviewSessionState,
    PreviewSessionStore,
)

logger = logging.getLogger(__name__)


# ── Config model (compile-time validation via CONFIG_MODEL) ──────


class PreviewModuleConfig(BaseModel):
    """Pydantic config for the preview module (validated at compile time).

    Named ``PreviewModuleConfig`` to avoid clashing with
    ``core.app.schema.PreviewConfig`` (top-level ``preview:`` block).
    """

    model_config = {"extra": "forbid"}

    workspace: str = Field(default="", description="Auto-injected by the daemon.")


# ─────────────────────────── params ────────────────────────────


class SetStateParams(BaseModel):
    """Set a single scalar in the session's preview state map."""
    key: str = Field(..., description="State key, e.g. 'current_state', 'yaml', 'progress'.")
    value: Any = Field(..., description="Arbitrary JSON-serialisable value.")


class PatchStateParams(BaseModel):
    """Merge a dict of key/value updates into the state map."""
    patch: dict[str, Any] = Field(..., description="Fields to merge.")


class EmitParams(BaseModel):
    """Push a free-form event to the preview stream."""
    event_type: str = Field(..., description="Event category, e.g. 'compile_attempt', 'user_answered'.")
    data: dict[str, Any] = Field(default_factory=dict, description="Event payload.")


class PushNodeParams(BaseModel):
    """Add or replace a canvas node (ReactFlow-shaped)."""
    id: str | None = Field(
        default=None,
        description="Unique node id. Auto-derived from label (slug) if omitted.",
    )
    type: str = Field(default="default", description="Node visual type (e.g. 'state', 'agent', 'tool').")
    label: str = Field(default="", description="Human-readable label for the node.")
    position: dict[str, float] = Field(
        default_factory=lambda: {"x": 0.0, "y": 0.0},
        description="{'x': px, 'y': px} coordinates.",
    )
    data: dict[str, Any] = Field(default_factory=dict, description="Arbitrary payload attached to the node.")
    status: str = Field(default="idle", description="idle | running | done | error")


class UpdateNodeParams(BaseModel):
    """Partial update to an existing node."""
    id: str = Field(..., description="Node id to update.")
    updates: dict[str, Any] = Field(..., description="Fields to merge into the node.")


class HighlightNodeParams(BaseModel):
    """Shortcut to set a node's ``status`` field and broadcast it."""
    id: str = Field(..., description="Node id.")
    status: str = Field(..., description="idle | running | done | error")


class RemoveNodeParams(BaseModel):
    id: str = Field(..., description="Node id to remove.")


class PushEdgeParams(BaseModel):
    """Add or replace a canvas edge (ReactFlow-shaped)."""
    id: str | None = Field(
        default=None,
        description="Unique edge id. Auto-derived from 'source->target' if omitted.",
    )
    source: str = Field(..., description="Source node id.")
    target: str = Field(..., description="Target node id.")
    label: str = Field(default="", description="Optional label displayed on the edge.")
    data: dict[str, Any] = Field(default_factory=dict)


class RemoveEdgeParams(BaseModel):
    id: str = Field(..., description="Edge id to remove.")


_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _slugify(text: str, fallback: str) -> str:
    s = _SLUG_RE.sub("-", (text or "").strip().lower()).strip("-")
    return s or fallback


class GetStateParams(BaseModel):
    """No params - returns the full snapshot."""


class ClearParams(BaseModel):
    """No params - wipes the session's preview state."""


class SetResourceParams(BaseModel):
    """Upsert a resource into a named channel."""
    channel: str = Field(..., description="Channel name (e.g. 'nodes', 'files', 'slides').")
    id: str = Field(..., description="Unique id within the channel.")
    payload: dict[str, Any] = Field(default_factory=dict, description="Arbitrary payload.")


class PatchResourceParams(BaseModel):
    """Merge fields into an existing resource. Creates it if absent."""
    channel: str = Field(...)
    id: str = Field(...)
    patch: dict[str, Any] = Field(...)


class DeleteResourceParams(BaseModel):
    channel: str = Field(...)
    id: str = Field(...)


class ListResourcesParams(BaseModel):
    channel: str = Field(...)


class ClearChannelParams(BaseModel):
    channel: str = Field(...)


class BulkSetResourcesParams(BaseModel):
    """Upsert many resources in one event (snapshot/import)."""
    channel: str = Field(...)
    items: dict[str, dict[str, Any]] = Field(..., description="Map of id → payload.")
    replace: bool = Field(default=False, description="If true, drop existing channel before insert.")


# ─────────────────────────── module ────────────────────────────


class PreviewModule(BaseModule):
    """Per-session live preview for Digitorn apps.

    All actions resolve the current session via the
    ``_ACTIVE_SESSION_VAR`` ContextVar set by ``set_active_session``.
    Each mutation publishes a ``preview:*`` event on the session
    room (envelope.seq stamped by SessionBus, persisted in
    history_log) and schedules a debounced flush to ``state.json``
    on disk - which is the single source of truth that's read back
    on reconnect.
    """

    MODULE_ID = "preview"
    VERSION = "1.0.0"
    CONFIG_MODEL = PreviewModuleConfig

    def get_manifest(self) -> ModuleManifest:
        return ModuleManifest.from_module(self).model_copy(update={
            "description": (
                "Universal live-preview module. Agents push canvas nodes, "
                "state, and events to a per-session Socket.IO stream consumed "
                "by the app's web UI (ReactFlow canvas, timeline, YAML panel "
                "- zero-code for the developer)."
            ),
            "author": "Digitorn Team",
        })

    def __init__(self) -> None:
        super().__init__()
        self._store = PreviewSessionStore()
        # Socket.IO bridge - set by bootstrap to emit events on the bus.
        self._event_bus: Any | None = None
        self._bus_app_id: str | None = None
        # Debounced flush: a burst of agent writes within
        # ``_persist_debounce_s`` collapses into one ``state.json``
        # write. Pending timers are cancelled on cleanup + force-
        # flushed on abort / shutdown.
        self._persist_debounce_s: float = 0.5
        self._pending_flushes: dict[str, asyncio.Task] = {}
        # Per-session workspace dir. ``WorkspaceModule._resolve_sync_dir``
        # guarantees a path for every session of an app whose
        # ``workspace_mode != "none"``. Sessions without an entry have
        # no preview persistence (transient state only - tests, CLI).
        self._session_workspaces: dict[str, str] = {}

    # ── session wiring ────────────────────────────────────────

    def set_active_session(
        self, session_id: str | None, user_id: str | None = None,
    ) -> None:
        """Set the active session id (and owning user) for the CURRENT
        asyncio task.

        Stored in module-level ContextVars so concurrent agent turns
        from different sessions never see each other's pointer.
        Tasks spawned with ``asyncio.create_task`` inherit a snapshot
        of the caller's context, so the active session correctly
        propagates from the request handler down into the agent loop
        and its tool dispatch.
        """
        _ACTIVE_SESSION_VAR.set(session_id)
        _ACTIVE_USER_VAR.set(user_id)
        if session_id and user_id:
            state = self._store.get_or_create(session_id)
            if state.user_id is None:
                state.user_id = user_id

    async def activate_session(
        self, session_id: str, user_id: str | None = None,
        workspace: str | None = None,
        set_active: bool = True,
    ) -> PreviewSessionState:
        """Bring a session online and reconcile in-memory state with
        ``state.json`` on disk.

        ``state.json`` is the single source of truth. We re-read it
        on every activation and overlay any keys / channels it carries
        onto the cached state (disk wins when both have the same key
        - disk is the persisted version of an earlier flush, in-memory
        is a write that may not have flushed yet, but in steady state
        they converge).

        Without this overlay, a reconnect that lands after the
        in-memory cache for the session was somehow re-created empty
        (different module instance, late binding race, ...) would
        emit an empty snapshot to the client even though disk has
        the persisted state. The user-visible bug was sessions
        appearing blank after switching tabs / browsers.

        ``set_active=True`` also flips the per-task ContextVar so
        downstream tool calls in this task resolve to this session.
        """
        if set_active:
            _ACTIVE_SESSION_VAR.set(session_id)
            _ACTIVE_USER_VAR.set(user_id)
        if workspace:
            self._session_workspaces[session_id] = workspace
        state = self._store.get_or_create(session_id)
        await self._hydrate_from_disk(state)
        if user_id and not state.user_id:
            state.user_id = user_id
        return state

    async def _hydrate_from_disk(self, state: PreviewSessionState) -> None:
        """Overlay the on-disk ``state.json`` onto ``state``.

        Idempotent and non-destructive: keys present in memory but
        absent on disk survive. Per-channel resource dicts are merged
        key-by-key.

        For each resource, the ``updated_at`` timestamp arbitrates: if
        the in-memory copy is newer (an in-flight write that has not
        yet flushed), keep it; otherwise the disk version wins. Without
        this guard, a reconnect during an active agent turn would
        clobber the in-memory state with a stale on-disk snapshot, and
        the agent's most recent write would silently disappear.
        """
        ws = self._session_workspaces.get(state.session_id) or ""
        if not ws:
            return
        try:
            from digitorn.modules.preview.fs_backend import read_snapshot
            data = await asyncio.to_thread(
                read_snapshot, ws, state.session_id,
            )
        except Exception as exc:
            logger.warning(
                "preview_state_read_failed sid=%s: %s",
                state.session_id, exc,
            )
            return
        if not data:
            return
        # Overlay scalar state. Disk wins (state map is small + cheap
        # to flush, the in-flight-vs-disk window is tiny).
        for k, v in (data.get("state") or {}).items():
            state.state[k] = v
        # Overlay resources channel by channel, item by item, but only
        # if disk's payload is at least as fresh as memory's.
        for ch_name, items in (data.get("resources") or {}).items():
            if not isinstance(items, dict):
                continue
            ch = state.resources.setdefault(ch_name, {})
            for rid, payload in items.items():
                if not isinstance(payload, dict):
                    ch[rid] = payload
                    continue
                existing = ch.get(rid)
                if isinstance(existing, dict):
                    mem_ts = existing.get("updated_at") or 0
                    disk_ts = payload.get("updated_at") or 0
                    if mem_ts > disk_ts:
                        # In-memory is newer (in-flight write) - keep it.
                        continue
                ch[rid] = dict(payload)
        if data.get("user_id") and not state.user_id:
            state.user_id = data["user_id"]

    async def hydrate_session(
        self, session_id: str, user_id: str | None = None,
        workspace: str | None = None,
    ) -> PreviewSessionState:
        return await self.activate_session(
            session_id, user_id=user_id, workspace=workspace,
            set_active=False,
        )

    def bind_session_workspace(self, session_id: str, workspace: str) -> None:
        """Tell the module a session is filesystem-backed. Idempotent."""
        if session_id and workspace:
            self._session_workspaces[session_id] = workspace

    def _resolve_session_id(self) -> str:
        sid = _ACTIVE_SESSION_VAR.get()
        if sid:
            return sid
        # Fallback: a synthetic "default" session so dev/tests without
        # a session still work. Never used in production - the agent
        # loop always sets an active session.
        return "_default_"

    def _session(self) -> PreviewSessionState:
        return self._store.get_or_create(self._resolve_session_id())

    async def on_stop(self) -> None:
        """Force-flush every active session's ``state.json`` so a daemon
        kill does not lose the last debounce window's mutations."""
        try:
            await self.flush_all()
        except Exception as exc:
            logger.warning("preview_on_stop_flush_failed: %s", exc)

    async def cleanup_session(self, session_id: str) -> None:
        """Flush + drop in-memory state for a session.

        Flush runs first so ``state.json`` reflects the final in-memory
        state - what a subsequent reopen expects to read.
        """
        await self._flush_now(session_id)
        task = self._pending_flushes.pop(session_id, None)
        if task is not None and not task.done():
            task.cancel()
        self._store.drop(session_id)

    async def flush_all(self) -> None:
        """Force-flush every active session to disk."""
        for sid in list(self._store.session_ids()):
            try:
                await self._flush_now(sid)
            except Exception as exc:
                logger.warning("preview_flush_shutdown_failed sid=%s: %s", sid, exc)

    # ── Durable snapshot plumbing ─────────────────────────────

    def _schedule_persist(self, session_id: str) -> None:
        """Leading-edge debounced flush. Caps staleness at one debounce window."""
        if not session_id:
            return
        existing = self._pending_flushes.get(session_id)
        if existing is not None and not existing.done():
            return
        try:
            loop = asyncio.get_event_loop()
        except RuntimeError:
            return
        task = loop.create_task(self._debounced_flush(session_id))
        self._pending_flushes[session_id] = task

    async def _debounced_flush(self, session_id: str) -> None:
        try:
            await asyncio.sleep(self._persist_debounce_s)
            await self._flush_now(session_id)
        except asyncio.CancelledError:
            return
        except Exception as exc:
            logger.warning("preview_debounced_flush_failed sid=%s: %s", session_id, exc)
        finally:
            self._pending_flushes.pop(session_id, None)

    async def _flush_now(self, session_id: str) -> None:
        """Write the in-memory snapshot to ``state.json`` on disk.

        ``state.json`` lives under
        ``{workspace}/.digitorn/sessions/{sid}/`` - that JSON file is
        the single source of truth for preview state. Sessions
        without a workspace dir (apps with ``workspace_mode: none``,
        or tests with no preview persistence at all) are silently
        skipped: their state lives in memory for the duration of
        the process and dies at restart.
        """
        state = self._store.get(session_id)
        if state is None:
            return
        ws = self._session_workspaces.get(session_id) or ""
        if not ws:
            return

        # Materialise the snapshot synchronously BEFORE handing off to
        # the thread pool. ``state.resources`` is a live dict that any
        # concurrent ``set_resource`` mutates in place; iterating it
        # inside the worker thread (or even just letting Python
        # advance through nested dicts while the loop yields) risks
        # ``RuntimeError: dictionary changed size during iteration``
        # or a torn snapshot. Building a fully deep-copied tree here
        # under the same task that owns the event loop guarantees a
        # consistent point-in-time view.
        snapshot_state = dict(state.state)
        snapshot_resources = {
            ch: {
                rid: (dict(payload) if isinstance(payload, dict) else payload)
                for rid, payload in list(items.items())
            }
            for ch, items in list(state.resources.items())
        }
        app_id = self._bus_app_id or ""
        user_id = state.user_id or ""

        try:
            from datetime import datetime, timezone
            from digitorn.modules.preview.fs_backend import write_snapshot
            await asyncio.to_thread(
                write_snapshot, ws, session_id,
                app_id=app_id, user_id=user_id,
                state=snapshot_state, resources=snapshot_resources,
                saved_at=datetime.now(timezone.utc).isoformat(),
            )
        except Exception as exc:
            logger.warning(
                "preview_fs_flush_failed sid=%s: %s",
                session_id, exc,
            )

    def snapshot_for(
        self, session_id: str, user_id: str | None = None,
    ) -> dict[str, Any]:
        """Return the replay snapshot for a session (may be empty)."""
        state = self._store.get(session_id)
        if state is None:
            state = self._store.get_or_create(session_id)
        return state.snapshot()

    # ── internal publish helper ───────────────────────────────

    async def _publish(
        self,
        session_state: PreviewSessionState,
        event_type: str,
        data: dict[str, Any],
    ) -> None:
        """Broadcast a state change to the session room and schedule
        a disk flush.

        The wire envelope picks up its ordering ``seq`` from
        ``SessionBus.emit`` (single source of truth, also persisted in
        ``history_log``). No per-event ``preview_seq`` field is added
        to the payload - clients dedup on ``envelope.event_id`` and
        order by ``envelope.seq`` like every other session event.
        """
        bus = self._event_bus
        if bus is None:
            logger.warning(
                "preview_event_dropped: _event_bus is None "
                "(type=%s session=%s)",
                event_type, session_state.session_id,
            )
        else:
            user_id = session_state.user_id or _ACTIVE_USER_VAR.get() or "local"
            app_id = self._bus_app_id or ""
            sid = session_state.session_id
            key = bus.session_key(app_id, sid, user_id)
            try:
                await bus.publish(key, {
                    "type": f"preview:{event_type}",
                    "data": data,
                })
            except Exception as exc:
                logger.warning(
                    "preview_event_emit_failed: type=%s error=%s",
                    event_type, exc,
                )

        # Schedule a debounced disk flush. A burst of N mutations in
        # ``_persist_debounce_s`` collapses into one ``state.json``
        # write.
        self._schedule_persist(session_state.session_id)

    # ── actions ────────────────────────────────────────────────

    @action(
        description="Set a single key in the session's live preview state map.",
        params_model=SetStateParams,
        risk_level="low",
        tags=["preview", "ui"],
        internal=True,
    )
    async def set_state(self, params: SetStateParams) -> ActionResult:
        sess = self._session()
        sess.state[params.key] = params.value
        await self._publish(sess, "state_changed", {"key": params.key, "value": params.value})
        return ActionResult(success=True, data={"key": params.key, "value": params.value})

    @action(
        description="Merge a dict of fields into the session's live preview state.",
        params_model=PatchStateParams,
        risk_level="low",
        tags=["preview", "ui"],
        internal=True,
    )
    async def patch_state(self, params: PatchStateParams) -> ActionResult:
        sess = self._session()
        sess.state.update(params.patch)
        await self._publish(sess, "state_patched", {"patch": params.patch})
        return ActionResult(success=True, data={"state": dict(sess.state)})

    @action(
        description="Read the current preview state + canvas snapshot for the session.",
        params_model=GetStateParams,
        risk_level="low",
        tags=["preview", "ui"],
        internal=True,
    )
    async def get_state(self, params: GetStateParams) -> ActionResult:
        sess = self._session()
        return ActionResult(success=True, data=sess.snapshot())

    @action(
        description="Clear all preview state, nodes, edges, and events for the session.",
        params_model=ClearParams,
        risk_level="low",
        tags=["preview", "ui"],
        internal=True,
    )
    async def clear(self, params: ClearParams) -> ActionResult:
        sess = self._session()
        sess.clear()
        await self._publish(sess, "cleared", {})
        return ActionResult(success=True, data={"cleared": True})

    @action(
        description="Push a free-form event to the live preview stream.",
        params_model=EmitParams,
        risk_level="low",
        tags=["preview", "ui"],
        internal=True,
    )
    async def emit(self, params: EmitParams) -> ActionResult:
        sess = self._session()
        await self._publish(sess, params.event_type, params.data)
        return ActionResult(success=True, data={
            "event_type": params.event_type,
            "data": params.data,
        })

    @action(
        description="Upsert a resource into a named channel. Generic primitive that any app shell can plug into.",
        params_model=SetResourceParams,
        risk_level="low",
        tags=["preview", "resource"],
        internal=True,
    )
    async def set_resource(self, params: SetResourceParams) -> ActionResult:
        sess = self._session()
        ch = sess.channel(params.channel)
        new_id = params.id not in ch
        ch[params.id] = dict(params.payload)
        if new_id:
            sess._maybe_warn_resource_size(params.channel)
        await self._publish(sess, "resource_set", {
            "channel": params.channel,
            "id": params.id,
            "payload": ch[params.id],
        })
        return ActionResult(success=True, data={"channel": params.channel, "id": params.id})

    @action(
        description="Merge fields into an existing resource (creates it if absent).",
        params_model=PatchResourceParams,
        risk_level="low",
        tags=["preview", "resource"],
        internal=True,
    )
    async def patch_resource(self, params: PatchResourceParams) -> ActionResult:
        sess = self._session()
        ch = sess.channel(params.channel)
        existing = ch.get(params.id) or {}
        existing.update(params.patch)
        ch[params.id] = existing
        await self._publish(sess, "resource_patched", {
            "channel": params.channel,
            "id": params.id,
            "patch": params.patch,
            "payload": existing,
        })
        return ActionResult(success=True, data={"channel": params.channel, "id": params.id})

    @action(
        description="Delete a resource from a channel.",
        params_model=DeleteResourceParams,
        risk_level="low",
        tags=["preview", "resource"],
        internal=True,
    )
    async def delete_resource(self, params: DeleteResourceParams) -> ActionResult:
        sess = self._session()
        ch = sess.channel(params.channel)
        existed = params.id in ch
        if existed:
            del ch[params.id]
            await self._publish(sess, "resource_deleted", {
                "channel": params.channel,
                "id": params.id,
            })
        return ActionResult(success=True, data={"existed": existed})

    @action(
        description="List every resource id+payload in a channel.",
        params_model=ListResourcesParams,
        risk_level="low",
        tags=["preview", "resource"],
        internal=True,
    )
    async def list_resources(self, params: ListResourcesParams) -> ActionResult:
        sess = self._session()
        ch = sess.channel(params.channel)
        return ActionResult(success=True, data={
            "channel": params.channel,
            "items": dict(ch),
            "count": len(ch),
        })

    @action(
        description="Clear every resource in a channel.",
        params_model=ClearChannelParams,
        risk_level="low",
        tags=["preview", "resource"],
        internal=True,
    )
    async def clear_channel(self, params: ClearChannelParams) -> ActionResult:
        sess = self._session()
        if params.channel in sess.resources:
            sess.resources[params.channel] = {}
            await self._publish(sess, "channel_cleared", {"channel": params.channel})
        return ActionResult(success=True, data={"channel": params.channel})

    @action(
        description="Upsert many resources in one shot (snapshot/import).",
        params_model=BulkSetResourcesParams,
        risk_level="low",
        tags=["preview", "resource"],
        internal=True,
    )
    async def bulk_set_resources(self, params: BulkSetResourcesParams) -> ActionResult:
        sess = self._session()
        # Build the new channel snapshot OFF the live dict, then swap
        # it in atomically. The previous "clear() then loop-insert"
        # pattern left a window where a concurrent ``set_resource``
        # could land between the clear and the next insert and get
        # silently dropped.
        items = {rid: dict(p) for rid, p in params.items.items()}
        if params.replace:
            sess.resources[params.channel] = items
        else:
            ch = sess.channel(params.channel)
            ch.update(items)
        sess._maybe_warn_resource_size(params.channel)
        await self._publish(sess, "resource_bulk_set", {
            "channel": params.channel,
            "items": {rid: dict(p) for rid, p in params.items.items()},
            "replace": params.replace,
        })
        return ActionResult(success=True, data={"channel": params.channel, "count": len(params.items)})

    @action(
        description="Add or replace a canvas node (wrapper over set_resource('nodes', ...)).",
        params_model=PushNodeParams,
        risk_level="low",
        tags=["preview", "canvas"],
        internal=True,
    )
    async def push_node(self, params: PushNodeParams) -> ActionResult:
        sess = self._session()
        node_id = params.id or _slugify(
            params.label, fallback=f"node-{len(sess.channel('nodes')) + 1}",
        )
        import time as _t
        payload = {
            "id": node_id,
            "type": params.type,
            "label": params.label,
            "position": params.position,
            "data": params.data,
            "status": params.status,
            "updated_at": _t.time(),
        }
        sess.channel("nodes")[node_id] = payload
        await self._publish(sess, "resource_set", {
            "channel": "nodes", "id": node_id, "payload": payload,
        })
        return ActionResult(success=True, data=payload)

    @action(
        description="Partially update an existing canvas node.",
        params_model=UpdateNodeParams,
        risk_level="low",
        tags=["preview", "canvas"],
        internal=True,
    )
    async def update_node(self, params: UpdateNodeParams) -> ActionResult:
        sess = self._session()
        ch = sess.channel("nodes")
        node = ch.get(params.id)
        if node is None:
            return ActionResult(success=False, error=f"node '{params.id}' not found")
        for k, v in params.updates.items():
            if k in ("type", "label", "position", "status", "data"):
                node[k] = v
            else:
                node.setdefault("data", {})[k] = v
        import time as _t
        node["updated_at"] = _t.time()
        await self._publish(sess, "resource_patched", {
            "channel": "nodes", "id": params.id,
            "patch": params.updates, "payload": node,
        })
        return ActionResult(success=True, data=node)

    @action(
        description="Highlight a node by setting its status (idle|running|done|error).",
        params_model=HighlightNodeParams,
        risk_level="low",
        tags=["preview", "canvas"],
        internal=True,
    )
    async def highlight_node(self, params: HighlightNodeParams) -> ActionResult:
        sess = self._session()
        ch = sess.channel("nodes")
        node = ch.get(params.id)
        if node is None:
            return ActionResult(success=False, error=f"node '{params.id}' not found")
        node["status"] = params.status
        import time as _t
        node["updated_at"] = _t.time()
        await self._publish(sess, "resource_patched", {
            "channel": "nodes", "id": params.id,
            "patch": {"status": params.status}, "payload": node,
        })
        return ActionResult(success=True, data={"id": params.id, "status": params.status})

    @action(
        description="Remove a canvas node by id (and any edges touching it).",
        params_model=RemoveNodeParams,
        risk_level="low",
        tags=["preview", "canvas"],
        internal=True,
    )
    async def remove_node(self, params: RemoveNodeParams) -> ActionResult:
        sess = self._session()
        nodes = sess.channel("nodes")
        edges = sess.channel("edges")
        if params.id in nodes:
            del nodes[params.id]
            await self._publish(sess, "resource_deleted", {"channel": "nodes", "id": params.id})
        to_drop = [
            eid for eid, e in edges.items()
            if e.get("source") == params.id or e.get("target") == params.id
        ]
        for eid in to_drop:
            del edges[eid]
            await self._publish(sess, "resource_deleted", {"channel": "edges", "id": eid})
        return ActionResult(success=True, data={"id": params.id, "edges_dropped": to_drop})

    @action(
        description="Add or replace a canvas edge between two nodes.",
        params_model=PushEdgeParams,
        risk_level="low",
        tags=["preview", "canvas"],
        internal=True,
    )
    async def push_edge(self, params: PushEdgeParams) -> ActionResult:
        sess = self._session()
        edge_id = params.id or f"{params.source}->{params.target}"
        payload = {
            "id": edge_id,
            "source": params.source,
            "target": params.target,
            "label": params.label,
            "data": params.data,
        }
        sess.channel("edges")[edge_id] = payload
        await self._publish(sess, "resource_set", {
            "channel": "edges", "id": edge_id, "payload": payload,
        })
        return ActionResult(success=True, data=payload)

    @action(
        description="Remove a canvas edge by id.",
        params_model=RemoveEdgeParams,
        risk_level="low",
        tags=["preview", "canvas"],
        internal=True,
    )
    async def remove_edge(self, params: RemoveEdgeParams) -> ActionResult:
        sess = self._session()
        ch = sess.channel("edges")
        if params.id in ch:
            del ch[params.id]
            await self._publish(sess, "resource_deleted", {"channel": "edges", "id": params.id})
        return ActionResult(success=True, data={"id": params.id})
