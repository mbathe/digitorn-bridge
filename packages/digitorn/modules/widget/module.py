"""Widget Module - declarative UI rendered by the Flutter client.

The agent calls ``widget.render(zone, ref/tree, ctx)`` to push a
widget into the user's screen, ``widget.update(widget_id, patch)``
to mutate it live, and ``widget.close(widget_id)`` to take it down.
Each call publishes a Socket.IO event on the per-session widget channel
(namespace ``/events``, room ``session:{session_id}``).

Per-session isolation: every action mutates the state for whichever
``session_id`` the agent loop has activated on the module. Two users
opening two sessions each get two independent widget surfaces.

Widgets at startup come from the app's ``widgets:`` block compiled
into ``CompiledApp.widgets``. The agent uses the actions below to
push **inline** widgets (Z1) or trigger registered ones via ``ref:``.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any

from pydantic import BaseModel, Field

from digitorn.modules.base import ActionResult, BaseModule
from digitorn.modules.decorators import action
from digitorn.modules.manifest import ModuleManifest
from digitorn.modules.widget.expr import substitute_tree
from digitorn.modules.widget.store import (
    MountedWidget,
    WidgetEvent,
    WidgetSessionState,
    WidgetSessionStore,
)

logger = logging.getLogger(__name__)


# ── Config model (compile-time validation via CONFIG_MODEL) ──────


class WidgetModuleConfig(BaseModel):
    """Pydantic config for the widget module (validated at compile time)."""

    model_config = {"extra": "forbid"}

    workspace: str = Field(default="", description="Auto-injected by the daemon.")


_VALID_ZONES = {"inline", "chat_side", "workspace", "modal"}


# ─────────────────────────── params ────────────────────────────


class RenderParams(BaseModel):
    """Mount or replace a widget in one of the four zones."""
    zone: str = Field(..., description="inline | chat_side | workspace | modal")
    target: str | None = Field(
        default=None,
        description="Tab id (workspace) or modal name (modal). None for inline / chat_side.",
    )
    widget_id: str | None = Field(
        default=None,
        description="Stable id for later update/close. Auto-generated if not provided.",
    )
    ref: str | None = Field(
        default=None,
        description="Name of a pre-declared inline widget in the app's widgets.inline map.",
    )
    tree: dict[str, Any] | None = Field(
        default=None,
        description="Inline widget tree. Mutually exclusive with ref.",
    )
    ctx: dict[str, Any] = Field(
        default_factory=dict,
        description="Context bag exposed as ctx.* in the widget tree.",
    )
    turn_id: str | None = Field(
        default=None,
        description="Optional turn id to associate the widget with a chat turn.",
    )


class UpdateParams(BaseModel):
    """Patch a previously rendered widget."""
    widget_id: str = Field(..., description="The id returned by the render call.")
    patch: dict[str, Any] = Field(
        ...,
        description="Dotted-path keys: 'data.sources', 'state.filter', 'ctx.path'…",
    )


class CloseParams(BaseModel):
    widget_id: str = Field(..., description="Widget to unmount.")


class ErrorParams(BaseModel):
    """Surface an error inside a mounted widget without closing it."""
    widget_id: str = Field(...)
    binding: str | None = Field(
        default=None,
        description="Optional name of the data binding that failed.",
    )
    message: str = Field(..., description="Human-readable error.")


class GetStateParams(BaseModel):
    """Read the session's widget state (or one key)."""
    key: str | None = Field(
        default=None,
        description=(
            "Optional dotted-path key to read a single value, e.g. "
            "'form.email' or 'results.rag.query'. When omitted, the "
            "full snapshot is returned."
        ),
    )


class SetStateParams(BaseModel):
    """Write into the session's widget state.

    Used by the agent when it wants to expose a value to the next
    widget render or to other modules that read widget state via
    ``ctx.widget_state``. The same map is also injected into the
    system prompt under the WIDGET CONTEXT section.
    """
    set: dict[str, Any] = Field(
        ...,
        description="Key/value pairs to merge into the session state.",
    )


class ClearParams(BaseModel):
    """No params - clears all mounted widgets + state for the session."""


# ─────────────────────────── module ────────────────────────────


