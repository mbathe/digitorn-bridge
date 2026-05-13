"""_ChatMixin - conversation turn execution.

The big one - owns ``chat`` / ``_chat_locked`` / ``check_notifications``
/ ``_register_wake_handler``. Method bodies copied verbatim from the
original manager.
"""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import Any

from digitorn.core.app.sessions import ConversationSession
from digitorn.core.runtime.types import TurnResult

from ._models import _resolve_tool_display, _recover_interrupted_session

logger = logging.getLogger(__name__)


# Module-level set holding fire-and-forget session_store persist
# tasks. asyncio.create_task returns a weak ref - if we don't hold a
# strong ref, the task may be GC'd before it runs and the persist is
# silently lost. The done callback removes the task once it completes.
# Bounded to avoid runaway memory under sustained backpressure.
_BG_SESSION_PERSIST_TASKS: set[asyncio.Task] = set()
_BG_SESSION_PERSIST_MAX = 1000

# Strong-ref set for the fire-and-forget end-of-turn result emit
# (Socket.IO emit can take 1-5s under Windows IOCP backpressure on a
# slow client; awaiting it held the session lock and delayed the next
# user message by the same duration). Without this set, ``create_task``
# returns a Task the loop only weakly tracks -- GC can collect it
# mid-flight, silently dropping the ``result`` event.
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
    """Fire-and-forget the heavy session_store persists.

    The two callers (per-tool-call hook and end-of-turn) used to
    ``await asyncio.gather(...)`` these ops while the per-session
    lock was held - end result was the user observing "the next
    message doesn't go through" because the lock release was waiting
    on multi-second ``append_events`` writes (O(N²) load+save of the
    whole event log).

    Scheduling pattern:
      * snapshot lists are passed by value (caller already copied),
        so concurrent mutation by the next turn is impossible
      * the two store ops run in parallel via ``gather``
      * the task is detached - control returns immediately to the
        caller, lock releases as soon as ``_chat_locked`` returns
      * the task is held in a module-level set so it isn't GC'd
        before completion; the done callback evicts it
      * when ``in_flight >= _BG_SESSION_PERSIST_MAX`` we WARN and
        drop the new task. The DB layer (``_persist_turn_bg``) is
        unaffected - it has its own isolated worker - so durable
        state survives. Only the hot session_store cache is missed.
    """
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

    # Pick the messages persist mode:
    # - delta path (O(1) per turn) when we have a valid baseline AND
    #   the new list is at least as long as it was at turn start
    #   (i.e. compaction did NOT shrink it during this turn).
    # - full-blob fallback (O(N) per turn) for compaction case OR when
    #   the caller didn't pass a baseline. ``save_messages`` also clears
    #   stale per-turn keys so the next ``load_messages`` doesn't merge
    #   the legacy blob WITH the deltas.
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
                # Per-turn key (O(1) write) instead of ``append_events``
                # which loaded + extended + re-saved the WHOLE event log
                # (O(N²) cumulative over a session). The read path
                # ``load_events`` already aggregates per-turn keys, so
                # this is transparent to readers - same interface,
                # constant time per write.
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
    ) -> TurnResult:
        """Process a single conversation message within a session.

        Creates the session on first call. Maintains message history
        across calls with the same session_id.

        Events are also published to the session event bus for any
        persistent SSE subscribers (SDK clients).

        Args:
            app_id: The deployed app's ID.
            session_id: Unique session identifier (client-generated).
            message: User message text.
            on_tool_call: Optional callback for tool call display.
            on_tool_start: Optional callback before tool execution.
            on_thinking: Optional callback for agent reasoning text.

        Returns:
            TurnResult with the agent's response.
        """
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
            # ``workspace_mode: auto`` - default to a per-session isolated
            # dir under ``~/.digitorn/workspaces/<app_id>/<sid>/`` when the
            # caller provides nothing. Falling back to ``Path.cwd()`` used
            # to silently dump every agent write into whatever directory
            # the daemon was launched from (usually the repo root), and
            # aliased every session to the same dir so the per-session
            # preview snapshot was never persisted. The isolated default
            # matches the layout that ``WorkspaceModule._resolve_sync_dir``
            # and ``hydrate_files_from_disk`` expect.
            per_session_default = str(
                Path.home() / ".digitorn" / "workspaces" / app_id / session_id
            )
            # Reject a ``_persisted_ws`` that equals the daemon's current
            # working directory - that's the stale value baked in by the
            # pre-fix code path for any session created before the
            # per-session default was introduced. Without this guard the
            # agent keeps seeing the daemon's cwd (typically the repo
            # root) as its workspace for the rest of the session's life.
            daemon_cwd = str(Path.cwd().resolve())
            if _persisted_ws:
                try:
                    if str(Path(_persisted_ws).resolve()) == daemon_cwd:
                        _persisted_ws = ""
                except Exception:
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
        # Detect the daemon's per-session auto-workspace shape:
        # ``~/.digitorn/workspaces/<app_id>/<session_id>/``. When the
        # workspace path is already scoped to one (app, session), tell
        # the layout to FLATTEN — otherwise it nests
        # ``apps/<app_id>/sessions/<sid>/`` redundantly inside the
        # workspace's ``.digitorn/``, doubling the app_id and sid in
        # the path tree for no reason.
        _per_session_ws = False
        if ws:
            try:
                _ws_path = Path(ws).resolve()
                _per_session_ws = (
                    _ws_path.name == session_id
                    and _ws_path.parent.name == app_id
                )
            except Exception:
                _per_session_ws = False
        layout = WorkspaceLayout(ws, app_id, per_session=_per_session_ws)
        layout.ensure_session_dirs(session_id)

        fs_mod = deployed.modules.get("filesystem")
        if fs_mod and hasattr(fs_mod, "_checkpoint_dir"):
            fs_mod._checkpoint_dir = str(layout.session_checkpoints_dir(session_id))

        # Serialize concurrent access to the same session.
        # CRITICAL: the lock MUST be held during _chat_locked execution AND
        # all session persistence (put, save_messages, append_events) which
        # happens INSIDE _chat_locked. The lock is only released after
        # _chat_locked fully returns - never split persistence across the lock.
        session_lock = self._session_store.session_lock(app_id, session_id, uid)
        active_key = f"{app_id}:{session_id}"
        self._active_sessions.add(active_key)
        lock_acquired = False
        try:
            try:
                # Lock wait timeout. The previous 300 s default was
                # calibrated to ``agent_turn`` budget which is wrong:
                # the agent_turn timeout protects ONE turn from
                # running too long; the lock timeout protects a
                # NEW message from blocking too long when a previous
                # turn is in progress. The Phase 3 message queue at
                # the API layer (``apps_v2/messages.py``) already
                # handles "session busy" gracefully (enqueues + emits
                # ``message_queued`` SSE), so we just need a short
                # safety net here for the rare case where a message
                # bypassed the queue (legacy wait mode, internal
                # dispatch). 30 s is plenty for a turn that's about
                # to finish; beyond that we surface ``session_busy``
                # so the caller can retry or queue.
                try:
                    from digitorn.core.config import get_settings
                    _lock_timeout = get_settings().session.lock_timeout
                except Exception:
                    _lock_timeout = 30.0
                # Safety net: clamp ridiculous overrides. The lock is
                # meant for serialisation, not for waiting out a
                # whole turn budget.
                _lock_timeout = min(max(float(_lock_timeout), 5.0), 60.0)
                await asyncio.wait_for(
                    session_lock.acquire(), timeout=_lock_timeout,
                )
                lock_acquired = True
            except asyncio.TimeoutError:
                # Error message is matched by the HTTP error
                # classifier (apps_v2/_shared.py:_classify_error)
                # to map to ``code=session_busy`` (HTTP 409) with a
                # clean user-facing message. Keep the substring
                # ``session lock`` in sync with that classifier.
                raise RuntimeError(
                    f"Session lock timeout after {_lock_timeout:.0f}s "
                    f"for {app_id}/{session_id} - another turn is "
                    f"still in progress; retry or use the message queue."
                )
            # All session state mutations happen inside _chat_locked under
            # the acquired lock. No work after this call should touch the
            # session store for the same session_id.
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
            # TurnState cleanup - cancels the heartbeat task and frees
            # the entry so the next ``/state`` query correctly reports
            # ``turn: null``. Wrapped - cleanup must never raise.
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
    ) -> "TurnResult":
        """Inner chat logic, called under per-session lock."""
        from digitorn.core.runtime.agent_loop import agent_turn

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

        # Update workspace on the session if it was missing (e.g. old sessions)
        if workspace and not session.workspace:
            session.workspace = workspace
        # ``workdir`` is the agent-facing path. When the user-submitted
        # ``workspace`` (legacy field on SessionMessageRequest) is set
        # AND the session has no workdir yet, treat it as the workdir.
        # New session creation sets both correctly via the dedicated
        # POST /sessions route; this branch covers older clients that
        # still pass ``workspace=`` on each message and pre-split sessions.
        if workspace and not getattr(session, "workdir", ""):
            session.workdir = workspace

        # Defensive: strip {WORKSPACE} from the system message even for resumed
        # sessions that were persisted before substitution was applied at creation.
        from digitorn.core.runtime.types import apply_workspace_to_messages
        apply_workspace_to_messages(session.messages, workspace, yaml_ws)

        # ── Event log: capture everything for session reconstruction ──────
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

        # Capture ALL bus events (preview:*, widget:*, etc.) into the event log
        # so the client can fully replay the turn. This handler is removed at
        # turn end to avoid leaking between turns.
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
            except Exception:
                pass

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

        # ── Smart resume: if session was interrupted, recover interrupted work ──
        if session.interrupted and session.messages:
            session.interrupted = False  # Clear flag
            _recovered = _recover_interrupted_session(session.messages)
            logger.info(
                "Session '%s' resumed after interruption (%d tool calls recovered)",
                session_id, _recovered,
            )

        # Snapshot the message count BEFORE this turn appends anything
        # (so the delta we persist at end-of-turn includes the new
        # user/reminder message + assistant response + tool results,
        # i.e. everything THIS turn produced). The end-of-turn persist
        # writes ``session.messages[_messages_baseline:]`` under a
        # per-turn key, which keeps the cumulative cost at O(1) per
        # turn. Falls back to a full blob via ``save_messages`` when
        # compaction shrinks the list (delta would be negative).
        _messages_baseline = len(session.messages)

        # Build user message - multimodal if images provided
        if image_refs:
            from digitorn.core.runtime.multimodal import build_user_message_with_images
            user_msg = build_user_message_with_images(message, image_refs)
            session.messages.append(user_msg)
            if not session.title and message:
                session.title = message[:80]
        elif reminder:
            session.messages.append({
                "role": "system",
                "content": (
                    f"[REMINDER from cron] You scheduled this earlier and it "
                    f"just fired. Take whatever action you committed to. "
                    f"Message: {message}"
                ),
            })
            session.last_active = time.time()
        else:
            session.add_user(message)
        _log_event("turn_start", {"message": message, "images": len(image_refs or [])})

        await asyncio.to_thread(self._session_store.put, session)

        from digitorn.core.runtime.types import apply_workspace_override

        import copy
        ctx = copy.copy(deployed.entry_context)
        ctx.session_id = session_id
        ctx.user_id = uid
        # Tag the context with the app_id too - without this,
        # `_get_session_metrics(ctx)` falls back to app_id="default" and
        # SessionMetrics accumulate in the wrong bucket. Downstream
        # consumers (usage_events record, list_sessions join, cost
        # calculation) then see 0 because they look up by the real
        # app_id. We set it unconditionally; the entry_context copy
        # may or may not already have it set upstream.
        ctx.app_id = app_id
        # One-turn template directive (set by ``POST /messages`` when the
        # user attached a template). Lives only on this per-turn ctx
        # copy, so the next turn (fresh ctx copy from entry_context)
        # automatically forgets it — no cleanup required.
        ctx.template_system_prompt = template_system_prompt or ""
        # Apply WORKDIR (agent-facing) to the context, not workspace.
        # The agent's tools (Read/Write/Bash, Ws*) operate inside
        # ``ctx.workspace`` - that path must be the workdir so the
        # agent never touches the daemon-private ``session.workspace``
        # under ~/.digitorn/.
        agent_workdir = (
            getattr(session, "workdir", "")
            or workspace
            or session.workspace
        )
        # Final safety net: pre-split sessions / lazy-create paths that
        # missed the workspace default end up here with an empty workdir.
        # Without a workspace, shell.bash can't resolve cwd and rejects
        # every command up-front. Materialise the canonical per-session
        # path so the agent always has somewhere to work.
        if not agent_workdir:
            from pathlib import Path as _Path
            _fallback = _Path.home() / ".digitorn" / "workspaces" / app_id / session_id
            try:
                _fallback.mkdir(parents=True, exist_ok=True)
            except Exception:
                pass
            agent_workdir = str(_fallback)
            if not session.workspace:
                session.workspace = agent_workdir
            if not getattr(session, "workdir", ""):
                session.workdir = agent_workdir
        apply_workspace_override(ctx, agent_workdir, yaml_ws)

        # ── Gateway routing (multi-user / SaaS deployment) ───────────
        # If this session has an authenticated user AND the brain
        # doesn't carry a user-managed credential, hot-swap the
        # provider to one that talks to the digitorn gateway with
        # the user's JWT. The YAML stays untouched; only ctx.provider
        # changes for the duration of this session.
        # See: digitorn.core.credentials.gateway_resolver for the
        # decision tree.
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
                # ``is_byok_enabled`` is a single-row lookup that
                # short-circuits on cloud mode + anonymous users.
                # The credential injection has already completed in
                # the dispatch layer, so by this point the deployed
                # provider is configured with the user's own key
                # (or the call would have aborted with
                # ``CredentialAuthRequired``).
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
                    # ``resolve_session_provider`` may return a fresh
                    # provider instance (gateway-routed for shared
                    # accounts, BYOK swap, etc.). The proxy installed
                    # at bootstrap time wraps the DEPLOY-time provider;
                    # the swap here would undo it -- ``ctx.provider``
                    # would become a real ``BaseLLMProvider`` again
                    # and every ``chat_stream`` would run on the
                    # daemon main loop. Re-wrap so the session provider
                    # ALSO goes through the worker's ``LLMProviderProxy``.
                    # No-op when ``llm_provider`` isn't workered or when
                    # ``resolved`` is already a proxy (idempotent).
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

                # Behavior classifier - per-session provider stash.
                # The classifier ran historically on a SHARED module
                # singleton; concurrent users would race (a swap by
                # user A leaks to user B). We resolve once per session
                # and stash on ``ctx`` - the agent_loop forwards it to
                # ``classify_turn(provider_override=...)`` so each
                # turn picks the provider matching its own
                # (user, BYOK) combination without mutating shared state.
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
                            # User explicitly opted out - keep the
                            # deploy-time provider (its api_key has
                            # been hot-swapped by inject_session_time
                            # for this caller).
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

        # Register the TurnState so the /state endpoint + state:snapshot
        # SSE event can report "a turn is running now" the instant the
        # client asks, without having to wait for the first token event.
        # The correlation_id is authoritative - comes from the POST path.
        _turn_corr_id = correlation_id or ""
        # Stash the correlation_id on the context so streaming events
        # like ``assistant_stream_snapshot`` can include it in their
        # payload. Without this, the snapshot envelope's correlation_id
        # is empty, which made the web client fall back to "find last
        # streaming bubble" - and when a snapshot raced after
        # message_done (no streaming bubble left) it created a NEW
        # bubble, doubling the assistant message visually.
        ctx._correlation_id = _turn_corr_id
        if _turn_corr_id:
            self.turn_state_begin(app_id, session_id, _turn_corr_id)
            # Start the heartbeat pulser - announces liveness every few
            # seconds so a client watchdog can distinguish "still thinking"
            # from "server stuck".
            self._start_turn_heartbeat(app_id, session_id, uid, _turn_corr_id)

        _save_counter = 0

        async def _on_tool_call(name: str, params: dict, result: Any, call_id: str = "") -> None:
            # Defense-in-depth: the agent loop occasionally emitted
            # tool_call events with an empty name when the provider's
            # streaming chunk fragmented the name mid-flight and the
            # fragment was flushed before the fqn arrived. Clients saw
            # "?" bubbles. Recover the name from params / result_data
            # where possible; last resort is "unknown" - and we log a
            # stack trace of the source so we can hunt the root cause
            # rather than silently masking it.
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
            # ── Standardize result: ALWAYS extract success + error + data ──
            # Every tool_call event sent to the client has the same shape:
            #   success: bool (always present)
            #   error: str (always present, empty if no error)
            #   result: dict (always present, tool-specific data)
            ok, err = True, ""
            result_data: Any = None
            if isinstance(result, dict):
                # Explicit success=false means failure
                if result.get("success") is False:
                    ok = False
                # Explicit error field means failure
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
            # Same op_id as the preceding ``tool_start`` - the client
            # uses it to correlate running → completed on the same
            # chip. Falls back to a generated id only if the provider
            # gave us nothing (defensive).
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
                # memory_update is a side effect of the tool call that
                # just completed - reuse the tool's op_id as parent
                # so the client can show it under the same chip.
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
                    # terminal_output belongs to the shell tool call;
                    # share its op_id so the client attaches the
                    # stdout/stderr panel to the right chip.
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
                # This path reflects the TOOL result of ``Agent(...)``
                # (dispatch/status/wait). The underlying sub-agent
                # cycle's spawn/progress/result events are emitted
                # separately by the ``_relay`` notify path above -
                # here we only carry the current dispatch snapshot so
                # clients don't need to pick between two sources.
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

            # Persist after EVERY tool call - zero data loss on crash/disconnect.
            # A client reconnecting with ?since=N gets everything.
            # Wrapped in to_thread() because the KV backend (DiskCache/SQLite)
            # uses synchronous I/O that would block the event loop. Run the
            # two writes in parallel since they target distinct keys.
            #
            # Messages: save THIS turn's delta only via ``save_turn_messages``
            # (O(M) where M is messages-so-far-this-turn) instead of the
            # full blob (O(N) cumulative). Falls back to the legacy
            # full-blob path if compaction shrunk the list mid-turn -
            # save_messages also clears stale per-turn keys to keep
            # load_messages consistent.
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
                # Save ONLY this turn's events using a turn-scoped key, so
                # previous turns are not overwritten. The full history is
                # reconstructed by load_session_events() which aggregates
                # all turn event logs.
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
            # TurnState: advance phase + bump liveness. The state
            # envelope picks this up instantly so /state sees
            # phase='tool_use' + updated tool_calls_count, and the
            # client reflects "agent is calling tool X" without
            # having to parse the event stream manually.
            self.turn_state_update(
                app_id, session_id,
                phase="tool_use", tool_calls_delta=1,
            )
            label, detail = _tool_label(name, params)
            display = _resolve_tool_display(deployed, name, params)
            # The op_id is the provider-assigned call_id (Anthropic
            # ``tool_use.id`` / OpenAI ``tool_call.id``). Falling back
            # to a fresh ``op-tool-<hex>`` preserves the contract when
            # the provider streams a call without an id (rare, seen
            # on partial chunks).
            op_id = call_id or gen_op_id("tool")
            # Snapshot the params dict so the persisted event log
            # captures every field present AT TOOL_START — including
            # the ``intent`` first-property the LLM injected for the
            # Lovable-style progressive UI. Without this copy, the
            # downstream ``execute_tool`` path runs ``params.pop("intent")``
            # on the SAME dict reference before the event bus / disk
            # journal had a chance to serialize, so a session reloaded
            # from history showed empty intents on every tool call
            # (the live frontend captured the value off the in-memory
            # SSE event just fine; only the cold-replay path saw the
            # already-stripped snapshot). Shallow copy is enough — the
            # stripped key is at the top level, no nested mutation in
            # the offending path.
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
            # Filter short narrations that just describe tool calls -
            # the ToolCallGroup already shows this info.
            lines = stripped.split("\n")
            if len(lines) <= 2 and len(stripped) < 80:
                _log_event("thinking_filtered", {"text": stripped})
                return
            # Turn-scoped helper - every event of THIS turn shares
            # op_id = correlation_id (the turn's id), op_type = TURN.
            # Centralised so the 7 emitters below don't repeat the
            # boilerplate (and can't forget a field).
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
            """Fire-and-forget turn-scoped emission from sync callbacks.
            Same contract as the async helpers, but suitable for the
            ``_on_token_bus`` / ``_track_*_token`` sync signatures.
            """
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
            # Bump liveness - token arrival is the primary signal that
            # the LLM is actually producing output (not stuck in a
            # provider retry loop).
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
            """Live progress while the LLM composes a tool call's args.
            Lets the frontend show "Write · 47 tokens" climbing in
            real-time so the user knows the agent is working through a
            big call before execution even begins.

            ``intent`` is the verb phrase extracted from the partial JSON
            buffer as soon as the schema's first property (``intent``)
            closes its string literal. Surfaced on the wire so the
            Lovable-style progress line gets the verb BEFORE tool_start
            fires — without this, intents only appear after the LLM
            finishes composing the entire call.

            Skipped for hidden tools (Memory ops, Agent spawn, search_*,
            etc.) - they don't render a card at all once execution
            starts, so a streaming placeholder would just flash a chip
            that immediately disappears and confuses the eye.
            """
            from digitorn.core.events.envelope import OpState as _OS
            from digitorn.core.runtime.tool_display import build_display
            try:
                if build_display(name, None, None).get("hidden"):
                    return
            except Exception:
                pass
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
            # stream_done marks the end of LLM streaming, not the end
            # of the turn (message_done is the terminal). Keep RUNNING
            # so a reconnecting client still sees the turn as active
            # until message_done lands.
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
            # A hook firing is a ONE-SHOT event, not a long-running
            # op. We used to reuse ``hook_event.hook_id`` as op_id
            # with op_state=RUNNING for phases that weren't explicitly
            # completed/failed - that left ``_system`` (the singleton
            # id used by built-in hooks) stuck in ``active_ops`` forever.
            # Fix: every hook event is TERMINAL on emission, and each
            # invocation gets a fresh op_id so two firings of the same
            # hook don't overwrite each other's state in the client
            # registry.
            phase = (hook_event.phase or "").lower()
            if phase in ("failed", "error"):
                op_state = _OS.FAILED
            elif phase in ("cancelled",):
                op_state = _OS.CANCELLED
            else:
                # pre / on / completed / done / success / unknown →
                # COMPLETED. The event is itself the "I happened" -
                # the client renders it as a log entry, not a chip
                # that stays alive.
                op_state = _OS.COMPLETED
            # Fresh op_id per fire. The stable hook_id is preserved in
            # the payload for clients that want to group all firings
            # of the same hook in a debug panel.
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
        # Expose the live session + store to ctx so runtime helpers
        # (e.g. title_generator) can update persisted fields without
        # plumbing a new parameter through the whole call chain.
        try:
            ctx.session = session  # type: ignore[attr-defined]
            ctx.session_store = self._session_store  # type: ignore[attr-defined]
        except Exception:
            pass
        try:
            _turn_coro = agent_turn(
                ctx,
                session.messages,
                max_turns=deployed.compiled.execution.max_turns,
                timeout=deployed.compiled.execution.timeout,
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
            # Each cleanup step is wrapped individually so a failure in one
            # never prevents the others from running. The finally block must
            # NEVER raise - leaks happen when it does.
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
            except Exception:
                pass
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

        # ── Log final events for the turn ──────────────────────────────────
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
        except Exception:
            pass
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
        except Exception:
            pass

        if result.content:
            # Idempotent guard: the agent loop now appends the final
            # assistant message to ``session.messages`` BEFORE persisting
            # so history_log gets the row. Without this guard we'd
            # double-append on every successful turn (and the LLM would
            # see two identical assistant turns in its context).
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
            except Exception:
                pass

        # ── Persist session, messages, events ────────────────────────
        # Strategy: ``_store.put(session)`` is awaited under the lock
        # because the session object itself is about to mutate when the
        # next turn starts (turn_count++, add_user, …). Pickling it
        # under the lock guarantees an atomic snapshot. It's the cheap
        # op (~10-50ms) so the lock release stays fast.
        #
        # The two heavy ops (``save_messages`` rewrites the whole
        # message blob, ``append_events`` is O(N²) load+save of the
        # full event log) are dispatched fire-and-forget on snapshot
        # COPIES of the lists. They never block the lock release, so
        # the next message from the same session can start immediately.
        #
        # Crash safety is preserved: the durable DB layer
        # (``_persist_turn_bg`` in agent_loop) writes via its own
        # isolated worker on the same end-of-turn signal, with per-row
        # incremental inserts. Even if a session_store bg task is
        # interrupted, the DB has the latest turn and the cache will
        # rehydrate from it on the next read.
        session.turn_count += 1
        if not _aborted:
            session.interrupted = False  # Successful turn clears interruption flag

        _store = self._session_store
        _uid = session.user_id
        # Snapshot the heavy lists under the lock - cheap, just copies
        # the list-of-refs. The dicts inside aren't mutated after
        # add_assistant/add_user, so a shallow copy is safe.
        _snap_messages = list(session.messages)
        _snap_events = list(_event_log)

        # Diagnostic: time each end-of-turn await so a stuck step shows
        # up in the daemon log. The user-visible symptom is "next
        # message blocks after a turn ends" - any await that takes >1s
        # under the session lock is a candidate culprit.
        _end_t0 = time.monotonic()

        try:
            await asyncio.to_thread(_store.put, session)
        except Exception as persist_exc:
            logger.warning("session_put_failed: %s", persist_exc)
        _t_put = time.monotonic() - _end_t0

        # Lock will release as soon as ``_chat_locked`` returns. The
        # heavy persists run in background after that. ``messages_baseline``
        # tells the scheduler how to slice the per-turn delta - if
        # compaction shrunk the list mid-turn, the scheduler falls back
        # to the full-blob path automatically.
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
            # Surface the resolved model name on the turn_complete SSE
            # so the web Context panel can show it without waiting for
            # the next /sessions/{sid} polling cycle (Flutter web parity).
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
        except Exception:
            pass

        _t_ws_start = time.monotonic()
        try:
            _ws = workspace or ""
            if _ws:
                from digitorn.core.api.apps_v2 import _get_workspace_status
                result_event_data["workspace_status"] = await asyncio.to_thread(
                    _get_workspace_status, _ws,
                )
        except Exception:
            pass
        _t_ws = time.monotonic() - _t_ws_start

        _t_emit_start = time.monotonic()
        if not _aborted:
            from digitorn.core.events.envelope import (
                SessionEvent as _SE, OpType as _OT, OpState as _OS,
            )
            # ``result`` is the turn's payload event (assistant text,
            # usage, workspace status). Share op_id with the turn,
            # op_state=COMPLETED marks the turn's payload delivery.
            _turn_op_id = correlation_id or f"turn-{session_id}"
            _result_envelope = _SE.build(
                type="result",
                app_id=app_id, session_id=session_id, user_id=uid,
                op_id=_turn_op_id, op_type=_OT.TURN, op_state=_OS.COMPLETED,
                correlation_id=correlation_id or "",
                payload=result_event_data,
            )
            # Fire-and-forget: ``event_bus.emit`` awaits the Socket.IO
            # transport, which under a slow / disconnecting client can
            # take 1-5s on Windows IOCP (TCP write buffer backpressure
            # blocks the proactor's WriteFile). Awaiting it here held
            # the session lock for the full duration, delaying the
            # NEXT user message on the same session by the same amount.
            # Now: scheduled on the loop, kept alive via a strong-ref
            # set so GC doesn't collect the in-flight task. The
            # underlying emit still has its own 5s timeout +
            # backpressure semaphore (see session_bus.py).
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
                # Loop not running (shutdown). Fall back to awaiting so
                # we don't silently drop the result event during a
                # graceful close.
                await self.event_bus.emit(_result_envelope)
        _t_emit = time.monotonic() - _t_emit_start

        # Usage tracking is owned by the digitorn LLM gateway.
        # The daemon does not record token/cost rows anymore.

        # Total end-of-turn time spent under the session lock. If this
        # is consistently > 1-2 s, the next message from the same
        # session will appear blocked because session_lock.acquire()
        # waits on the previous turn's release. Surface the breakdown
        # so users can pinpoint which step is slow.
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
        """Drain background notifications and run an agent turn if any exist.

        Returns a TurnResult if notifications were found and processed,
        or None if there are no pending notifications.
        """
        from digitorn.core.runtime.agent_loop import agent_turn

        deployed = self._get_deployed(app_id, user_id=user_id)
        cb = deployed.context_builder
        if cb is None or not hasattr(cb, "drain_bg_notifications"):
            return None

        notifications = cb.drain_bg_notifications(session_id=session_id)

        # Off-loop: ``drain_buffered`` reads + unlinks the JSONL buffer
        # for this app. Tiny IO on the happy path but pile-ups under
        # cron storms or background-trigger bursts can put the buffer
        # file into the multi-MB range; doing the read on the main
        # loop stalls every turn the storm runs through. Worker
        # thread keeps the loop free.
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
            # Each background-task notification has a task_id that
            # doubles as its op_id (lifecycle: running → progress
            # events → completed/failed). We always emit RUNNING here
            # because the notification arrives WHILE the task is alive;
            # the terminal state is later emitted by bg_task_update.
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

        try:
            result = await agent_turn(
                ctx,
                session.messages,
                max_turns=deployed.compiled.execution.max_turns,
                timeout=deployed.compiled.execution.timeout,
                on_tool_call=_on_tool_call,
                hook_runner=hook_runner,
            )
        finally:
            if _had_hook_cb and hook_runner is not None:
                hook_runner.on_hook_event = _prev_hook_cb

        if result.content:
            # Idempotent guard - see the matching block above for context.
            # The agent loop's no-tool-calls exit path now appends the
            # assistant message itself, so this site only needs to add
            # it when the loop didn't (legacy / sub-agent code paths).
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
        # notification_result closes the background-notification cycle
        # started by the ``notification`` emits above. Use a stable
        # op_id based on the aggregate (count) so the client can
        # correlate per-batch if it wants to.
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
