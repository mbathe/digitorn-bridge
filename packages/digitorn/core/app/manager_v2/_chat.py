"""_ChatMixin - conversation turn execution."""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import Any

from digitorn.core.app.sessions import ConversationSession
from digitorn.core.runtime.system_directive import inject_system_directive
from digitorn.core.runtime.types import TurnResult

from ._models import _resolve_tool_display, _recover_interrupted_session

logger = logging.getLogger(__name__)


# Strong-ref set for fire-and-forget session_store persists; without it asyncio GC can drop the task mid-write.
_BG_SESSION_PERSIST_TASKS: set[asyncio.Task] = set()
_BG_SESSION_PERSIST_MAX = 1000

# Strong-ref set for end-of-turn emits; Socket.IO emit can take 1-5s under backpressure and we don't want to hold the session lock.
_END_OF_TURN_EMIT_TASKS: set[asyncio.Task] = set()


def _schedule_bg_persist_msgs_events(
    store: Any,
    app_id: str,
    session_id: str,
    turn_index: int,
    snap_messages: list[dict[str, Any]],
    snap_events: list[dict[str, Any]],
    user_id: str,
    *,
    messages_baseline: int | None = None,
) -> None:
    if len(_BG_SESSION_PERSIST_TASKS) >= _BG_SESSION_PERSIST_MAX:
        logger.warning(
            "session_persist_backpressure dropping new persist "
            "(in_flight=%d max=%d) app=%s sid=%s - DB layer "
            "(_persist_turn_bg) unaffected; session_store cache will "
            "rehydrate from DB on next read",
            len(_BG_SESSION_PERSIST_TASKS), _BG_SESSION_PERSIST_MAX,
            app_id, session_id,
        )
        return

    # Delta path is O(1) per turn but only valid when baseline is set and compaction didn't shrink the list.
    _use_delta = (
        messages_baseline is not None
        and 0 <= messages_baseline <= len(snap_messages)
    )
    if _use_delta:
        _delta_msgs = snap_messages[messages_baseline:]
        _msgs_op = asyncio.to_thread(
            store.save_turn_messages, app_id, session_id, turn_index,
            _delta_msgs, user_id=user_id,
        )
    else:
        _msgs_op = asyncio.to_thread(
            store.save_messages, app_id, session_id, snap_messages,
            user_id=user_id,
        )

    async def _bg() -> None:
        try:
            results = await asyncio.gather(
                _msgs_op,
                asyncio.to_thread(
                    store.save_turn_events, app_id, session_id,
                    turn_index, snap_events, user_id=user_id,
                ),
                return_exceptions=True,
            )
            for r in results:
                if isinstance(r, BaseException):
                    logger.warning(
                        "session_persistence_bg_failed app=%s sid=%s: %s",
                        app_id, session_id, r,
                    )
        except Exception as exc:
            logger.warning(
                "session_persistence_bg_dispatch_failed app=%s sid=%s: %s",
                app_id, session_id, exc,
            )

    task = asyncio.create_task(
        _bg(), name=f"session-persist:{app_id}:{session_id}",
    )
    _BG_SESSION_PERSIST_TASKS.add(task)
    task.add_done_callback(_BG_SESSION_PERSIST_TASKS.discard)