class WidgetModule(BaseModule):
    """Per-session declarative-widget runtime."""

    MODULE_ID = "widget"
    VERSION = "1.0.0"
    CONFIG_MODEL = WidgetModuleConfig

    def get_prompt_sections(self) -> list[dict[str, Any]]:
        """Inject the current session's widget state into the system prompt.

        Every value the user typed in a form, every result of a tool
        triggered by a widget, every set_state call - all of it
        appears under a ``# Widget context`` section so the agent can
        reference them in its reasoning and pass them to subsequent
        tool calls. This is what makes widgets behave as a
        bidirectional bridge between the UI and the agent.

        Format (rendered as markdown):

            # Widget context

            ## form.last_booking_topic
            "1:1 with Alice"

            ## state.selected_sources
            ["s1", "s2", "s3"]

            ## last tool result
            tool: rag.query → {"hits": 12, "top_score": 0.94}

        The agent loop calls this hook every turn for the active
        session, so the state is always fresh.
        """
        import json as _json

        sid = self._active_session_id
        if not sid:
            return []
        sess = self._store.get(sid)
        if sess is None or not sess.state:
            return []

        lines: list[str] = []
        state = sess.state

        # Form fields - the most common case
        form = state.get("form") or {}
        if form:
            lines.append("## Form values")
            for k, v in form.items():
                pretty = v if isinstance(v, str) else _json.dumps(v, default=str)
                lines.append(f"- **{k}**: {pretty}")
            lines.append("")

        # Free-form state set via widget.set_state or set_state action
        scalars: dict[str, Any] = {}
        for k, v in state.items():
            if k in ("form", "results", "last_result", "last_form"):
                continue
            scalars[k] = v
        if scalars:
            lines.append("## Session state")
            for k, v in scalars.items():
                pretty = v if isinstance(v, str) else _json.dumps(v, default=str)
                if len(str(pretty)) > 200:
                    pretty = str(pretty)[:200] + "…"
                lines.append(f"- **{k}**: {pretty}")
            lines.append("")

        # Last tool result triggered from a widget
        last = state.get("last_result")
        if last:
            tool_name = last.get("tool", "?")
            value = last.get("value")
            pretty = _json.dumps(value, default=str)[:400]
            lines.append("## Last widget tool result")
            lines.append(f"- **{tool_name}**: {pretty}")
            lines.append("")

        # Mounted widgets - useful when the agent needs to reference
        # widget_ids for update/close calls.
        if sess.mounted:
            lines.append("## Currently mounted widgets")
            for wid, mw in sess.mounted.items():
                ref_or_inline = mw.ref or "(inline)"
                lines.append(f"- **{wid}** (zone={mw.zone}, ref={ref_or_inline})")
            lines.append("")

        if not lines:
            return []

        return [{
            "title": "WIDGET CONTEXT",
            "content": "\n".join(lines).strip(),
            "priority": 6,
        }]

    def get_manifest(self) -> ModuleManifest:
        return ModuleManifest.from_module(self).model_copy(update={
            "description": (
                "Declarative UI widgets - agent pushes render/update/close "
                "events via Socket.IO. Trees are validated at compile time; "
                "the Flutter client renders them with no extra code."
            ),
            "author": "Digitorn Team",
        })

    def __init__(self) -> None:
        super().__init__()
        self._store = WidgetSessionStore()
        self._active_session_id: str | None = None
        # Socket.IO bridge - set by bootstrap to emit events on the bus
        self._event_bus: Any | None = None
        self._bus_app_id: str | None = None

    # ── session wiring ────────────────────────────────────────

    def set_active_session(self, session_id: str | None) -> None:
        self._active_session_id = session_id

    def _resolve_session_id(self) -> str:
        return self._active_session_id or "_default_"

    def _session(self) -> WidgetSessionState:
        return self._store.get_or_create(self._resolve_session_id())

    def _build_scopes(
        self,
        sess: WidgetSessionState,
        ctx: dict[str, Any] | None = None,
        item: Any = None,
    ) -> dict[str, Any]:
        """Build the scope dict the expression evaluator reads from.

        Mirrors the spec §6.1: ``form``, ``state``, ``ctx``, ``item``,
        ``session`` (id), ``app`` (id) - everything the agent or the
        UI might want to substitute into a widget tree.
        """
        return {
            "form": dict(sess.state.get("form") or {}),
            "state": {k: v for k, v in sess.state.items() if k != "form"},
            "ctx": dict(ctx or {}),
            "item": item,
            "session": {"session_id": sess.session_id},
        }

    async def cleanup_session(self, session_id: str) -> None:
        self._store.drop(session_id)

    def snapshot_for(self, session_id: str) -> dict[str, Any]:
        state = self._store.get(session_id) or self._store.get_or_create(session_id)
        return state.snapshot()

    # ── publish helper ────────────────────────────────────────

    def _publish(
        self,
        session_state: WidgetSessionState,
        event_type: str,
        data: dict[str, Any],
    ) -> WidgetEvent:
        seq = session_state.next_seq()
        evt = WidgetEvent(seq=seq, event_type=event_type, data=data)
        session_state.events.append(evt)

        # ── Socket.IO: emit to session room via SocketIOBus ──
        bus = self._event_bus
        if bus is not None:
            user_id = getattr(session_state, "user_id", None) or "local"
            app_id = self._bus_app_id or ""
            sid = session_state.session_id
            key = bus.session_key(app_id, sid, user_id)
            try:
                loop = asyncio.get_running_loop()
                # event_type already includes "widget:" prefix
                bus_type = event_type if event_type.startswith("widget:") else f"widget:{event_type}"
                loop.create_task(bus.publish(key, {
                    "type": bus_type,
                    "data": {**data, "widget_seq": seq},
                }))
            except RuntimeError:
                pass
        return evt

    # ── actions ────────────────────────────────────────────────

    @action(
        description="Render a widget in one of the 4 zones (inline, chat_side, workspace, modal).",
        params_model=RenderParams,
        risk_level="low",
        tags=["widget", "ui"],
    )
    async def render(self, params: RenderParams) -> ActionResult:
        if params.zone not in _VALID_ZONES:
            return ActionResult(
                success=False,
                error=f"unknown zone {params.zone!r} (allowed: {sorted(_VALID_ZONES)})",
            )
        if params.ref is None and params.tree is None:
            return ActionResult(
                success=False,
                error="render() requires either ``ref`` (inline widget name) or ``tree`` (inline tree)",
            )
        if params.ref is not None and params.tree is not None:
            return ActionResult(
                success=False,
                error="render() takes either ``ref`` OR ``tree``, not both",
            )

        sess = self._session()
        widget_id = params.widget_id or f"w_{uuid.uuid4().hex[:12]}"

        # ── Server-side template substitution ────────────────────
        # If the agent passes a tree containing ``{{form.X}}`` or
        # ``{{state.X}}`` references, resolve them against the live
        # session state BEFORE we publish the event. This way
        # the client renders concrete values without needing its
        # own copy of the state map. The ``ctx`` provided by the
        # agent is also exposed to the evaluator as ``{{ctx.X}}``.
        scopes = self._build_scopes(sess, ctx=params.ctx)
        rendered_tree = (
            substitute_tree(params.tree, scopes)
            if params.tree is not None else None
        )

        mounted = MountedWidget(
            widget_id=widget_id,
            zone=params.zone,
            target=params.target,
            ref=params.ref,
            tree=rendered_tree,
            ctx=dict(params.ctx),
            turn_id=params.turn_id,
        )
        sess.mounted[widget_id] = mounted
        self._publish(sess, "widget:render", mounted.to_dict())
        return ActionResult(success=True, data={"widget_id": widget_id})

    @action(
        description="Patch a previously rendered widget (data.X, state.X, ctx.X).",
        params_model=UpdateParams,
        risk_level="low",
        tags=["widget", "ui"],
    )
    async def update(self, params: UpdateParams) -> ActionResult:
        sess = self._session()
        if params.widget_id not in sess.mounted:
            return ActionResult(
                success=False,
                error=f"widget {params.widget_id!r} not mounted in session {sess.session_id!r}",
            )
        # Substitute ``{{...}}`` tokens inside the patch values too -
        # the agent can write ``patch={"state.greeting": "Hello {{form.name}}"}``
        # and have it resolved against the live state before sending.
        scopes = self._build_scopes(sess)
        rendered_patch = substitute_tree(params.patch, scopes)
        self._publish(sess, "widget:update", {
            "widget_id": params.widget_id,
            "patch": rendered_patch,
        })
        return ActionResult(success=True, data={"widget_id": params.widget_id})

    @action(
        description="Close (unmount) a previously rendered widget.",
        params_model=CloseParams,
        risk_level="low",
        tags=["widget", "ui"],
    )
    async def close(self, params: CloseParams) -> ActionResult:
        sess = self._session()
        existed = sess.mounted.pop(params.widget_id, None)
        self._publish(sess, "widget:close", {"widget_id": params.widget_id})
        return ActionResult(
            success=True,
            data={"widget_id": params.widget_id, "was_mounted": existed is not None},
        )

    @action(
        description="Surface an error in a widget (e.g. failed data binding) without closing it.",
        params_model=ErrorParams,
        risk_level="low",
        tags=["widget", "ui"],
    )
    async def error(self, params: ErrorParams) -> ActionResult:
        sess = self._session()
        self._publish(sess, "widget:error", {
            "widget_id": params.widget_id,
            "binding": params.binding,
            "message": params.message,
        })
        return ActionResult(success=True, data={"widget_id": params.widget_id})

    @action(
        description="Read the session's widget state (or one key via dotted path).",
        params_model=GetStateParams,
        risk_level="low",
        tags=["widget", "ui"],
    )
    async def get_state(self, params: GetStateParams) -> ActionResult:
        sess = self._session()
        if params.key is None:
            return ActionResult(success=True, data=sess.snapshot())
        # Dotted-path read into the state map only (not the whole snapshot)
        cursor: Any = sess.state
        for part in params.key.split("."):
            if isinstance(cursor, dict) and part in cursor:
                cursor = cursor[part]
            else:
                return ActionResult(success=True, data={"value": None, "found": False})
        return ActionResult(success=True, data={"value": cursor, "found": True})

    @action(
        description="Write into the session's widget state - visible to the agent and other modules.",
        params_model=SetStateParams,
        risk_level="low",
        tags=["widget", "ui"],
    )
    async def set_state(self, params: SetStateParams) -> ActionResult:
        sess = self._session()
        sess.state.update(params.set)
        self._publish(sess, "widget:state", {"state": dict(sess.state)})
        return ActionResult(success=True, data={"state": dict(sess.state)})

    @action(
        description="Clear all mounted widgets and state for the session.",
        params_model=ClearParams,
        risk_level="low",
        tags=["widget", "ui"],
    )
    async def clear(self, params: ClearParams) -> ActionResult:
        sess = self._session()
        sess.clear()
        self._publish(sess, "widget:cleared", {})
        return ActionResult(success=True, data={"cleared": True})
