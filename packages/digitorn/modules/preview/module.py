"""Preview Module — universal live canvas for Digitorn apps.

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

Every mutation also appends a ``PreviewEvent`` with an incrementing
``seq`` so clients can reconcile after a reconnect.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

from pydantic import BaseModel, Field

from digitorn.modules.base import ActionResult, BaseModule
from digitorn.modules.decorators import action
from digitorn.modules.manifest import ModuleManifest
from digitorn.modules.preview.store import (
    PreviewEdge,
    PreviewEvent,
    PreviewNode,
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
    """No params — returns the full snapshot."""


class ClearParams(BaseModel):
    """No params — wipes the session's preview state."""


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

    All actions resolve the current session via
    :meth:`BaseModule._get_session_id` (same mechanism as memory).
    Every mutation publishes a ``PreviewEvent`` with an incrementing
    sequence number, stored in the session's event ring buffer for
    snapshot replay on (re)connect.
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
                "— zero-code for the developer)."
            ),
            "author": "Digitorn Team",
        })

    def __init__(self) -> None:
        super().__init__()
        self._store = PreviewSessionStore(loader=self._load_snapshot_from_db)
        self._active_session_id: str | None = None
        self._active_user_id: str | None = None
        # Socket.IO bridge — set by bootstrap to emit events on the bus
        self._event_bus: Any | None = None
        self._bus_app_id: str | None = None
        # ── Durable snapshot / debounced persistence ───────────────
        # Every mutation schedules a debounced flush to DB. A burst of
        # agent writes (e.g. 20 files in a turn) collapses into a single
        # row update. Pending timers are cancelled on cleanup + force-
        # flushed on abort / shutdown so nothing is ever lost.
        self._persist_debounce_s: float = 0.5
        self._pending_flushes: dict[str, asyncio.Task] = {}
        self._persistence_enabled: bool = True
        # Map session_id → workspace dir when the session is filesystem-backed.
        # Populated by ``activate_session`` whenever the session has a
        # user-chosen workspace. Empty entries default to DB persistence.
        self._session_workspaces: dict[str, str] = {}

    # ── session wiring ────────────────────────────────────────

    def set_active_session(
        self, session_id: str | None, user_id: str | None = None,
    ) -> None:
        """Set the active session id (and owning user) for the next action call.

        Called by the agent loop before dispatching tool calls. This is
        the SYNC entry point — callers that want DB hydration should
        use :meth:`activate_session` (async) once per session instead.
        """
        self._active_session_id = session_id
        self._active_user_id = user_id
        if session_id and user_id:
            state = self._store.get_or_create(session_id)
            if state.user_id is None:
                state.user_id = user_id

    async def activate_session(
        self, session_id: str, user_id: str | None = None,
        workspace: str | None = None,
        set_active: bool = True,
    ) -> PreviewSessionState:
        if set_active:
            self._active_session_id = session_id
            self._active_user_id = user_id
        if workspace:
            self._session_workspaces[session_id] = workspace
        state = await self._store.get_or_create_async(session_id)
        if user_id and not state.user_id:
            state.user_id = user_id
        return state

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
        sid = self._active_session_id
        if sid:
            return sid
        # Fallback: a synthetic "default" session so dev/tests without
        # a session still work. Never used in production — the agent
        # loop always sets an active session.
        return "_default_"

    def _session(self) -> PreviewSessionState:
        return self._store.get_or_create(self._resolve_session_id())

    async def on_stop(self) -> None:
        """Module shutdown hook — force-flush every active session to DB.

        Called during ``app_manager.undeploy`` (which the daemon invokes
        at shutdown for every deployed app). Without this, a daemon
        kill would lose the last debounce window worth of mutations.
        """
        try:
            await self.flush_all()
        except Exception as exc:
            logger.warning("preview_on_stop_flush_failed: %s", exc)

    async def cleanup_session(self, session_id: str) -> None:
        """Drop all preview state for a session AND flush to DB.

        We flush BEFORE dropping so the snapshot on disk reflects the
        final in-memory state — that's what a "reopen" expects to see.
        """
        await self._flush_now(session_id)
        # Cancel any pending debounced flush for this session.
        task = self._pending_flushes.pop(session_id, None)
        if task is not None and not task.done():
            task.cancel()
        self._store.drop(session_id)

    async def flush_all(self) -> None:
        """Force-flush every active session to DB.

        Called at daemon shutdown so no in-memory state is lost.
        """
        for sid in list(self._store.session_ids()):
            try:
                await self._flush_now(sid)
            except Exception as exc:
                logger.warning("preview_flush_shutdown_failed sid=%s: %s", sid, exc)

    # ── Durable snapshot plumbing ─────────────────────────────

    def _schedule_persist(self, session_id: str) -> None:
        """Leading-edge debounced flush. Caps staleness at one debounce window."""
        if not self._persistence_enabled or not session_id:
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
        """Write the in-memory snapshot to the active backend (disk or DB)."""
        state = self._store.get(session_id)
        if state is None:
            return

        snapshot_state = dict(state.state)
        snapshot_resources = {
            ch: {rid: dict(payload) for rid, payload in items.items()}
            for ch, items in state.resources.items()
        }
        app_id = self._bus_app_id or ""
        user_id = state.user_id or ""
        seq = state._seq

        ws = self._session_workspaces.get(session_id) or ""
        if ws:
            # Filesystem backend — store under {ws}/.digitorn/sessions/{sid}/
            try:
                from datetime import datetime, timezone
                from digitorn.modules.preview.fs_backend import write_snapshot
                await asyncio.to_thread(
                    write_snapshot, ws, session_id,
                    app_id=app_id, user_id=user_id,
                    state=snapshot_state, resources=snapshot_resources,
                    seq=seq,
                    saved_at=datetime.now(timezone.utc).isoformat(),
                )
            except Exception as exc:
                logger.warning("preview_fs_flush_failed sid=%s: %s", session_id, exc)
            return

        try:
            from digitorn.core.database import get_session_factory
            from digitorn.core.models import SessionWorkspaceSnapshot
            from sqlalchemy import select
        except Exception as exc:
            logger.debug("preview_persist_skipped: %s", exc)
            return

        try:
            sf = get_session_factory()
        except RuntimeError:
            return  # DB not initialised (standalone tests)

        async with sf() as session:
            async with session.begin():
                row = (
                    await session.execute(
                        select(SessionWorkspaceSnapshot).where(
                            SessionWorkspaceSnapshot.session_id == session_id,
                        )
                    )
                ).scalar_one_or_none()
                if row is None:
                    row = SessionWorkspaceSnapshot(
                        session_id=session_id,
                        app_id=app_id,
                        user_id=user_id,
                        state=snapshot_state,
                        resources=snapshot_resources,
                        preview_seq=seq,
                    )
                    session.add(row)
                else:
                    row.app_id = app_id or row.app_id
                    row.user_id = user_id or row.user_id
                    row.state = snapshot_state
                    row.resources = snapshot_resources
                    row.preview_seq = seq

    async def _load_snapshot_from_db(self, session_id: str) -> dict | None:
        """Read a persisted snapshot. Returns dict shape compatible with
        ``PreviewSessionState.restore_from_dict`` or None if missing.

        Tries the filesystem backend first (if the session is bound to a
        user-chosen workspace); falls back to the DB backend.
        """
        ws = self._session_workspaces.get(session_id) or ""
        if ws:
            try:
                from digitorn.modules.preview.fs_backend import read_snapshot
                data = await asyncio.to_thread(read_snapshot, ws, session_id)
                if data is not None:
                    return {
                        "session_id": session_id,
                        "user_id": data.get("user_id") or None,
                        "state": data.get("state") or {},
                        "resources": data.get("resources") or {},
                        "seq": int(data.get("seq") or 0),
                        "app_id": data.get("app_id"),
                    }
            except Exception as exc:
                logger.warning("preview_fs_load_failed sid=%s: %s", session_id, exc)
            # Fall through to DB only if filesystem yielded nothing.

        try:
            from digitorn.core.database import get_session_factory
            from digitorn.core.models import SessionWorkspaceSnapshot
            from sqlalchemy import select
        except Exception:
            return None

        try:
            sf = get_session_factory()
        except RuntimeError:
            return None

        async with sf() as session:
            row = (
                await session.execute(
                    select(SessionWorkspaceSnapshot).where(
                        SessionWorkspaceSnapshot.session_id == session_id,
                    )
                )
            ).scalar_one_or_none()
            if row is None:
                return None
            return {
                "session_id": row.session_id,
                "user_id": row.user_id or None,
                "state": row.state or {},
                "resources": row.resources or {},
                "seq": row.preview_seq,
                "app_id": row.app_id,
            }

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
    ) -> PreviewEvent:
        seq = session_state.next_seq()
        evt = PreviewEvent(seq=seq, event_type=event_type, data=data)
        session_state.events.append(evt)

        # ── Socket.IO: emit to session room via SocketIOBus ──
        bus = self._event_bus
        if bus is None:
            logger.warning(
                "preview_event_dropped: _event_bus is None "
                "(type=%s seq=%d session=%s)",
                event_type, seq, session_state.session_id,
            )
        else:
            user_id = session_state.user_id or self._active_user_id or "local"
            app_id = self._bus_app_id or ""
            sid = session_state.session_id
            key = bus.session_key(app_id, sid, user_id)
            try:
                await bus.publish(key, {
                    "type": f"preview:{event_type}",
                    "data": {**data, "preview_seq": seq},
                })
            except Exception as exc:
                logger.warning(
                    "preview_event_emit_failed: type=%s seq=%d error=%s",
                    event_type, seq, exc,
                )

        # Schedule a debounced DB persist. A burst of N mutations within
        # `_persist_debounce_s` collapses into one row update.
        self._schedule_persist(session_state.session_id)
        return evt

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
        evt = await self._publish(sess, params.event_type, params.data)
        return ActionResult(success=True, data=evt.to_dict())

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
        ch[params.id] = dict(params.payload)
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
        ch = sess.channel(params.channel)
        if params.replace:
            ch.clear()
        for rid, payload in params.items.items():
            ch[rid] = dict(payload)
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