class _ChatMixin:
    """Conversation turn execution methods."""

    def _register_wake_handler(self, app_id: str) -> None:
        async def _wake(session_id: str, message: str) -> None:
            existing = await asyncio.to_thread(self._session_store.get, app_id, session_id)
            if existing is None:
                logger.info(
                    "wake_skipped session_not_found app=%s session=%s",
                    app_id, session_id,
                )
                return
            try:
                await self.chat(
                    app_id, session_id, message,
                    user_id=existing.user_id,
                    reminder=True,
                )
            except Exception as exc:
                logger.warning(
                    "wake_chat_failed app=%s session=%s error=%s",
                    app_id, session_id, exc,
                )
        self._scheduler.register_wake_handler(app_id, _wake)

    async def chat(
        self,
        app_id: str,
        session_id: str,
        message: str,
        *,
        user_id: str | None = None,
        workspace: str | None = None,
        on_tool_call: Any | None = None,
        on_tool_start: Any | None = None,
        on_thinking: Any | None = None,
        on_thinking_started: Any | None = None,
        on_thinking_delta: Any | None = None,
        on_hook_event: Any | None = None,
        on_token: Any | None = None,
        on_stream_done: Any | None = None,
        on_status: Any | None = None,
        on_out_token: Any | None = None,
        on_in_token: Any | None = None,
        image_refs: list[dict[str, Any]] | None = None,
        reminder: bool = False,
        correlation_id: str | None = None,
        client_message_id: str | None = None,
        template_system_prompt: str = "",
        mode_id: str | None = None,
    ) -> TurnResult:
        """Process a single conversation message within a session."""
        deployed = self._get_deployed(app_id, user_id=user_id)

        ws_mode = getattr(deployed.compiled.execution, "workspace_mode", "auto")
        yaml_ws = getattr(deployed.compiled.execution, "workspace", "")

        # Try to reuse persisted workspace from the session (set on first call)
        uid = user_id or "local"
        _existing_session = await asyncio.to_thread(self._session_store.get, app_id, session_id, user_id=uid)
        _persisted_ws = getattr(_existing_session, "workspace", "") if _existing_session else ""

        if ws_mode == "none":
            ws = ""
        elif ws_mode == "fixed":
            ws = str(Path(yaml_ws).resolve()) if yaml_ws else str(Path.cwd())
        elif ws_mode == "required":
            # Use client workspace, or persisted, or fail
            ws = workspace or _persisted_ws
            if not ws:
                raise RuntimeError("This app requires a workspace. Set one before chatting.")
            ws = str(Path(ws).resolve())
        else:
            # workspace_mode=auto defaults to a per-session isolated dir so writes don't leak into the daemon's cwd.
            per_session_default = str(
                Path.home() / ".digitorn" / "workspaces" / app_id / session_id
            )
            # Reject a persisted ws equal to the daemon's cwd (stale value from before per-session defaults).
            daemon_cwd = str(Path.cwd().resolve())
            if _persisted_ws:
                try:
                    if str(Path(_persisted_ws).resolve()) == daemon_cwd:
                        _persisted_ws = ""
                except OSError:
                    pass
            if workspace or _persisted_ws or yaml_ws:
                ws = workspace or _persisted_ws or yaml_ws
            else:
                ws = per_session_default
            ws = str(Path(ws).resolve()) if ws else ""

        if deployed.mode not in ("conversation", "one_shot"):
            raise RuntimeError(
                f"App '{app_id}' is in '{deployed.mode}' mode, "
                f"not compatible with chat"
            )

        from digitorn.core.workspace import WorkspaceLayout
        from digitorn.core.workdirs import is_named_project_path
        # Three workspace shapes (per-session daemon ws, project-shared workdir, user-picked folder); layouts differ for each.
        _per_session_ws = False
        _external_session_dir: Path | None = None
        if ws:
            try:
                _ws_path = Path(ws).resolve()
                if _ws_path.name == session_id and _ws_path.parent.name == app_id:
                    _per_session_ws = True
                elif is_named_project_path(_ws_path):
                    _per_session_ws = True
                    _external_session_dir = (
                        Path.home()
                        / ".digitorn" / "workspaces"
                        / app_id / session_id
                    )
            except Exception:
                _per_session_ws = False
                _external_session_dir = None
        layout = WorkspaceLayout(
            ws, app_id,
            per_session=_per_session_ws,
            external_session_dir=_external_session_dir,
        )
        layout.ensure_session_dirs(session_id)

        fs_mod = deployed.modules.get("filesystem")
        if fs_mod and hasattr(fs_mod, "_checkpoint_dir"):
            fs_mod._checkpoint_dir = str(layout.session_checkpoints_dir(session_id))

        session_lock = self._session_store.session_lock(app_id, session_id, uid)
        active_key = f"{app_id}:{session_id}"
        self._active_sessions.add(active_key)
        lock_acquired = False
        try:
            try:
                # Short lock-wait safety net; the API-layer queue handles real busy cases gracefully.
                try:
                    from digitorn.core.config import get_settings
                    _lock_timeout = get_settings().session.lock_timeout
                except Exception:
                    _lock_timeout = 30.0
                _lock_timeout = min(max(float(_lock_timeout), 5.0), 60.0)
                await asyncio.wait_for(
                    session_lock.acquire(), timeout=_lock_timeout,
                )
                lock_acquired = True
            except asyncio.TimeoutError:
                # The substring "session lock" is matched by _classify_error to map to session_busy / HTTP 409.
                raise RuntimeError(
                    f"Session lock timeout after {_lock_timeout:.0f}s "
                    f"for {app_id}/{session_id} - another turn is "
                    f"still in progress; retry or use the message queue."
                )
            result = await self._chat_locked(
                deployed, app_id, session_id, uid, message, ws,
                on_tool_call, on_tool_start, on_thinking,
                on_hook_event, on_token,
                on_out_token, on_in_token,
                on_thinking_started=on_thinking_started,
                image_refs=image_refs,
                on_thinking_delta=on_thinking_delta,
                on_stream_done=on_stream_done,
                on_status=on_status,
                reminder=reminder,
                correlation_id=correlation_id,
                client_message_id=client_message_id,
                template_system_prompt=template_system_prompt,
                mode_id=mode_id,
            )
            return result
        finally:
            # Each cleanup wrapped - finally must never raise
            try:
                if lock_acquired:
                    session_lock.release()
            except Exception:
                logger.warning("session_lock_release_failed app=%s session=%s", app_id, session_id, exc_info=True)
            try:
                self._active_sessions.discard(active_key)
            except Exception:
                logger.debug("active_sessions_discard_failed", exc_info=True)
            try:
                self.turn_state_end(app_id, session_id)
            except Exception:
                logger.debug("turn_state_end_failed", exc_info=True)

    async def _chat_locked(
        self,
        deployed: Any,
        app_id: str,
        session_id: str,
        uid: str,
        message: str,
        workspace: str,
        on_tool_call: Any,
        on_tool_start: Any,
        on_thinking: Any,
        on_hook_event: Any,
        on_token: Any,
        on_out_token: Any | None = None,
        on_in_token: Any | None = None,
        on_thinking_started: Any | None = None,
        on_thinking_delta: Any | None = None,
        on_stream_done: Any | None = None,
        on_status: Any | None = None,
        image_refs: list[dict[str, Any]] | None = None,
        reminder: bool = False,
        correlation_id: str | None = None,
        client_message_id: str | None = None,
        template_system_prompt: str = "",
        mode_id: str | None = None,
    ) -> "TurnResult":
        from digitorn.core.runtime.agent_loop import agent_turn
        from digitorn.core.runtime.mode_merge import resolve_mode

        from digitorn.core.runtime.types import WORKSPACE_PLACEHOLDER
        yaml_ws = getattr(deployed.compiled.execution, "workspace", "") or ""
        effective_prompt = deployed.entry_context.system_prompt or ""

        if effective_prompt:
            resolved_ws = workspace or yaml_ws or ""
            if yaml_ws and workspace and yaml_ws != workspace:
                effective_prompt = effective_prompt.replace(yaml_ws, workspace)
            effective_prompt = effective_prompt.replace(WORKSPACE_PLACEHOLDER, resolved_ws)

        session = await asyncio.to_thread(self._session_store.get, app_id, session_id, user_id=uid)
        if session is None:
            persisted_messages = await asyncio.to_thread(self._session_store.load_messages, app_id, session_id, user_id=uid)
            session = ConversationSession(
                session_id=session_id,
                app_id=app_id,
                user_id=uid,
                workspace=workspace,
            )
            if persisted_messages:
                session.messages = persisted_messages
                logger.info(
                    "Resumed session '%s' for app '%s' user '%s' (%d messages)",
                    session_id, app_id, uid, len(persisted_messages),
                )
            else:
                session.add_system(effective_prompt)
                logger.info(
                    "New session '%s' for app '%s' user '%s' workspace='%s'",
                    session_id, app_id, uid, workspace,
                )

        # Ensure messages[0] carries the deploy-time system prompt regardless of where the session came from; idempotent.
        try:
            has_system_msg = any(
                m.get("role") == "system" for m in session.messages
            )
            if not has_system_msg and effective_prompt:
                session.messages.insert(
                    0, {"role": "system", "content": effective_prompt},
                )
                logger.warning(
                    "Healed missing system prompt for session '%s' "
                    "app '%s' user '%s' (sys_prompt_len=%d)",
                    session_id, app_id, uid, len(effective_prompt),
                )
        except Exception as _heal_exc:
            logger.debug(
                "system_prompt heal skipped (%s): %s",
                type(_heal_exc).__name__, _heal_exc,
            )

        # Update workspace on the session if it was missing (e.g. old sessions)
        if workspace and not session.workspace:
            session.workspace = workspace
        # Legacy clients still pass `workspace` on each message; promote it to workdir when missing.
        if workspace and not getattr(session, "workdir", ""):
            session.workdir = workspace

        # sessions that were persisted before substitution was applied at creation.
        from digitorn.core.runtime.types import apply_workspace_to_messages
        apply_workspace_to_messages(session.messages, workspace, yaml_ws)

        _turn_index = session.turn_count
        _event_log: list[dict[str, Any]] = []
        _out_token_total = [0]
        _in_token_total = [0]

        try:
            from digitorn.core.config import get_settings
            _MAX_EVENTS_PER_TURN = get_settings().session.max_events_per_turn
        except Exception:
            _MAX_EVENTS_PER_TURN = 50000  # Safety cap - prevent OOM on runaway turns

        def _log_event(event_type: str, data: dict[str, Any]) -> None:
            if len(_event_log) >= _MAX_EVENTS_PER_TURN:
                return  # Silently drop - turn is already too large
            _event_log.append({
                "type": event_type,
                "ts": time.time(),
                "turn": _turn_index,
                "data": data,
            })

        async def _bus_capture(captured_user_id: str, envelope: dict[str, Any]) -> None:
            try:
                env_sid = envelope.get("session_id")
                if env_sid and env_sid != session_id:
                    return  # Ignore events from other sessions
                ev_type = envelope.get("type") or "unknown"
                # Skip types already logged explicitly (avoid duplicates)
                if ev_type in ("tool_start", "tool_call", "thinking", "status",
                               "stream_done", "hook", "token_count",
                               "turn_start", "turn_end", "stream_text",
                               "thinking_filtered"):
                    return
                _log_event(ev_type, envelope.get("payload") or {})
            except Exception as exc:
                logger.debug("bus_capture event log failed: %s", exc)

        try:
            self.event_bus.add_handler(_bus_capture)
        except Exception:
            logger.debug("bus_capture_handler_add_failed", exc_info=True)

        if session.memory_snapshot:
            _mem = deployed.entry_context.memory_module
            if _mem and hasattr(_mem, 'store') and _mem.store:
                _mem.store.restore_from_dict(session.memory_snapshot)
                logger.info(
                    "Memory restored for session '%s' (goal=%s, todos=%d, facts=%d)",
                    session_id,
                    bool(_mem.store.working.goal),
                    len(_mem.store.working.todos),
                    len(_mem.store.working.key_facts),
                )

        if session.interrupted and session.messages:
            session.interrupted = False  # Clear flag
            _recovered = _recover_interrupted_session(session.messages)
            logger.info(
                "Session '%s' resumed after interruption (%d tool calls recovered)",
                session_id, _recovered,
            )

        # Snapshot count before this turn so the end-of-turn delta persist stays O(1).
        _messages_baseline = len(session.messages)

        # Fresh turn: drop any error left by the previous turn on the canonical
        # SessionState (the ConversationSession is a throwaway view) so poll-based
        # clients don't surface a stale failure. Re-set below if this turn fails.
        try:
            _st0 = self._session_store._store.state(session_id)
            if _st0 is not None:
                _st0.last_error = None
        except Exception:
            logger.debug("clear last_error failed", exc_info=True)

        # Build user message - multimodal if images provided
        if image_refs:
            from digitorn.core.runtime.multimodal import build_user_message_with_images
            user_msg = build_user_message_with_images(message, image_refs)
            session.messages.append(user_msg)
            if not session.title and message:
                session.title = message[:80]
        elif reminder:
            cron_content = (
                f"[REMINDER from cron] You scheduled this earlier and it "
                f"just fired. Take whatever action you committed to. "
                f"Message: {message}"
            )
            await inject_system_directive(
                None,
                content=cron_content,
                source="cron_reminder",
                messages=session.messages,
                bus=self.event_bus,
                app_id=app_id,
                session_id=session_id,
                user_id=uid or "local",
                metadata={"message": message[:200]},
            )
            session.last_active = time.time()
        else:
            session.add_user(message)

        # Persist per-turn iframe addendums as system_message events so they replay in order on cold reload.
        if template_system_prompt:
            await inject_system_directive(
                None,
                content=template_system_prompt,
                source="template_addendum",
                messages=session.messages,
                bus=self.event_bus,
                app_id=app_id,
                session_id=session_id,
                user_id=uid or "local",
                metadata={"scope": "turn"},
            )
        _log_event("turn_start", {"message": message, "images": len(image_refs or [])})

        await asyncio.to_thread(self._session_store.put, session)

        from digitorn.core.runtime.types import apply_workspace_override

        import copy
        ctx = copy.copy(deployed.entry_context)
        ctx.session_id = session_id
        ctx.user_id = uid
        # Tag ctx with app_id so SessionMetrics + usage_events accumulate in the right bucket.
        ctx.app_id = app_id
        ctx.template_system_prompt = ""
        # Agent tools operate inside ctx.workspace; route to workdir so they never touch the daemon-private session workspace.
        agent_workdir = (
            getattr(session, "workdir", "")
            or workspace
            or session.workspace
        )
        # Safety net so shell.bash always has a cwd; materialise the per-session workspace if upstream missed it.
        if not agent_workdir:
            from pathlib import Path as _Path
            _fallback = _Path.home() / ".digitorn" / "workspaces" / app_id / session_id
            try:
                _fallback.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                logger.warning("session workspace dir create failed at %s: %s", _fallback, exc)
            agent_workdir = str(_fallback)
            if not session.workspace:
                session.workspace = agent_workdir
            if not getattr(session, "workdir", ""):
                session.workdir = agent_workdir
        apply_workspace_override(ctx, agent_workdir, yaml_ws)

        # For authenticated users without BYOK, hot-swap ctx.provider to talk to the gateway with the user's JWT.
        try:
            from digitorn.core.credentials.gateway_resolver import (
                resolve_session_provider,
            )
            from digitorn.core.credentials.byok_store import (
                is_byok_enabled,
            )
            from digitorn.core.config import get_settings
            _settings_snapshot = get_settings()
            agent_block = next(
                (a for a in deployed.compiled.agents
                 if a.agent_id == ctx.agent_id),
                None,
            )
            if agent_block is not None and ctx.provider is not None:
                byok_on = await is_byok_enabled(uid, app_id)
                resolved = await resolve_session_provider(
                    deployed_provider=ctx.provider,
                    agent=agent_block,
                    user_id=uid,
                    app_id=app_id,
                    modules=getattr(deployed, "modules", {}) or {},
                    settings=_settings_snapshot,
                    byok_enabled=byok_on,
                )
                if resolved is not ctx.provider:
                    # Re-wrap the resolved provider so the session keeps using the worker LLMProviderProxy; idempotent.
                    try:
                        from digitorn.workers.llm_wrap import (
                            maybe_wrap_provider,
                        )
                        resolved = maybe_wrap_provider(
                            resolved, agent_block.brain,
                        )
                    except Exception as exc:
                        logger.debug(
                            "session_provider_wrap_skipped agent=%s "
                            "err=%s", agent_block.agent_id, exc,
                        )
                    ctx.provider = resolved

                # Per-session provider stash for the classifier so concurrent users don't race the shared module singleton.
                try:
                    bm = getattr(ctx, "behavior_module", None)
                    cls_default = getattr(bm, "_classifier_provider", None)
                    bcfg = getattr(deployed.compiled, "behavior", None)
                    bbrain = getattr(bcfg, "brain", None) if bcfg else None
                    if (
                        cls_default is not None
                        and bbrain is not None
                        and getattr(bbrain, "model", "")
                    ):
                        if byok_on:
                            ctx._session_classifier_provider = cls_default
                        else:
                            cls_resolved = await resolve_session_provider(
                                deployed_provider=cls_default,
                                agent=type("_Wrap", (), {"brain": bbrain})(),
                                user_id=uid,
                                app_id=app_id,
                                modules=getattr(deployed, "modules", {}) or {},
                                settings=_settings_snapshot,
                                byok_enabled=False,
                            )
                            ctx._session_classifier_provider = cls_resolved
                except Exception as cexc:
                    logger.debug(
                        "classifier_provider per-session resolve skipped: %s",
                        cexc,
                    )
        except Exception as exc:
            logger.warning(
                "gateway_resolver failed for app=%s user=%s; keeping "
                "deployed provider: %s", app_id, uid, exc, exc_info=True,
            )

        if deployed.sandbox_pool is not None:
            # Per-session sandbox: acquire a worker from the pool
            try:
                pool_worker = await deployed.sandbox_pool.acquire(workspace, session_id)
                ctx.sandbox_worker = pool_worker
            except Exception as exc:
                logger.error("sandbox_pool_acquire_failed app=%s session=%s: %s", app_id, session_id, exc)
                # Fall through without sandbox - better than crashing
        elif deployed.sandbox_worker is not None:
            deployed.sandbox_worker.update_workspace(workspace)
            ctx.sandbox_worker = deployed.sandbox_worker

        cb = deployed.context_builder
        if cb is not None:
            cb._agent_context = ctx

        bus_key = self.event_bus.session_key(app_id, session_id, uid)

        # Wire event bus to agent context so emergency compaction can emit events
        ctx._event_bus = self.event_bus
        ctx._bus_key = bus_key

        # Register TurnState eagerly so /state can report the running turn before the first token event.
        _turn_corr_id = correlation_id or ""
        # Stash correlation_id on ctx so streaming snapshots can include it and clients don't double-bubble.
        ctx._correlation_id = _turn_corr_id
        if _turn_corr_id:
            self.turn_state_begin(app_id, session_id, _turn_corr_id)
            self._start_turn_heartbeat(app_id, session_id, uid, _turn_corr_id)

        _save_counter = 0

        async def _on_tool_call(name: str, params: dict, result: Any, call_id: str = "") -> None:
            # Recover empty-name tool calls (streaming chunk fragmented before fqn arrived) so clients don't see "?" bubbles.
            if not name:
                recovered = (params or {}).get("name") or ""
                if not recovered and isinstance(result, dict):
                    recovered = (
                        result.get("name", "")
                        or result.get("tool", "")
                        or ""
                    )
                if not recovered:
                    recovered = "unknown"
                import traceback as _tb
                logger.warning(
                    "tool_call_empty_name recovered=%r call_id=%r "
                    "params_keys=%s result_type=%s "
                    "stack=%s",
                    recovered, call_id,
                    list((params or {}).keys())[:5],
                    type(result).__name__,
                    "".join(_tb.format_stack()[-4:-1]).replace("\n", " | "),
                )
                name = recovered
            nonlocal _save_counter
            ok, err = True, ""
            result_data: Any = None
            if isinstance(result, dict):
                if result.get("success") is False:
                    ok = False
                if result.get("error") and result.get("error") != "":
                    ok = False
                    err = str(result.get("error", ""))
                result_data = result
            elif hasattr(result, "success"):
                ok = result.success
                err = getattr(result, "error", "") or ""
                if hasattr(result, "data") and isinstance(result.data, dict):
                    result_data = result.data

            from digitorn.core.cli.ui import _tool_label
            label, detail = _tool_label(name, params)

            display = _resolve_tool_display(deployed, name, params)

            event_data: dict[str, Any] = {
                "id": call_id,
                "name": name, "params": params,
                "success": ok, "error": err,
                "label": label, "detail": detail,
                "display": display,
                "result": result_data,
            }

            # Include unified diff for edit-type tools (clients display it inline)
            if isinstance(result_data, dict) and "diff" in result_data:
                event_data["diff"] = result_data["diff"][:4000]

            # Include previous_content from metadata for frontend diff view.
            # metadata is NOT sent to the LLM - only to SSE clients.
            _meta = getattr(result, "metadata", None)
            if not _meta and isinstance(result, dict):
                _meta = result.get("metadata")
            if isinstance(_meta, dict):
                if "previous_content" in _meta:
                    event_data["previous_content"] = _meta["previous_content"]
                if "new_content" in _meta:
                    event_data["new_content"] = _meta["new_content"]
                if "image_data" in _meta:
                    event_data["image_data"] = _meta["image_data"]
                    event_data["image_mime"] = _meta.get("media_type", "image/png")

            from digitorn.core.events.envelope import (
                SessionEvent, OpType, OpState, gen_op_id,
            )
            # Reuse the tool_start op_id so the client correlates running -> completed on the same chip.
            op_id = call_id or gen_op_id("tool")
            op_state = OpState.FAILED if not ok else OpState.COMPLETED
            event_data["op_id"] = op_id
            event_data["correlation_id"] = correlation_id or None
            await self.event_bus.emit(SessionEvent.build(
                type="tool_call",
                app_id=app_id,
                session_id=session_id,
                user_id=uid,
                op_id=op_id,
                op_type=OpType.TOOL,
                op_state=op_state,
                correlation_id=correlation_id or "",
                payload=event_data,
            ))

            # Derived events - mirror what /chat/stream builds from tool_call
            # Resolve short names (Agent → agent_spawn.spawn_agent) to get the action part
            from digitorn.core.runtime.tool_names import to_fqn
            inner_name = params.get("name", name) if name == "execute_tool" else name
            resolved = to_fqn(inner_name)
            action = resolved.split(".")[-1] if "." in resolved else inner_name
            logger.debug(
                "derived_event_check name=%r action=%r result_data_type=%s",
                name, action, type(result_data).__name__ if result_data else "None",
            )

            _MEMORY_ACTIONS = {"set_goal", "remember", "task_create", "task_update"}
            _SHELL_ACTIONS = {"bash"}
            _AGENT_ACTIONS = {"agent"}

            if action in _MEMORY_ACTIONS:
                from digitorn.core.events.envelope import (
                    SessionEvent as _SE, OpType as _OT, OpState as _OS,
                )
                await self.event_bus.emit(_SE.build(
                    type="memory_update",
                    app_id=app_id, session_id=session_id, user_id=uid,
                    op_id=(call_id or op_id or f"memory-{name}"),
                    op_type=_OT.TOOL, op_state=_OS.COMPLETED,
                    op_parent_id=(call_id or op_id) if call_id else None,
                    correlation_id=correlation_id or "",
                    payload={
                        "action": action, "result": result_data, "name": name,
                        "op_parent_id": call_id or op_id,
                    },
                ))
            elif action in _SHELL_ACTIONS:
                # Extract stdout/stderr - try every known result structure
                _stdout, _stderr = "", ""
                for src in (result_data, getattr(result, "data", None), result):
                    if isinstance(src, dict) and ("stdout" in src or "stderr" in src):
                        _stdout = src.get("stdout", "")
                        _stderr = src.get("stderr", "")
                        break
                    if isinstance(src, dict) and "data" in src and isinstance(src["data"], dict):
                        _stdout = src["data"].get("stdout", "")
                        _stderr = src["data"].get("stderr", "")
                        break
                if _stdout or _stderr:
                    from digitorn.core.events.envelope import (
                        SessionEvent as _SE, OpType as _OT, OpState as _OS,
                    )
                    await self.event_bus.emit(_SE.build(
                        type="terminal_output",
                        app_id=app_id, session_id=session_id, user_id=uid,
                        op_id=(call_id or op_id or f"shell-{name}"),
                        op_type=_OT.TOOL, op_state=_OS.COMPLETED,
                        op_parent_id=(call_id or op_id) if call_id else None,
                        correlation_id=correlation_id or "",
                        payload={
                            "stdout": _stdout[:2000], "stderr": _stderr[:500],
                            "op_parent_id": call_id or op_id,
                        },
                    ))
            elif action in _AGENT_ACTIONS:
                # Build structured agent_event from tool result
                _agent_data: dict[str, Any] = {"action": action, "name": name}
                if isinstance(result_data, dict):
                    # For wait/wait_all: forward results + completed_agents
                    if "results" in result_data:
                        _agent_data["action"] = "agent_wait_all"
                        _agent_data["completed_agents"] = [
                            {"agent_id": r.get("agent_id", ""), "status": r.get("status", "")}
                            for r in result_data.get("results", [])
                        ]
                    # For spawn: forward agent_id, specialist, task
                    if "agent_id" in result_data:
                        _agent_data["agent_id"] = result_data["agent_id"]
                    if "specialist" in result_data:
                        _agent_data["specialist"] = result_data["specialist"]
                    if "task" in result_data:
                        _agent_data["task"] = str(result_data["task"])[:200]
                    if "status" in result_data:
                        _agent_data["status"] = result_data["status"]
                    _agent_data["result"] = result_data
                from digitorn.core.events.envelope import (
                    SessionEvent, OpType, OpState, gen_op_id,
                )
                agent_id_here = (
                    _agent_data.get("agent_id")
                    or gen_op_id("agent")
                )
                _status = _agent_data.get("status", "")
                _op_state = {
                    "spawned": OpState.RUNNING,
                    "running": OpState.RUNNING,
                    "completed": OpState.COMPLETED,
                    "failed": OpState.FAILED,
                    "cancelled": OpState.CANCELLED,
                    "timeout": OpState.TIMEOUT,
                }.get(_status, OpState.RUNNING)
                _agent_data["op_id"] = agent_id_here
                await self.event_bus.emit(SessionEvent.build(
                    type="agent_event",
                    app_id=app_id,
                    session_id=session_id,
                    user_id=uid,
                    op_id=agent_id_here,
                    op_type=OpType.AGENT,
                    op_state=_op_state,
                    correlation_id=correlation_id or "",
                    payload=_agent_data,
                ))

            if on_tool_call is not None:
                await on_tool_call(name, params, result, call_id)

            # Log to persistent event log
            _log_event("tool_call", {
                "name": name, "label": label, "detail": detail,
                "params": params, "success": ok, "error": err,
            })

            # Persist after every tool call so reconnecting clients see everything; per-turn delta keeps it O(M) per turn.
            _store = self._session_store
            _msgs = session.messages
            _uid = session.user_id
            _elog = _event_log
            if 0 <= _messages_baseline <= len(_msgs):
                _delta = _msgs[_messages_baseline:]
                _msgs_op = asyncio.to_thread(
                    _store.save_turn_messages, app_id, session_id, _turn_index,
                    _delta, user_id=_uid,
                )
            else:
                _msgs_op = asyncio.to_thread(
                    _store.save_messages, app_id, session_id, _msgs, user_id=_uid,
                )
            _tc_results = await asyncio.gather(
                _msgs_op,
                asyncio.to_thread(
                    _store.save_turn_events, app_id, session_id, _turn_index, _elog, user_id=_uid,
                ),
                return_exceptions=True,
            )
            for _r in _tc_results:
                if isinstance(_r, BaseException):
                    logger.warning(
                        "Failed to persist messages for %s/%s: %s",
                        app_id, session_id, _r,
                    )

        async def _on_tool_start_bus(name: str, params: dict, call_id: str = "") -> None:
            from digitorn.core.cli.ui import _tool_label
            from digitorn.core.events.envelope import (
                SessionEvent, OpType, OpState, gen_op_id,
            )
            self.turn_state_update(
                app_id, session_id,
                phase="tool_use", tool_calls_delta=1,
            )
            label, detail = _tool_label(name, params)
            display = _resolve_tool_display(deployed, name, params)
            op_id = call_id or gen_op_id("tool")
            # Snapshot the params dict before execute_tool pops `intent`, so the persisted event log keeps the original.
            params_snapshot = dict(params) if isinstance(params, dict) else params
            await self.event_bus.emit(SessionEvent.build(
                type="tool_start",
                app_id=app_id,
                session_id=session_id,
                user_id=uid,
                op_id=op_id,
                op_type=OpType.TOOL,
                op_state=OpState.RUNNING,
                correlation_id=correlation_id or "",
                op_parent_id=None,
                payload={
                    "id": op_id,          # legacy alias - old clients
                    "call_id": call_id,   # legacy alias
                    "name": name,
                    "params": params_snapshot,
                    "label": label,
                    "detail": detail,
                    "display": display,
                    "correlation_id": correlation_id or None,
                },
            ))
            _log_event("tool_start", {"name": name, "label": label, "detail": detail, "params": params_snapshot})
            if on_tool_start is not None:
                await on_tool_start(name, params, call_id)

        async def _on_thinking_bus(text: str, count: int = 0) -> None:
            if not text or not text.strip():
                return
            stripped = text.strip()
            # Filter short narrations that just describe tool calls; ToolCallGroup already shows that info.
            lines = stripped.split("\n")
            if len(lines) <= 2 and len(stripped) < 80:
                _log_event("thinking_filtered", {"text": stripped})
                return
            from digitorn.core.events.envelope import (
                SessionEvent as _SE, OpType as _OT, OpState as _OS,
            )
            _turn_op_id = correlation_id or f"turn-{session_id}"

            def _turn_event(ev_type: str, state: _OS, payload: dict) -> _SE:
                return _SE.build(
                    type=ev_type,
                    app_id=app_id, session_id=session_id, user_id=uid,
                    op_id=_turn_op_id, op_type=_OT.TURN, op_state=state,
                    correlation_id=correlation_id or "",
                    payload=payload,
                )

            payload: dict[str, Any] = {"text": stripped}
            if count > 0:
                payload["count"] = count
            await self.event_bus.emit(_turn_event(
                "thinking", _OS.RUNNING, payload,
            ))
            _log_event("thinking", payload)
            if on_thinking is not None:
                await on_thinking(stripped)

        async def _on_thinking_started_bus() -> None:
            self.turn_state_update(app_id, session_id, phase="thinking")
            from digitorn.core.events.envelope import (
                SessionEvent as _SE, OpType as _OT, OpState as _OS,
            )
            _turn_op_id = correlation_id or f"turn-{session_id}"
            await self.event_bus.emit(_SE.build(
                type="thinking_started", app_id=app_id, session_id=session_id,
                user_id=uid, op_id=_turn_op_id, op_type=_OT.TURN,
                op_state=_OS.RUNNING, correlation_id=correlation_id or "",
            ))
            if on_thinking_started is not None:
                await on_thinking_started()

        async def _on_thinking_delta_bus(delta: str, count: int = 0) -> None:
            from digitorn.core.events.envelope import (
                SessionEvent as _SE, OpType as _OT, OpState as _OS,
            )
            _turn_op_id = correlation_id or f"turn-{session_id}"
            payload: dict[str, Any] = {"delta": delta}
            if count > 0:
                payload["count"] = count
            await self.event_bus.emit(_SE.build(
                type="thinking_delta", app_id=app_id, session_id=session_id,
                user_id=uid, op_id=_turn_op_id, op_type=_OT.TURN,
                op_state=_OS.RUNNING, correlation_id=correlation_id or "",
                payload=payload,
            ))
            if on_thinking_delta is not None:
                await on_thinking_delta(delta)

        _stream_chunks: list[str] = []

        def _emit_turn_bg(ev_type: str, state, payload: dict) -> None:
            """Fire-and-forget turn-scoped emission from sync callbacks."""
            from digitorn.core.events.envelope import (
                SessionEvent as _SE, OpType as _OT,
            )
            _turn_op_id = correlation_id or f"turn-{session_id}"
            try:
                _loop = asyncio.get_running_loop()
                _loop.create_task(self.event_bus.emit(_SE.build(
                    type=ev_type, app_id=app_id, session_id=session_id,
                    user_id=uid, op_id=_turn_op_id, op_type=_OT.TURN,
                    op_state=state, correlation_id=correlation_id or "",
                    payload=payload,
                )))
            except RuntimeError:
                pass

        def _on_token_bus(delta: str, count: int = 0) -> None:
            from digitorn.core.events.envelope import OpState as _OS
            _stream_chunks.append(delta)
            self.turn_state_update(
                app_id, session_id, phase="generating",
            )
            payload = {"delta": delta}
            if count > 0:
                payload["count"] = count
            _emit_turn_bg("token", _OS.RUNNING, payload)
            if on_token is not None:
                if asyncio.iscoroutinefunction(on_token):
                    try:
                        loop = asyncio.get_running_loop()
                        loop.create_task(on_token(delta))
                    except RuntimeError:
                        pass
                else:
                    on_token(delta)

        def _track_out_token(count: int) -> None:
            from digitorn.core.events.envelope import OpState as _OS
            _out_token_total[0] += count
            self.turn_state_update(
                app_id, session_id, tokens_out_delta=count,
            )
            _emit_turn_bg("out_token", _OS.RUNNING, {"count": count})
            if on_out_token is not None:
                on_out_token(count)

        def _track_in_token(count: int) -> None:
            from digitorn.core.events.envelope import OpState as _OS
            _in_token_total[0] += count
            self.turn_state_update(
                app_id, session_id, tokens_in_delta=count,
            )
            _emit_turn_bg("in_token", _OS.RUNNING, {"count": count})
            if on_in_token is not None:
                on_in_token(count)

        def _on_tool_call_streaming(call_id: str, name: str, count: int, intent: str = "") -> None:
            """Live progress while the LLM composes a tool call's args."""
            from digitorn.core.events.envelope import OpState as _OS
            from digitorn.core.runtime.tool_display import build_display
            try:
                if build_display(name, None, None).get("hidden"):
                    return
            except Exception as exc:
                logger.debug("tool_display build for streaming failed name=%s: %s", name, exc)
            payload: dict[str, Any] = {"call_id": call_id, "name": name}
            if count > 0:
                payload["count"] = count
            if intent:
                payload["intent"] = intent
            _emit_turn_bg(
                "tool_call_streaming", _OS.RUNNING, payload,
            )

        def _on_status_bus(phase: str, details: dict | None = None) -> None:
            from digitorn.core.events.envelope import OpState as _OS
            _emit_turn_bg(
                "status", _OS.RUNNING, {"phase": phase, **(details or {})},
            )
            _log_event("status", {"phase": phase, **(details or {})})
            if on_status is not None:
                on_status(phase, details)

        def _on_stream_done_bus() -> None:
            from digitorn.core.events.envelope import OpState as _OS
            # stream_done ends LLM streaming but not the turn; keep RUNNING so reconnecting clients still see it active.
            _emit_turn_bg("stream_done", _OS.RUNNING, {})
            _log_event("stream_done", {})
            if on_stream_done is not None:
                on_stream_done()

        async def _on_hook_event(hook_event: Any) -> None:
            from digitorn.core.events.envelope import (
                SessionEvent as _SE, OpType as _OT, OpState as _OS, gen_op_id,
            )
            hook_data = {
                "hook_id": hook_event.hook_id,
                "action_type": hook_event.action_type,
                "phase": hook_event.phase,
                "details": hook_event.details,
            }
            # Every hook fire is terminal with a fresh op_id; stable hook_id stays in payload for grouping.
            phase = (hook_event.phase or "").lower()
            if phase in ("failed", "error"):
                op_state = _OS.FAILED
            elif phase in ("cancelled",):
                op_state = _OS.CANCELLED
            else:
                op_state = _OS.COMPLETED
            hook_op_id = gen_op_id("hook")
            hook_data["hook_op_id"] = hook_op_id
            await self.event_bus.emit(_SE.build(
                type="hook", app_id=app_id, session_id=session_id,
                user_id=uid, op_id=hook_op_id, op_type=_OT.SYSTEM,
                op_state=op_state, correlation_id=correlation_id or "",
                payload=hook_data,
            ))
            _log_event("hook", hook_data)
            if on_hook_event is not None:
                await on_hook_event(hook_event)

        hook_runner = deployed.hook_runner

        _had_hook_cb = False
        if hook_runner is not None:
            _prev_hook_cb = hook_runner.on_hook_event
            hook_runner.on_hook_event = _on_hook_event
            _had_hook_cb = True

        _turn_error = None
        _aborted = False
        active_key = f"{app_id}:{session_id}"
        # Expose live session + store to ctx so runtime helpers can update persisted fields without extra plumbing.
        ctx.session = session  # type: ignore[attr-defined]
        ctx.session_store = self._session_store  # type: ignore[attr-defined]
        # Stamp ctx.effective_turn so the agent loop's mode block + tool guard read this turn's mode selection.
        try:
            _effective = resolve_mode(deployed.compiled, mode_id)
            ctx.effective_turn = _effective  # type: ignore[attr-defined]
        except Exception as _exc:
            logger.debug("resolve_mode_failed: %s", _exc)
        _eff = getattr(ctx, "effective_turn", None)
        _eff_max_turns = (
            getattr(_eff, "max_turns", None)
            or deployed.compiled.execution.max_turns
        )
        _eff_timeout = (
            getattr(_eff, "timeout", None)
            or deployed.compiled.execution.timeout
        )
        try:
            _turn_coro = agent_turn(
                ctx,
                session.messages,
                max_turns=_eff_max_turns,
                timeout=_eff_timeout,
                on_tool_call=_on_tool_call,
                on_tool_start=_on_tool_start_bus,
                on_tool_call_streaming=_on_tool_call_streaming,
                on_thinking=_on_thinking_bus,
                on_thinking_started=_on_thinking_started_bus,
                on_thinking_delta=_on_thinking_delta_bus,
                hook_runner=hook_runner,
                on_token=_on_token_bus,
                on_stream_done=_on_stream_done_bus,
                on_status=_on_status_bus,
                on_out_token=_track_out_token,
                on_in_token=_track_in_token,
            )
            _task = asyncio.current_task()
            if _task is not None:
                self._session_tasks[active_key] = _task
            result = await _turn_coro
        except asyncio.CancelledError:
            _aborted = True
            result = TurnResult(content="[Interrupted by user]", error="aborted")
        except Exception as exc:
            _turn_error = exc
            result = TurnResult(content="", error=str(exc))
        finally:
            try:
                self._session_tasks.pop(active_key, None)
            except Exception:
                logger.debug("session_task_pop_failed", exc_info=True)
            # Persist event log even if turn crashed - partial replay > nothing
            try:
                if _event_log:
                    await asyncio.to_thread(
                        self._session_store.save_turn_events,
                        app_id, session_id, _turn_index, _event_log, user_id=session.user_id,
                    )
            except Exception:
                logger.warning("failed to persist event log on error for %s/%s", app_id, session_id)
            # Remove bus capture handler (safety net for early returns/crashes)
            try:
                self.event_bus.remove_handler(_bus_capture)
            except Exception as exc:
                logger.debug("event_bus remove_handler failed: %s", exc)
            try:
                if _had_hook_cb and hook_runner is not None:
                    hook_runner.on_hook_event = _prev_hook_cb
            except Exception:
                logger.debug("hook_callback_restore_failed", exc_info=True)
            # Mark session as interrupted if turn failed or was aborted
            # - enables smart resume (orphaned tool_calls get synthetic results)
            if _aborted or _turn_error or (result and result.error):
                try:
                    session.interrupted = True
                    session.interrupted_at = time.time()
                except Exception:
                    logger.debug("session_interrupt_flag_failed", exc_info=True)
                try:
                    await asyncio.to_thread(self._session_store.put, session)
                except Exception:
                    logger.warning("failed to persist interrupted session %s (put)", session_id)
                try:
                    await asyncio.to_thread(
                        self._session_store.save_messages,
                        app_id, session_id, session.messages, user_id=session.user_id,
                    )
                except Exception:
                    logger.warning("failed to persist interrupted session %s (messages)", session_id)

        if _stream_chunks:
            _log_event("stream_text", {"content": "".join(_stream_chunks)})
        if _out_token_total[0] or _in_token_total[0]:
            _log_event("token_count", {
                "out_tokens": _out_token_total[0],
                "in_tokens": _in_token_total[0],
            })
        try:
            _rp = int(getattr(result, "prompt_tokens", 0) or 0)
            _rc = int(getattr(result, "completion_tokens", 0) or 0)
            if (_in_token_total[0], _out_token_total[0]) != (_rp, _rc):
                logger.warning(
                    "token_stream_mismatch app=%s sid=%s "
                    "in_stream=%d vs result.prompt=%d  "
                    "out_stream=%d vs result.completion=%d",
                    app_id, session_id,
                    _in_token_total[0], _rp,
                    _out_token_total[0], _rc,
                )
        except Exception as exc:
            logger.debug("token mismatch check failed: %s", exc)
        _log_event("turn_end", {
            "content": result.content,
            "tool_calls_count": result.tool_calls_count,
            "turns_used": result.turns_used,
            "truncated": result.truncated,
            "error": result.error,
        })

        # Remove the bus capture handler - prevents cross-turn leakage
        try:
            self.event_bus.remove_handler(_bus_capture)
        except Exception as exc:
            logger.debug("_chat best-effort block failed: %s", exc)

        if result.content:
            # agent_loop already appended the assistant row; this guard prevents a double-append on every successful turn.
            _msgs = session.messages
            _last = _msgs[-1] if _msgs else None
            _already_there = (
                isinstance(_last, dict)
                and _last.get("role") == "assistant"
                and _last.get("content") == result.content
            )
            if not _already_there:
                session.add_assistant(result.content)

        _mem = ctx.memory_module
        if _mem and hasattr(_mem, 'store') and _mem.store:
            try:
                session.memory_snapshot = _mem.store.to_dict()
            except Exception as exc:
                logger.debug("memory snapshot to_dict failed: %s", exc)

        # Cheap session.put under the lock for an atomic snapshot; heavy persists dispatched on snapshot copies so lock release stays fast.
        session.turn_count += 1
        if not _aborted:
            session.interrupted = False  # Successful turn clears interruption flag

        _store = self._session_store
        _uid = session.user_id
        _snap_messages = list(session.messages)
        _snap_events = list(_event_log)

        _end_t0 = time.monotonic()

        try:
            await asyncio.to_thread(_store.put, session)
        except Exception as persist_exc:
            logger.warning("session_put_failed: %s", persist_exc)
        _t_put = time.monotonic() - _end_t0

        _schedule_bg_persist_msgs_events(
            _store, app_id, session_id, _turn_index,
            _snap_messages, _snap_events, _uid,
            messages_baseline=_messages_baseline,
        )

        # Build rich result event with usage/cost/context for all SSE clients
        result_event_data: dict[str, Any] = {
            "content": result.content,
            "session_id": session_id,
            "tool_calls_count": result.tool_calls_count,
            "turns_used": result.turns_used,
            "truncated": result.truncated,
            "error": result.error,
        }

        # Usage: token counts + cost estimate
        result_event_data["usage"] = {
            "input_tokens": result.prompt_tokens,
            "output_tokens": result.completion_tokens,
        }
        try:
            from digitorn.core.runtime.session_metrics import get_session_metrics
            sm = get_session_metrics(app_id, session_id)
            result_event_data["usage"]["total_input_tokens"] = sm.prompt_tokens
            result_event_data["usage"]["total_output_tokens"] = sm.completion_tokens
            result_event_data["usage"]["total_tokens"] = sm.total_tokens
            result_event_data["turn_number"] = sm.turn
            _model = sm.model or (getattr(ctx.provider, "model", "") if ctx else "")
            if _model:
                result_event_data["model"] = _model
            _ml = _model.lower()
            if "opus" in _ml:
                _pi, _po = 15.0, 75.0
            elif "sonnet" in _ml:
                _pi, _po = 3.0, 15.0
            else:
                _pi, _po = 0.80, 4.0
            result_event_data["usage"]["cost_usd"] = round(
                sm.prompt_tokens * _pi / 1_000_000 + sm.completion_tokens * _po / 1_000_000, 6
            )
            result_event_data["context"] = sm.context.snapshot()
        except Exception as exc:
            logger.debug("session_metrics enrichment failed: %s", exc)

        _t_ws_start = time.monotonic()
        try:
            _ws = workspace or ""
            if _ws:
                from digitorn.core.api.apps_v2 import _get_workspace_status
                result_event_data["workspace_status"] = await asyncio.to_thread(
                    _get_workspace_status, _ws,
                )
        except Exception as exc:
            logger.debug("workspace_status enrichment failed: %s", exc)
        _t_ws = time.monotonic() - _t_ws_start

        _t_emit_start = time.monotonic()
        if not _aborted:
            from digitorn.core.events.envelope import (
                SessionEvent as _SE, OpType as _OT, OpState as _OS,
            )
            _turn_op_id = correlation_id or f"turn-{session_id}"
            _result_envelope = _SE.build(
                type="result",
                app_id=app_id, session_id=session_id, user_id=uid,
                op_id=_turn_op_id, op_type=_OT.TURN, op_state=_OS.COMPLETED,
                correlation_id=correlation_id or "",
                payload=result_event_data,
            )
            # Fire-and-forget emit; Socket.IO under slow clients can take seconds and would hold the session lock.
            try:
                _emit_task = asyncio.create_task(
                    self.event_bus.emit(_result_envelope),
                    name=f"end_of_turn_emit:{session_id}:{_turn_op_id}",
                )
                _END_OF_TURN_EMIT_TASKS.add(_emit_task)
                _emit_task.add_done_callback(
                    _END_OF_TURN_EMIT_TASKS.discard,
                )
            except RuntimeError:
                # Loop closed (shutdown); fall back to awaiting so the result event isn't dropped during graceful close.
                await self.event_bus.emit(_result_envelope)
        _t_emit = time.monotonic() - _t_emit_start

        # Usage tracking is owned by the digitorn LLM gateway.
        # The daemon does not record token/cost rows anymore.

        _t_total = time.monotonic() - _end_t0
        if _t_total > 1.0:
            logger.warning(
                "end_of_turn_slow app=%s sid=%s total=%.2fs "
                "put=%.2fs ws=%.2fs emit=%.2fs",
                app_id, session_id, _t_total, _t_put, _t_ws, _t_emit,
            )

        # Emit a dedicated error event so clients can display it prominently.
        # The result event also has error, but clients may not check it.
        if result.error and result.error != "aborted":
            from digitorn.core.api.apps_v2 import _classify_error
            from digitorn.core.events.envelope import (
                SessionEvent as _SE, OpType as _OT, OpState as _OS,
            )
            try:
                error_data = _classify_error(
                    _turn_error if _turn_error else RuntimeError(result.error)
                )
                error_data["session_id"] = session_id
                # Persist on the canonical SessionState so poll-based clients
                # (dev CLI, plain REST) surface it via summary(). SSE clients
                # get the live event emitted just below.
                try:
                    _st_err = self._session_store._store.state(session_id)
                    if _st_err is not None:
                        _st_err.last_error = error_data
                except Exception:
                    logger.debug("persist last_error failed", exc_info=True)
                try:
                    from digitorn.core.runtime.session_metrics import (
                        get_session_metrics,
                    )
                    get_session_metrics(app_id, session_id).record_error(
                        str(error_data.get("error")
                            or error_data.get("detail")
                            or error_data.get("code")
                            or "error")
                    )
                except Exception:
                    logger.debug("record_error failed", exc_info=True)
                _turn_op_id = correlation_id or f"turn-{session_id}"
                await self.event_bus.emit(_SE.build(
                    type="error",
                    app_id=app_id, session_id=session_id, user_id=uid,
                    op_id=_turn_op_id, op_type=_OT.TURN, op_state=_OS.FAILED,
                    correlation_id=correlation_id or "",
                    payload=error_data,
                ))
            except Exception:
                pass  # Don't crash if error classification fails

        return result

    async def check_notifications(
        self,
        app_id: str,
        session_id: str,
        *,
        user_id: str = "local",
        on_tool_call: Any | None = None,
        on_hook_event: Any | None = None,
    ) -> TurnResult | None:
        """Drain background notifications and run an agent turn if any exist."""
        from digitorn.core.runtime.agent_loop import agent_turn

        deployed = self._get_deployed(app_id, user_id=user_id)
        cb = deployed.context_builder
        if cb is None or not hasattr(cb, "drain_bg_notifications"):
            return None

        notifications = cb.drain_bg_notifications(session_id=session_id)

        # Off-loop drain; the JSONL buffer can grow large during cron storms and stall every turn if read on-loop.
        buffered = await asyncio.to_thread(
            self._job_store.drain_buffered, app_id,
        )
        if buffered:
            notifications.extend(buffered)

        if not notifications:
            return None

        session = await asyncio.to_thread(self._session_store.get, app_id, session_id)
        if session is None:
            return None

        from digitorn.core.runtime.agent_loop import (
            _format_bg_task_notification,
            _format_watcher_notification,
        )

        for notif in notifications:
            if notif.get("type") == "watcher":
                text = _format_watcher_notification(notif)
            else:
                text = _format_bg_task_notification(notif)

            session.messages.append({"role": "system", "content": text})

        logger.info(
            "Background notification check: %d task(s), triggering agent turn",
            len(notifications),
        )

        bus_key = self.event_bus.session_key(app_id, session_id, user_id)

        for notif in notifications:
            from digitorn.core.events.envelope import (
                SessionEvent as _SE, OpType as _OT, OpState as _OS, gen_op_id,
            )
            _task_id = notif.get("task_id") or gen_op_id("bg")
            await self.event_bus.emit(_SE.build(
                type="notification",
                app_id=app_id, session_id=session_id, user_id=user_id,
                op_id=_task_id, op_type=_OT.TOOL, op_state=_OS.RUNNING,
                payload=notif,
            ))

        async def _on_tool_call(name: str, params: dict, result_val: Any, call_id: str = "") -> None:
            from digitorn.core.events.envelope import (
                SessionEvent, OpType, OpState, gen_op_id,
            )
            ok, err = True, ""
            if isinstance(result_val, dict):
                ok = result_val.get("success", True)
                err = result_val.get("error", "")
            elif hasattr(result_val, "success"):
                ok = result_val.success
                err = getattr(result_val, "error", "") or ""
            op_id = call_id or gen_op_id("tool")
            await self.event_bus.emit(SessionEvent.build(
                type="tool_call",
                app_id=app_id,
                session_id=session_id,
                user_id=user_id,
                op_id=op_id,
                op_type=OpType.TOOL,
                op_state=OpState.FAILED if not ok else OpState.COMPLETED,
                payload={
                    "id": op_id,
                    "call_id": call_id,
                    "name": name, "params": params,
                    "success": ok, "error": err,
                },
            ))
            if on_tool_call is not None:
                await on_tool_call(name, params, result_val, call_id)

        from digitorn.core.runtime.types import apply_workspace_override

        import copy
        ctx = copy.copy(deployed.entry_context)
        ctx.session_id = session_id
        yaml_ws = getattr(deployed.compiled.execution, "workspace", "")
        ws = yaml_ws or str(Path.cwd())
        apply_workspace_override(ctx, ws, yaml_ws)
        hook_runner = deployed.hook_runner

        _had_hook_cb = False
        if hook_runner is not None and on_hook_event is not None:
            _prev_hook_cb = hook_runner.on_hook_event
            hook_runner.on_hook_event = on_hook_event
            _had_hook_cb = True

        _eff = getattr(ctx, "effective_turn", None)
        _eff_max_turns = (
            getattr(_eff, "max_turns", None)
            or deployed.compiled.execution.max_turns
        )
        _eff_timeout = (
            getattr(_eff, "timeout", None)
            or deployed.compiled.execution.timeout
        )
        try:
            result = await agent_turn(
                ctx,
                session.messages,
                max_turns=_eff_max_turns,
                timeout=_eff_timeout,
                on_tool_call=_on_tool_call,
                hook_runner=hook_runner,
            )
        finally:
            if _had_hook_cb and hook_runner is not None:
                hook_runner.on_hook_event = _prev_hook_cb

        if result.content:
            # agent_loop's no-tool-calls exit already appends the assistant message; this guard avoids a double-append.
            _msgs = session.messages
            _last = _msgs[-1] if _msgs else None
            _already_there = (
                isinstance(_last, dict)
                and _last.get("role") == "assistant"
                and _last.get("content") == result.content
            )
            if not _already_there:
                session.add_assistant(result.content)

        await asyncio.to_thread(self._session_store.put, session)

        from digitorn.core.events.envelope import (
            SessionEvent as _SE, OpType as _OT, OpState as _OS,
        )
        await self.event_bus.emit(_SE.build(
            type="notification_result",
            app_id=app_id, session_id=session_id, user_id=user_id,
            op_id=f"notif-batch-{session_id}",
            op_type=_OT.SYSTEM,
            op_state=_OS.FAILED if result.error else _OS.COMPLETED,
            payload={
                "content": result.content,
                "session_id": session_id,
                "notifications_count": len(notifications),
                "tool_calls_count": result.tool_calls_count,
                "error": result.error,
            },
        ))

        return result
