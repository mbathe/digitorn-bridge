"""Routes for the sessions group, extracted from the legacy ``apps.py``.

This module is part of the ``apps_v2`` refactoring — same paths,
same response shapes, same behaviour, just split across multiple files.
"""

from __future__ import annotations

import asyncio
import json as _json
import logging
import os
import re
import re as _re
import shutil
import tempfile
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, model_validator

from digitorn.core.quota import QuotaPutRequest

from ._shared import (
    _MAX_CONCURRENT_TURNS,
    _turn_semaphore,
    _active_turn_tasks,
    _SAFE_ID_RE,
    _agent_turns_lock,
    _MESSAGE_MAX_BYTES,
    _MAX_ARTIFACT_DOWNLOAD_SIZE,
    _SECRET_REF_RE,
    _validate_app_id,
    _build_history_turns,
    _classify_error,
    _get_workspace_status,
    _validate_id,
    _inc_agent_turns,
    _activate_preview_session,
    _caller_user_id,
    _get_deployed,
    _raise_not_deployed,
    _is_deployed,
    _require_permission,
    _turn_event,
    _require_session_create_or_owner,
    _require_session_access,
    _refresh_deployed_agent_tools,
    _drain_queue_next,
    _context_advice,
    _merge_resources,
    _resolve_deployed_preview,
    _strip_content_from_files,
    _validate_payload_against_schema,
    _mime_matches,
    _assert_session_visible,
    _get_bg_session_store,
    _get_activation_store,
    _resolve_app_bundle_dir,
    _try_resize_image,
    _has_static_dist,
    _try_serve_static_dist,
    _proxy_preview_http,
    _serialise_widget_node,
    _serialise_widgets,
    _execute_widget_tool,
    _get_quota_store,
    _require_admin_for_quota,
    _usage_snapshot,
    _walk_yaml_for_secrets,
    _get_manager,
    _get_rate_limiter,
    DeployRequest,
    RunRequest,
    ChatRequest,
    AppSummary,
    AppResponse,
    ValidateRequest,
    PipelineRequest,
    NotificationCheckRequest,
    SessionMessageRequest,
    CreateSessionRequest,
    WorkspaceImportRequest,
    WorkspaceForkRequest,
    FileActionRequest,
    HunksActionRequest,
    WritebackRequest,
    CommitRequest,
    LspRpcRequest,
    LspCancelRequest,
    BackgroundSessionCreateRequest,
    PayloadSetRequest,
    BackgroundTaskRequest,
    BackgroundTaskActionRequest,
    WatcherCreateRequest,
    ToolExecuteRequest,
    WidgetActionRequest,
    InteractRequest,
    DisableRequest,
    ApprovalResolveRequest,
    SecretSetRequest,
    SecretsBulkSetRequest,
    OAuthCallbackParams,
    InjectOAuthTokenRequest
)

router = APIRouter(tags=["apps"])



@router.post("/{app_id}/sessions", response_model=AppResponse)
async def create_session(
    request: Request, app_id: str,
    body: CreateSessionRequest | None = None,
) -> AppResponse:
    """Create a new conversation session.

    Returns a server-generated session_id that the client uses for all
    subsequent calls (/messages, /events, /history, /abort, etc.).

    The session is initialized empty — no messages, no workspace, unless
    ``workspace_path`` is provided in the body. When set, the directory
    must exist and be writable; the daemon will persist session state
    inside ``{workspace_path}/.digitorn/sessions/{session_id}/``.
    """
    _validate_id(app_id)
    manager = _get_manager(request)
    if not _is_deployed(request, app_id):
        _raise_not_deployed(request, app_id)

    import uuid as _uuid
    from datetime import datetime, timezone
    from pathlib import Path as _Path

    user_id = getattr(request.state, "user_id", None) or "local"
    session_id = str(_uuid.uuid4())

    ws_path = (body.workspace_path if body else None) or ""

    # Strict workspace contract — when the app declares
    # ``execution.workspace_mode: required``, the workspace MUST be
    # provided at session creation. Refuse to spawn a session that
    # would only fail on its first turn with "This app requires a
    # workspace". The client is expected to prompt the user, then
    # POST /sessions again with ``workspace_path`` set. Once set, the
    # path is persisted on the session and every following message
    # (REST or Socket.IO) inherits it automatically — callers never
    # need to repeat ``workspace`` afterwards.
    deployed_for_check = _get_deployed(request, app_id)
    if deployed_for_check is not None:
        ws_mode = getattr(
            deployed_for_check.compiled.execution, "workspace_mode", "auto",
        )
        if ws_mode == "required" and not ws_path:
            raise HTTPException(
                status_code=400,
                detail={
                    "error": "workspace_required",
                    "message": (
                        f"App '{app_id}' requires a workspace. Pass "
                        f"`workspace_path` in the request body when "
                        f"creating the session."
                    ),
                    "workspace_mode": "required",
                },
            )

    if ws_path:
        try:
            p = _Path(ws_path).expanduser().resolve()
            if not p.exists():
                p.mkdir(parents=True, exist_ok=True)
            if not p.is_dir():
                raise HTTPException(
                    status_code=400,
                    detail=f"workspace_path is not a directory: {p}",
                )
            test = p / ".digitorn"
            test.mkdir(parents=True, exist_ok=True)
            ws_path = str(p)
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(
                status_code=400,
                detail=f"workspace_path unusable: {exc}",
            )

    # Create the session in the store so it appears in listings immediately
    from digitorn.core.app.sessions import ConversationSession
    session = ConversationSession(
        session_id=session_id,
        app_id=app_id,
        user_id=user_id,
        workspace=ws_path,
    )

    # Get the deployed app's system prompt for initialization
    deployed = _get_deployed(request, app_id)
    if deployed:
        effective_prompt = deployed.entry_context.system_prompt or ""
        if effective_prompt:
            session.add_system(effective_prompt)

        # Apply greeting if configured
        greeting = getattr(deployed.compiled.execution, "greeting", "")
        if greeting and greeting.strip():
            session.greeting = greeting.strip()

    await asyncio.to_thread(manager._session_store.put, session)

    # Compute initial context estimate
    context = {}
    if deployed:
        entry_ctx = deployed.entry_context
        system_prompt = entry_ctx.system_prompt or ""
        tools = entry_ctx.tools or []
        cc = entry_ctx.context_config

        sys_tokens = len(system_prompt) // 4
        tools_tokens = len(tools) * 90
        max_tok = cc.max_tokens if cc else 200000
        out_reserved = cc.output_reserved if cc else 4096
        effective = max_tok - out_reserved
        total = sys_tokens + tools_tokens

        context = {
            "max_tokens": max_tok,
            "output_reserved": out_reserved,
            "effective_max": effective,
            "system_prompt_tokens": sys_tokens,
            "system_prompt_pct": round(sys_tokens / max(effective, 1) * 100, 2),
            "tools_schema_tokens": tools_tokens,
            "tools_schema_pct": round(tools_tokens / max(effective, 1) * 100, 2),
            "message_history_tokens": 0,
            "message_history_pct": 0,
            "total_estimated_tokens": total,
            "pressure": round(total / max(effective, 1), 4),
            "available_tokens": max(0, effective - total),
            "compactions": 0,
        }

    preview_url: str | None = None
    if deployed is not None:
        # Dev-server mode (preview.enabled: true) — preview_manager exists.
        # Static-dist mode (preview.enabled: false) — no preview_manager but
        # the preview module is loaded and web/dist/ serves the UI.
        has_preview = (
            getattr(deployed, "preview_manager", None) is not None
            or "preview" in (deployed.modules or {})
        )
        if has_preview:
            # Include the caller's JWT so the iframe can authenticate
            # its Socket.IO connection without a separate login flow.
            _auth_hdr = request.headers.get("authorization", "")
            _preview_token = _auth_hdr.split(" ", 1)[1] if _auth_hdr.startswith("Bearer ") else ""
            preview_url = (
                f"/api/apps/{app_id}/preview-server/proxy/"
                f"?session_id={session_id}"
            )
            if _preview_token:
                preview_url += f"&token={_preview_token}"

    return AppResponse(success=True, data={
        "session_id": session_id,
        "app_id": app_id,
        "title": "",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "is_active": False,
        "message_count": 0,
        "greeting": getattr(session, "greeting", ""),
        "context": context,
        "preview_url": preview_url,
        "workspace": ws_path,
    })


@router.get("/{app_id}/sessions", response_model=AppResponse)
async def list_sessions(
    request: Request,
    app_id: str,
    limit: int = 50,
    offset: int = 0,
) -> AppResponse:
    """List sessions for an app with pagination.

    Query params:
        limit:  max sessions to return (default 50, max 200, 0 = all)
        offset: skip first N sessions (for pagination)

    Response includes ``total`` for the frontend to know if there are more.
    """
    _validate_id(app_id)
    limit = max(0, min(limit, 200))
    offset = max(0, offset)
    manager = _get_manager(request)
    if not _is_deployed(request, app_id):
        _raise_not_deployed(request, app_id)
    user_id = getattr(request.state, "user_id", None)
    total = await manager.count_sessions(app_id, user_id=user_id)
    sessions = await manager.list_sessions(app_id, user_id=user_id, limit=limit, offset=offset)
    for s in sessions:
        s["is_active"] = manager.is_session_active(app_id, s.get("session_id", ""))
    return AppResponse(success=True, data={
        "sessions": sessions,
        "total": total,
        "limit": limit,
        "offset": offset,
    })


@router.get("/{app_id}/sessions/search", response_model=AppResponse)
async def search_sessions(
    request: Request,
    app_id: str,
    q: str = "",
    limit: int = 20,
    offset: int = 0,
) -> AppResponse:
    """Search across all sessions — matches title and message content.

    Returns sessions ranked by relevance (title match > recent message match).
    """
    _validate_id(app_id)
    manager = _get_manager(request)
    if not _is_deployed(request, app_id):
        _raise_not_deployed(request, app_id)

    if not q or not q.strip():
        return AppResponse(success=True, data={"sessions": [], "total": 0, "query": q})

    query = q.strip().lower()
    user_id = getattr(request.state, "user_id", None)

    # Get all sessions (max 200 to bound search cost)
    if user_id:
        all_sessions = await manager.list_sessions(app_id, user_id=user_id, limit=200)
    else:
        all_sessions = await manager.list_sessions(app_id, limit=200)

    matches: list[tuple[int, dict]] = []  # (score, session_summary)
    for s in all_sessions:
        score = 0
        sid = s.get("session_id", "")
        title = s.get("title", "")
        snippets: list[str] = []

        # Title match (highest weight)
        if query in title.lower():
            score += 10
            snippets.append(f"title: {title[:100]}")

        # Search in persisted messages (first 10 messages, 500 chars each)
        try:
            uid = s.get("user_id", user_id or "local")
            messages = await asyncio.to_thread(manager._session_store.load_messages, app_id, sid, user_id=uid)
            if messages:
                for i, msg in enumerate(messages[:10]):
                    content = (msg.get("content", "") or "")[:500].lower()
                    if query in content:
                        score += 5 if i < 2 else 1
                        # Extract snippet around match
                        idx = content.find(query)
                        start = max(0, idx - 40)
                        end = min(len(content), idx + len(query) + 40)
                        snippet = content[start:end].strip()
                        snippets.append(f"message[{i}]: ...{snippet}...")
                        if len(snippets) >= 3:
                            break
        except Exception:
            pass

        if score > 0:
            s["relevance"] = score
            s["snippets"] = snippets[:3]
            matches.append((score, s))

    # Sort by relevance (descending), then by last_active (descending)
    matches.sort(key=lambda x: (-x[0], -x[1].get("last_active", 0)))
    total = len(matches)
    page = [m[1] for m in matches[offset:offset + limit]]

    return AppResponse(success=True, data={
        "sessions": page,
        "total": total,
        "query": q,
        "limit": limit,
        "offset": offset,
    })


@router.get("/{app_id}/sessions/{session_id}", response_model=AppResponse)
async def get_session(request: Request, app_id: str, session_id: str) -> AppResponse:
    """Get session status — metadata, live metrics, context pressure.

    Single endpoint for clients to get the full session state on load.
    All numbers come from the SessionMetrics counters (populated by
    agent_loop during execution) — no estimation or speculation.
    """
    _validate_id(app_id)
    _validate_id(session_id, "session_id")
    manager = _get_manager(request)
    if not _is_deployed(request, app_id):
        _raise_not_deployed(request, app_id)
    # Uniform session-access gate: anonymous → 401, cross-user → 404,
    # owner → returns the session. Without this, this endpoint was the
    # only ``/sessions/{sid}`` route that returned 404 for anonymous
    # callers (instead of 401), which made client-side error handling
    # inconsistent and gated every other CVE-fix path the helper
    # encodes.
    session = await _require_session_access(request, app_id, session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found or expired")

    data = session.summary()

    # is_active: True if an agent turn is in progress right now
    data["is_active"] = manager.is_session_active(app_id, session_id)

    # Live metrics from SessionMetrics (populated by agent_loop).
    # The agent_loop may store metrics under app_id="default" if ctx.app_id
    # is not set, so we try both the real app_id and "default".
    try:
        from digitorn.core.runtime.session_metrics import get_session_metrics, _sessions
        # Find the existing entry. Keys are "{app_id}:{session_id}:{agent_id}"
        # — we don't know the agent_id a priori (builder, main, chatbot, …),
        # so we scan every prefix match and pick the one with real tokens.
        # Falls back to "default:" prefix for legacy entries created before
        # ctx.app_id was wired (bootstrap pre-2026-04-19).
        sm = None
        best_total = -1
        for _try_app in (app_id, "default"):
            prefix = f"{_try_app}:{session_id}:"
            for _key, _entry in _sessions.items():
                if _key.startswith(prefix) and _entry.total_tokens > best_total:
                    sm = _entry
                    best_total = _entry.total_tokens
            if sm is not None and best_total > 0:
                break
        if sm is None:
            # Fallback: get_or_create (may be empty for new sessions)
            sm = get_session_metrics(app_id, session_id)
        data["turn_number"] = sm.turn
        data["tokens"] = {
            "prompt": sm.prompt_tokens,
            "completion": sm.completion_tokens,
            "total": sm.total_tokens,
        }
        ctx_snapshot = sm.context.snapshot()

        # If context is empty (no turn yet), compute initial estimate from the deployed app
        if ctx_snapshot.get("total_estimated_tokens", 0) == 0:
            deployed = _get_deployed(request, app_id)
            if deployed:
                entry_ctx = deployed.entry_context
                system_prompt = entry_ctx.system_prompt or ""
                tools = entry_ctx.tools or []
                cc = entry_ctx.context_config

                sys_tokens = len(system_prompt) // 4
                tools_tokens = len(tools) * 90
                msg_tokens = sum(
                    len(m.get("content", "")) // 4
                    for m in session.messages
                    if isinstance(m.get("content"), str)
                )
                total = sys_tokens + tools_tokens + msg_tokens
                max_tok = cc.max_tokens if cc else 200000
                out_reserved = cc.output_reserved if cc else 4096
                effective = max_tok - out_reserved

                ctx_snapshot = {
                    "max_tokens": max_tok,
                    "output_reserved": out_reserved,
                    "effective_max": effective,
                    "system_prompt_tokens": sys_tokens,
                    "system_prompt_pct": round(sys_tokens / max(effective, 1) * 100, 2),
                    "tools_schema_tokens": tools_tokens,
                    "tools_schema_pct": round(tools_tokens / max(effective, 1) * 100, 2),
                    "message_history_tokens": msg_tokens,
                    "message_history_pct": round(msg_tokens / max(effective, 1) * 100, 2),
                    "total_estimated_tokens": total,
                    "pressure": round(total / max(effective, 1), 4),
                    "available_tokens": max(0, effective - total),
                    "compactions": 0,
                }

        data["context"] = ctx_snapshot
        data["tools"] = {
            "total_calls": sm.tool_calls_total,
            "success": sm.tool_calls_success,
            "failed": sm.tool_calls_failed,
        }
        data["errors"] = {
            "total": sm.errors,
            "last": sm.last_error,
        }
        data["model"] = sm.model or (
            getattr(deployed.entry_context.provider, "model", "")
            if _get_deployed(request, app_id) else ""
        )
    except Exception:
        pass  # Metrics not available (session never had a chat turn)

    return AppResponse(success=True, data=data)


@router.delete("/{app_id}/sessions/{session_id}", response_model=AppResponse)
async def delete_session(request: Request, app_id: str, session_id: str) -> AppResponse:
    """Delete a session and its history."""
    _validate_id(app_id)
    _validate_id(session_id, "session_id")
    manager = _get_manager(request)
    if not _is_deployed(request, app_id):
        _raise_not_deployed(request, app_id)

    _uid = getattr(request.state, "user_id", None) or "local"
    deleted = await manager.end_session(app_id, session_id, user_id=_uid)
    if not deleted:
        raise HTTPException(status_code=404, detail="Session not found or expired")
    return AppResponse(success=True, data={
        "session_id": session_id,
        "deleted": True,
    })


@router.post("/{app_id}/sessions/{session_id}/fork", response_model=AppResponse)
async def fork_session(request: Request, app_id: str, session_id: str) -> AppResponse:
    """Fork a session — create a new session with the same message history."""
    import uuid as _uuid
    _validate_id(app_id)
    _validate_id(session_id, "session_id")
    manager = _get_manager(request)
    if not _is_deployed(request, app_id):
        _raise_not_deployed(request, app_id)
    # BUG-074: reject cross-user fork (session theft).
    source = await _require_session_access(request, app_id, session_id)
    _uid = getattr(request.state, "user_id", None) or "local"

    new_id = str(_uuid.uuid4())
    from digitorn.core.app.sessions import ConversationSession
    new_session = ConversationSession(
        session_id=new_id,
        app_id=app_id,
        user_id=_uid,
        messages=[m.copy() for m in source.messages],
        title=f"Fork of {source.title or session_id[:8]}",
        memory_snapshot=dict(source.memory_snapshot),
    )
    await asyncio.to_thread(manager._session_store.put, new_session)

    return AppResponse(success=True, data={
        "forked_from": session_id,
        "new_session_id": new_id,
        "message_count": len(new_session.messages),
    })


@router.post("/{app_id}/sessions/{session_id}/abort", response_model=AppResponse)
async def abort_session_turn(
    request: Request, app_id: str, session_id: str,
    purge_queue: bool = False,
) -> AppResponse:
    """Abort the currently running agent turn for a session.

    **Default behavior**: cancels only the currently running turn. The
    rest of the message queue is **preserved** and the dispatcher
    picks up the next message automatically — the user gets
    ``message_started`` for ``next_correlation_id`` within seconds.

    ``?purge_queue=true`` drops every queued message along with the
    abort — use when the user clicks "Stop everything" rather than
    "Skip this message".

    The session state (messages, memory, tool calls) is preserved up
    to the interruption point. Orphaned tool_calls get synthetic error
    results on the next message so the LLM resumes cleanly.

    Safe to call even if no turn is running (returns success with
    was_active=false).
    """
    _validate_id(app_id)
    _validate_id(session_id, "session_id")
    manager = _get_manager(request)
    if not _is_deployed(request, app_id):
        _raise_not_deployed(request, app_id)
    # BUG-071: reject cross-user aborts (destructive remote DoS).
    await _require_session_access(request, app_id, session_id)

    _uid = getattr(request.state, "user_id", None) or "local"

    was_active = manager.is_session_active(app_id, session_id)

    # Cancel the running asyncio task — this triggers CancelledError
    # inside _chat_locked which saves state + marks session.interrupted
    active_key = f"{app_id}:{session_id}"
    task = manager._session_tasks.get(active_key)
    if task is not None and not task.done():
        task.cancel()

    # Kill background shell tasks for this session so they don't orphan.
    # Each killed task sends a 'cancelled' notification to the agent queue
    # so the agent knows on resume.
    bg_killed = 0
    agents_killed = 0
    deployed = _get_deployed(request, app_id)
    if deployed:
        shell_mod = deployed.modules.get("shell")
        if shell_mod is not None and hasattr(shell_mod, "cleanup_session"):
            try:
                await shell_mod.cleanup_session(session_id)
                bg_killed = 1  # flag that cleanup ran
            except Exception:
                logger.debug("abort: shell cleanup_session failed", exc_info=True)

        # Kill running sub-agents for this session.
        # They can't survive without the coordinator, and their asyncio tasks
        # would leak if left running after the parent turn is cancelled.
        spawn_mod = deployed.modules.get("agent_spawn")
        if spawn_mod is not None and hasattr(spawn_mod, "cleanup_session"):
            try:
                await spawn_mod.cleanup_session(session_id)
                agents_killed = 1
            except Exception:
                logger.debug("abort: agent_spawn cleanup_session failed", exc_info=True)

        # Kill background tasks from context_builder (watchers, background_run)
        cb_mod = deployed.entry_context.context_builder if hasattr(deployed, "entry_context") else None
        if cb_mod is None:
            cb_mod = deployed.modules.get("context_builder")
        if cb_mod is not None and hasattr(cb_mod, "cleanup_session_bg_tasks"):
            try:
                await cb_mod.cleanup_session_bg_tasks(session_id)
            except Exception:
                logger.debug("abort: context_builder cleanup_session_bg_tasks failed", exc_info=True)

    # Queue handling — default is "keep the rest". Explicit opt-in
    # ``?purge_queue=true`` drops everything. The currently-running row
    # is ALSO cleaned up here (the drain in _run_turn's finally will
    # mark it done, but we want abort semantics: status=cancelled).
    queue_purged = 0
    from digitorn.core.app import message_queue as _mq
    try:
        # Mark the currently running row as cancelled (if any). The
        # drain chain will still kick in after _run_turn's finally
        # returns and pick the next queued message.
        entries = await _mq.list_for_session(session_id)
        for e in entries:
            if e.status == "running":
                try:
                    await _mq.mark_cancelled(e.id)
                    _mq.fail_awaiter(
                        e.correlation_id,
                        RuntimeError("aborted by user"),
                    )
                except Exception:
                    pass
                break
        if purge_queue:
            queue_purged = await _mq.clear(session_id)
    except Exception:
        logger.debug("abort: queue cleanup failed", exc_info=True)

    # Signal abort via the event bus (Socket.IO clients see it immediately)
    try:
        from digitorn.core.events.envelope import OpState as _OS
        # abort is session-scoped — every currently-active op in the
        # session transitions to CANCELLED through their own terminal
        # events (BUG-054 sweeper guarantees it). This event carries
        # the abort announcement itself so the client can show the
        # banner immediately.
        await manager.event_bus.emit(_turn_event(
            "abort",
            app_id=app_id, session_id=session_id, user_id=_uid or "local",
            correlation_id=f"abort-{session_id}",
            op_state=_OS.CANCELLED,
            payload={
                "session_id": session_id,
                "queue_purged": queue_purged,
                "queue_preserved": not purge_queue,
            },
        ))
    except Exception:
        pass

    # If the queue was preserved AND there's a next message queued,
    # kick off the drain NOW so the frontend sees message_started for
    # the next entry without having to wait for _run_turn's finally
    # (which might be blocked on the cancellation).
    if not purge_queue:
        try:
            # Small delay so the cancelled task fully winds down before
            # we dispatch the next. Prevents a race where the drain
            # tries to acquire the session_lock before the aborted
            # task has released it.
            async def _resume() -> None:
                await asyncio.sleep(0.2)
                try:
                    await _drain_queue_next(
                        request, app_id, session_id, _uid,
                    )
                except Exception as exc:
                    logger.warning("abort_resume_failed: %s", exc)
            asyncio.create_task(_resume())
        except Exception:
            pass

    return AppResponse(success=True, data={
        "session_id": session_id,
        "was_active": was_active,
        "aborted": True,
        "bg_tasks_cleaned": bg_killed > 0,
        "agents_cleaned": agents_killed > 0,
        "queue_purged": queue_purged,
        "queue_preserved": not purge_queue,
    })


@router.post("/{app_id}/sessions/{session_id}/resume", response_model=AppResponse)
async def resume_session(request: Request, app_id: str, session_id: str) -> AppResponse:
    """Resume an interrupted session automatically.

    When a session was interrupted (daemon crash, network loss, etc.),
    the client calls this to trigger recovery WITHOUT the user typing
    a message. The daemon:

    1. Detects orphaned tool_calls and injects synthetic error results
    2. Sends a system message telling the LLM to continue
    3. Triggers a new agent turn (async, events via Socket.IO)

    If the session is NOT interrupted or is currently active, returns
    a no-op success response.

    The client flow on reconnect:
        GET  /sessions/{sid}                   → check is_active + interrupted
        Socket.IO join_session with since=N    → reconnect + replay missed events
        POST /sessions/{sid}/resume            → auto-continue if interrupted
    """
    _validate_id(app_id)
    _validate_id(session_id, "session_id")
    manager = _get_manager(request)
    if not _is_deployed(request, app_id):
        _raise_not_deployed(request, app_id)

    _uid = getattr(request.state, "user_id", None) or "local"
    session = await manager.get_session(app_id, session_id, user_id=_uid)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")

    # If a turn is already running, nothing to resume
    if manager.is_session_active(app_id, session_id):
        return AppResponse(success=True, data={
            "resumed": False,
            "reason": "Turn already in progress",
        })

    # If session is not interrupted, nothing to do
    if not session.interrupted:
        return AppResponse(success=True, data={
            "resumed": False,
            "reason": "Session is not interrupted",
        })

    # Trigger an auto-continue turn via /messages
    # The manager will detect interrupted=True and run _recover_interrupted_session
    async def _resume_turn():
        try:
            await manager.chat(
                app_id, session_id,
                "[auto-resume] Continue from where you left off.",
                user_id=_uid,
            )
        except Exception as exc:
            from digitorn.core.events.envelope import OpState as _OS
            await manager.event_bus.emit(_turn_event(
                "error",
                app_id=app_id, session_id=session_id, user_id=_uid,
                correlation_id=f"resume-{session_id}",
                op_state=_OS.FAILED,
                payload={"error": f"Resume failed: {exc}"},
            ))

    async def _guarded_resume():
        async with _turn_semaphore:
            await _resume_turn()

    task = asyncio.create_task(_guarded_resume())
    _active_turn_tasks.add(task)
    task.add_done_callback(_active_turn_tasks.discard)

    return AppResponse(success=True, data={
        "resumed": True,
        "session_id": session_id,
    })


@router.post("/{app_id}/sessions/{session_id}/undo", response_model=AppResponse)
async def undo_session(request: Request, app_id: str, session_id: str) -> AppResponse:
    """Undo the last file edit via filesystem checkpoints."""
    _validate_id(app_id)
    _validate_id(session_id, "session_id")
    manager = _get_manager(request)
    if not _is_deployed(request, app_id):
        _raise_not_deployed(request, app_id)

    deployed = _get_deployed(request, app_id)
    if deployed is None:
        _raise_not_deployed(request, app_id)

    fs_module = deployed.modules.get("filesystem")
    if fs_module is None or not hasattr(fs_module, "_checkpoints"):
        return AppResponse(success=False, error="Filesystem undo not available")

    if not fs_module._checkpoints:
        return AppResponse(success=False, error="No checkpoints to undo")

    # Find the most recently checkpointed file
    from pathlib import Path as _Path
    latest_path = None
    latest_ts = 0.0
    for fpath, stack in fs_module._checkpoints.items():
        if stack and stack[-1][0] > latest_ts:
            latest_ts = stack[-1][0]
            latest_path = fpath
    if latest_path is None:
        return AppResponse(success=False, error="No checkpoints found")

    stack = fs_module._checkpoints[latest_path]
    _ts, content = stack.pop()
    _Path(latest_path).write_bytes(content)
    remaining = sum(len(s) for s in fs_module._checkpoints.values())

    return AppResponse(success=True, data={
        "path": latest_path,
        "restored_bytes": len(content),
        "remaining": remaining,
    })


@router.post("/{app_id}/sessions/{session_id}/compact", response_model=AppResponse)
async def compact_session(request: Request, app_id: str, session_id: str) -> AppResponse:
    """Compact the context window for a session (truncate old messages)."""
    _validate_id(app_id)
    _validate_id(session_id, "session_id")
    manager = _get_manager(request)
    if not _is_deployed(request, app_id):
        _raise_not_deployed(request, app_id)

    _uid = getattr(request.state, "user_id", None) or "local"
    session = await manager.get_session(app_id, session_id, user_id=_uid)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")

    deployed = _get_deployed(request, app_id)
    if deployed is None:
        _raise_not_deployed(request, app_id)

    messages = session.messages
    before = len(messages)
    if before < 4:
        return AppResponse(success=True, data={
            "before": before, "after": before, "freed": 0,
            "note": "Too few messages to compact",
        })

    try:
        from digitorn.core.runtime.compaction import emergency_compact
        ctx = deployed.entry_context
        # Manual path bypasses _chat_locked; pass session identity +
        # bus explicitly so the compaction event persists.
        await emergency_compact(
            ctx, messages,
            reason="manual",
            event_bus=manager.event_bus,
            app_id=app_id,
            session_id=session_id,
            user_id=_uid,
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Compaction failed: {exc}")

    after = len(messages)
    return AppResponse(success=True, data={
        "before": before,
        "after": after,
        "freed": before - after,
    })


@router.get("/{app_id}/sessions/{session_id}/export", response_model=AppResponse)
async def export_session(
    request: Request,
    app_id: str,
    session_id: str,
    format: str = "markdown",
) -> AppResponse:
    """Export a session as Markdown (or other formats).

    Produces a clean document with all conversation turns, tool calls,
    and results. Suitable for sharing, archiving, or documentation.
    """
    _validate_id(app_id)
    _validate_id(session_id, "session_id")
    manager = _get_manager(request)
    if not _is_deployed(request, app_id):
        _raise_not_deployed(request, app_id)

    # BUG-075: reject anonymous or cross-user export (data exfil).
    session = await _require_session_access(request, app_id, session_id)

    if format != "markdown":
        return AppResponse(success=False, error=f"Unsupported format: {format}. Use 'markdown'.")

    import datetime

    lines: list[str] = []
    title = session.title or "Untitled Session"
    created = datetime.datetime.fromtimestamp(session.created_at).strftime("%Y-%m-%d %H:%M")
    lines.append(f"# {title}")
    lines.append("")
    lines.append(f"**App:** {app_id} | **Session:** `{session_id[:8]}...` | **Created:** {created}")
    lines.append("")
    lines.append("---")
    lines.append("")

    turn_num = 0
    for msg in session.messages:
        role = msg.get("role", "")
        content = msg.get("content", "") or ""

        if role == "system":
            continue  # Skip system prompts in export

        if role == "user":
            turn_num += 1
            lines.append(f"## Turn {turn_num}")
            lines.append("")
            lines.append(f"**User:**")
            lines.append("")
            lines.append(content.strip())
            lines.append("")

        elif role == "assistant":
            if content.strip():
                lines.append(f"**Assistant:**")
                lines.append("")
                lines.append(content.strip())
                lines.append("")

            # Tool calls
            tool_calls = msg.get("tool_calls", [])
            if tool_calls:
                for tc in tool_calls:
                    func = tc.get("function", {})
                    name = func.get("name", "?")
                    args = func.get("arguments", "")
                    if isinstance(args, str) and len(args) > 200:
                        args = args[:200] + "..."
                    lines.append(f"> **Tool:** `{name}`")
                    if args:
                        lines.append(f"> ```json")
                        lines.append(f"> {args}")
                        lines.append(f"> ```")
                    lines.append("")

        elif role == "tool":
            # Tool result
            result = content
            if len(result) > 500:
                result = result[:500] + "\n... (truncated)"
            lines.append(f"> **Result:**")
            lines.append(f"> ```")
            for rline in result.split("\n")[:20]:
                lines.append(f"> {rline}")
            lines.append(f"> ```")
            lines.append("")

    # Footer with stats
    lines.append("---")
    lines.append("")
    lines.append(f"*Exported from Digitorn · {turn_num} turns · {len(session.messages)} messages*")

    md = "\n".join(lines)
    filename = f"{app_id}_{session_id[:8]}_{created.replace(' ', '_').replace(':', '')}.md"

    return AppResponse(success=True, data={
        "content": md,
        "format": "markdown",
        "filename": filename,
        "turns": turn_num,
        "messages": len(session.messages),
    })


@router.get("/{app_id}/sessions/{session_id}/history", response_model=AppResponse)
async def get_session_history(
    request: Request, app_id: str, session_id: str,
    include_system: bool = False,
    since_seq: int = 0,
    events_limit: int = 50000,
) -> AppResponse:
    """Full chronological history for a session — every message AND
    every event the daemon has ever recorded.

    The response carries **two parallel streams** the client stitches
    together to render the full timeline:

    - ``messages``: user↔assistant exchanges (reconstructed turns
      when ``include_system=false``, raw otherwise) — pulled from
      ``session_messages`` (append-only, durable).
    - ``events``: every envelope emitted on the session bus — user
      messages, tool_start / tool_call / tool_result, thinking
      (started/delta/complete), tokens, agent_event, hook_notification,
      quota_exceeded, compaction, abort, memory_update, context
      warnings, approval_request/resolve, anything the runtime
      fires. Pulled from ``session_events`` (append-only, durable,
      ordered by ``seq``). Nothing is filtered out by type — the
      client decides what to render.

    **Pagination** via ``since_seq`` + ``events_limit``:
      - First load: ``since_seq=0`` returns the first ``events_limit``
        events. Response ``events_next_seq`` is the seq to use for
        the next page (= last returned seq + 1). ``events_has_more``
        tells the client to keep paging.
      - Subsequent loads: pass the previous ``events_next_seq``.

    Defaults (``since_seq=0``, ``events_limit=50000``) return the
    full history for any realistic session in one round-trip. Raise
    ``events_limit`` if you know you need more; lower it for very
    long-running sessions where you want to stream the timeline.

    ``include_system=true`` returns the raw message list (including
    the behavior engine's system directives) — for the dev SDK
    inspecting behavior enforcement. Default is the clean
    user/assistant view for the chat UI.
    """
    _validate_id(app_id)
    _validate_id(session_id, "session_id")
    manager = _get_manager(request)
    if not _is_deployed(request, app_id):
        _raise_not_deployed(request, app_id)
    user_id = getattr(request.state, "user_id", None)
    session = await manager.get_session(app_id, session_id, user_id=user_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found or expired")
    # Build structured turns from raw LLM messages
    if include_system:
        turns = session.messages
    else:
        turns = _build_history_turns(session.messages)

    # Load full chronology from the unified ``history_log`` table
    # (authoritative bank-grade ledger). One SQL query returns
    # messages + events entrelacés dans l'ordre temporel exact.
    events = []
    events_total = 0
    events_next_seq = int(since_seq or 0)
    events_has_more = False
    try:
        from digitorn.core.database import get_session_factory
        from digitorn.core.models import HistoryLog
        from sqlalchemy import select, func, and_
        factory = get_session_factory()
        async with factory() as db:
            # Total count for this session (across all kinds).
            events_total = int((await db.execute(
                select(func.count())
                .select_from(HistoryLog)
                .where(HistoryLog.session_id == session_id)
            )).scalar() or 0)

            # Fetch rows strictly after since_seq, ordered by seq then
            # ts for determinism. **Filter on kind='event'** — messages
            # and audit rows live in the same table but DON'T belong in
            # the client's events[] stream. Without this filter a
            # kind='message' row leaks as a second ``user_message``
            # event (no event_id, no correlation_id) → frontend dedup
            # fails → duplicate user bubbles. Messages are served by
            # the ``messages`` array above.
            stmt = (
                select(HistoryLog)
                .where(HistoryLog.kind == "event")
                .where(HistoryLog.session_id == session_id)
                .where(HistoryLog.seq > int(since_seq or 0))
                .order_by(HistoryLog.seq.asc(), HistoryLog.ts.asc())
                .limit(int(events_limit))
            )
            rows = (await db.execute(stmt)).scalars().all()
            events = []
            for r in rows:
                payload = dict(r.payload or {})
                # Promote contract fields from payload to envelope top-level
                # so the client's reducer can read them directly (docs
                # at FRONTEND_CHAT_HISTORY_PROMPT.md). event_id in
                # particular is the dedup key — MUST be present.
                env_event_id = payload.get("event_id") or ""
                env_op_id = payload.get("op_id") or ""
                env_op_type = payload.get("op_type") or ""
                env_op_state = payload.get("op_state") or ""
                events.append({
                    "id": r.id,
                    "ts": r.ts.isoformat() if r.ts else None,
                    "seq": r.seq,
                    "kind": payload.get("event_kind") or r.kind,
                    "type": r.type,
                    "event_id": env_event_id,
                    "op_id": env_op_id,
                    "op_type": env_op_type,
                    "op_state": env_op_state,
                    "session_id": r.session_id,
                    "user_id": r.user_id,
                    "role": r.role,
                    "content": r.content,
                    "tool_call_id": r.tool_call_id,
                    "tool_calls": r.tool_calls,
                    "payload": payload,
                    "correlation_id": r.correlation_id or payload.get("correlation_id") or "",
                })
            if events:
                events_next_seq = int(events[-1]["seq"] or 0) + 1
                events_has_more = (
                    len(events) >= int(events_limit)
                    or (events_next_seq - int(since_seq or 0)) < events_total
                )
    except Exception as exc:
        logger.debug("history_log load failed, falling back to session_events: %s", exc)
        # Fallback to legacy session_events for backward compat during
        # the transition window.
        bus = manager.event_bus
        if bus is not None:
            try:
                events = await bus.async_replay(
                    user_id or "local", int(since_seq or 0),
                    session_id=session_id,
                    limit=int(events_limit),
                )
                events_total = len(events)
            except Exception:
                events = []

    # In-progress turn state so a reopened client knows immediately
    # that a turn is still running (and which queued messages are
    # waiting behind it).
    from digitorn.core.app import message_queue as _mq
    turn_active = await manager.is_turn_running(app_id, session.session_id)
    try:
        pending_entries = await _mq.list_for_session(
            session.session_id, include_finished=False,
        )
    except Exception:
        pending_entries = []

    data: dict[str, Any] = {
        **session.summary(),
        "messages": turns,
        "message_count": len(turns),
        "events": events or [],
        "event_count": len(events or []),
        "events_total": events_total,
        "events_next_seq": events_next_seq,
        "events_has_more": events_has_more,
        "turn_active": turn_active,
        "pending_queue": [e.to_dict() for e in pending_entries],
    }
    # Include snapshots for workspace/memory/preview state restoration
    if session.memory_snapshot:
        data["memory_snapshot"] = session.memory_snapshot
    if session.preview_snapshot:
        data["preview_snapshot"] = session.preview_snapshot
    return AppResponse(success=True, data=data)


@router.get(
    "/{app_id}/sessions/{session_id}/events",
    response_model=AppResponse,
)
async def list_session_events(
    request: Request, app_id: str, session_id: str,
    since_seq: int = 0,
    since_ts: str | None = None,
    limit: int = 500,
) -> AppResponse:
    """Fetch the persistent event log for a session.

    Unlike the in-memory ring buffer (which covers only the last N events
    per user), this endpoint reads from the ``session_events`` DB table
    that captures every meaningful event (hooks, tool calls, message
    lifecycle, errors, agent spawns, …) with timestamp + seq. Useful
    when:

    - The Flutter client reopens a session older than the ring buffer.
    - A new device joins and needs the full history.
    - Audit / compliance needs a provable turn-by-turn trace.

    Token-level events are NOT logged (reconstructed from persisted
    message content). Everything else IS.

    ``since_seq`` or ``since_ts`` filter to only events after the watermark.

    **Relationship to Socket.IO ``join_session``**

    The Socket.IO join flow pushes three distinct groups of events:

    1. *Durable replay* — the same ``session_events`` rows this HTTP
       route returns (identical shape, identical filter).
    2. *Preview/workspace hydration* — ``preview:snapshot``,
       ``workspace:snapshot`` emitted once at join time from the
       in-memory preview module (NOT persisted to ``session_events``).
    3. *Bootstrap side-channels* — occasional channel/widget snapshots.

    So ``count(/events) <= count(socket events)``: if the two numbers
    disagree, the difference is hydration, not a missing durable row.
    To verify parity, compare only the envelopes whose ``seq`` is set.

    Events are scoped to the caller's ``user_id`` — an admin querying
    another user's session gets nothing here (same rule as Socket.IO).
    """
    _validate_id(app_id)
    _validate_id(session_id, "session_id")
    if not _is_deployed(request, app_id):
        _raise_not_deployed(request, app_id)
    # BUG-070 + BUG-073: refuse anonymous + cross-user access.
    await _require_session_access(request, app_id, session_id)

    limit = max(1, min(limit, 5000))
    user_id = getattr(request.state, "user_id", "") or ""

    from digitorn.core.database import get_session_factory
    from digitorn.core.models import HistoryLog
    from sqlalchemy import select
    sf = get_session_factory()
    async with sf() as db:
        stmt = (
            select(HistoryLog)
            .where(HistoryLog.kind == "event")
            .where(HistoryLog.session_id == session_id)
            .order_by(HistoryLog.seq.asc())
            .limit(limit)
        )
        if user_id:
            stmt = stmt.where(HistoryLog.user_id == user_id)
        if since_seq:
            stmt = stmt.where(HistoryLog.seq > since_seq)
        if since_ts:
            try:
                from datetime import datetime as _dt
                ts = _dt.fromisoformat(since_ts.replace("Z", "+00:00"))
                stmt = stmt.where(HistoryLog.ts > ts)
            except Exception:
                pass
        r = await db.execute(stmt)
        rows = r.scalars().all()
    events_out: list[dict[str, Any]] = []
    for row in rows:
        payload = dict(row.payload or {})
        env: dict[str, Any] = {
            "type": row.type,
            "kind": payload.get("event_kind") or row.kind,
            "seq": row.seq,
            "ts": row.ts.isoformat() if row.ts else None,
            "app_id": row.app_id or None,
            "session_id": row.session_id or None,
            "user_id": row.user_id or None,
            "correlation_id": row.correlation_id or None,
            "payload": payload,
        }
        # Contract fields are persisted inside ``payload`` (JSON
        # column) — promote to the top level so this HTTP response
        # mirrors the Socket.IO live wire shape exactly.
        for _key in (
            "event_id", "op_id", "op_type", "op_state", "op_parent_id",
        ):
            val = payload.get(_key)
            if val is not None:
                env[_key] = val
        events_out.append(env)
    return AppResponse(success=True, data={
        "session_id": session_id,
        "count": len(events_out),
        "total": len(events_out),
        "events": events_out,
        "note": (
            "Socket.IO join_session also pushes preview/workspace "
            "hydration events that are not in this durable log."
        ),
    })


@router.get("/{app_id}/sessions/{session_id}/images/{image_id}")
async def get_session_image(
    request: Request, app_id: str, session_id: str, image_id: str,
):
    """Serve a session image by ID. Returns raw image bytes."""
    from fastapi.responses import Response
    from digitorn.core.image_store import get_image_store

    store = get_image_store()
    ref = store.get_ref(image_id, session_id)
    if ref is None:
        raise HTTPException(status_code=404, detail="Image not found")

    data = await store.get(image_id, session_id)
    if data is None:
        raise HTTPException(status_code=404, detail="Image data not found")

    return Response(
        content=data,
        media_type=ref.mime,
        headers={"Cache-Control": "public, max-age=86400"},
    )


@router.get("/{app_id}/sessions/{session_id}/memory", response_model=AppResponse)
async def get_session_memory(request: Request, app_id: str, session_id: str) -> AppResponse:
    """Get the current memory state for a session (goal, todos, facts).

    Lighter than full history — only the working memory snapshot.
    """
    _validate_id(app_id)
    _validate_id(session_id, "session_id")
    manager = _get_manager(request)
    if not _is_deployed(request, app_id):
        _raise_not_deployed(request, app_id)

    _uid = getattr(request.state, "user_id", None) or "local"
    session = await manager.get_session(app_id, session_id, user_id=_uid)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")

    # Memory snapshot from session
    memory = dict(session.memory_snapshot)

    # Also try to get live memory from the memory module
    deployed = _get_deployed(request, app_id)
    if deployed:
        mem_module = deployed.modules.get("memory")
        if mem_module and hasattr(mem_module, "store"):
            store = mem_module.store
            working = getattr(store, "working", None)
            if working:
                memory["goal"] = getattr(working, "goal", "") or ""
                memory["todos"] = [
                    t.to_dict() if hasattr(t, "to_dict") else str(t)
                    for t in getattr(working, "todos", [])
                ]
                memory["facts"] = [
                    {"content": f.content if hasattr(f, "content") else str(f)}
                    for f in getattr(working, "key_facts", [])
                ]

    return AppResponse(success=True, data=memory)


@router.get("/{app_id}/sessions/{session_id}/preview", response_model=AppResponse)
async def get_session_preview(request: Request, app_id: str, session_id: str) -> AppResponse:
    """Get the current preview snapshot for a session.

    Returns the full preview state: scalar state map, all resource
    channels, and the event ring buffer. Clients call this on connect
    (or reconnect) to hydrate the UI without replaying every event.
    """
    _validate_id(app_id)
    _validate_id(session_id, "session_id")
    if not _is_deployed(request, app_id):
        _raise_not_deployed(request, app_id)

    deployed = _get_deployed(request, app_id)
    if deployed is None:
        _raise_not_deployed(request, app_id)

    preview_mod = deployed.modules.get("preview")
    if preview_mod is None:
        return AppResponse(success=True, data={"state": {}, "resources": {}})

    user_id = getattr(request.state, "user_id", None)
    snapshot = preview_mod.snapshot_for(session_id, user_id=user_id)
    return AppResponse(success=True, data=snapshot)


@router.get("/{app_id}/sessions/{session_id}/queue", response_model=AppResponse)
async def list_session_queue(
    request: Request, app_id: str, session_id: str,
    include_finished: bool = False,
) -> AppResponse:
    """List pending + running messages for this session.

    Use ``include_finished=true`` to also see recently completed /
    failed / cancelled entries (kept briefly for tracking).
    """
    _validate_id(app_id)
    _validate_id(session_id, "session_id")
    if not _is_deployed(request, app_id):
        _raise_not_deployed(request, app_id)
    # BUG-076: reject anonymous / cross-user queue peeking.
    await _require_session_access(request, app_id, session_id)
    from digitorn.core.app import message_queue as _mq
    entries = await _mq.list_for_session(session_id, include_finished=include_finished)
    return AppResponse(success=True, data={
        "session_id": session_id,
        "entries": [e.to_dict() for e in entries],
        "total": len(entries),
    })


@router.get(
    "/{app_id}/sessions/{session_id}/active-ops",
    response_model=AppResponse,
)
async def list_active_ops(
    request: Request, app_id: str, session_id: str,
) -> AppResponse:
    """List non-terminal operations for a session.

    This is the reconnect probe. Every event the daemon emits carries
    ``{op_id, op_type, op_state}`` (see :mod:`digitorn.core.events.envelope`),
    so the current state of any in-flight tool / sub-agent / approval /
    compaction / turn can be reconstructed by grouping ``session_events``
    rows by ``op_id`` and keeping the latest. This route does that
    group-by server-side and returns ONLY the ops whose latest
    ``op_state`` is still non-terminal (``pending``, ``running``,
    ``waiting_approval``).

    Typical client flow after a reconnect::

        # 1. replay everything since last known seq (gives the turn
        #    timeline for rendering bubbles, tool chips, thinking, …)
        GET /sessions/{sid}/events?since_seq=<last>

        # 2. ask the server what's still alive right now
        GET /sessions/{sid}/active-ops
        → [{op_id, op_type, op_state, started_at, last_ts, ...}, ...]

    Without (2), a client that disconnected DURING a long tool call
    has to guess. With (2), the spinner is restored instantly.
    """
    _validate_id(app_id)
    _validate_id(session_id, "session_id")
    if not _is_deployed(request, app_id):
        _raise_not_deployed(request, app_id)
    # Cross-user / anonymous access → 404, same contract as other
    # per-session endpoints.
    await _require_session_access(request, app_id, session_id)

    from digitorn.core.database import get_session_factory
    from digitorn.core.models import HistoryLog
    from digitorn.core.events.envelope import TERMINAL_STATES, OpState
    from sqlalchemy import select

    user_id = getattr(request.state, "user_id", "") or ""
    terminal_names = {s.value for s in TERMINAL_STATES}

    # Scan persisted events, group by op_id, keep the latest.
    ops: dict[str, dict[str, Any]] = {}
    sf = get_session_factory()
    async with sf() as db:
        stmt = (
            select(HistoryLog)
            .where(HistoryLog.kind == "event")
            .where(HistoryLog.session_id == session_id)
            .where(HistoryLog.user_id == user_id)
            .order_by(HistoryLog.seq.asc())
        )
        r = await db.execute(stmt)
        rows = r.scalars().all()

    for row in rows:
        payload = row.payload or {}
        op_id = payload.get("op_id") or row.correlation_id
        if not op_id:
            continue
        op_type = payload.get("op_type")
        op_state = payload.get("op_state")
        # Backward-compat: old rows without the contract — infer from
        # type so the endpoint still gives something useful during the
        # migration window.
        if not op_type or not op_state:
            from digitorn.core.events.envelope import (
                _LEGACY_OP_TYPE, _LEGACY_OP_STATE,
            )
            _ot = _LEGACY_OP_TYPE.get(row.type)
            _os = _LEGACY_OP_STATE.get(row.type)
            op_type = op_type or (_ot.value if _ot else "system")
            op_state = op_state or (_os.value if _os else "running")
        entry = ops.setdefault(op_id, {
            "op_id": op_id,
            "op_type": op_type,
            "op_state": op_state,
            "op_parent_id": payload.get("op_parent_id"),
            "first_seq": row.seq,
            "started_at": row.ts.isoformat() if row.ts else None,
            "last_seq": row.seq,
            "last_ts": row.ts.isoformat() if row.ts else None,
            "last_type": row.type,
            "correlation_id": row.correlation_id or None,
        })
        # Later events update the running view of the op.
        entry["op_state"] = op_state
        entry["last_seq"] = row.seq
        entry["last_ts"] = row.ts.isoformat() if row.ts else None
        entry["last_type"] = row.type
        if payload.get("op_parent_id"):
            entry["op_parent_id"] = payload["op_parent_id"]

    active = [e for e in ops.values() if e["op_state"] not in terminal_names]
    # Stable ordering: oldest-first by first_seq so the client can
    # render them in the order they started.
    active.sort(key=lambda e: (e.get("first_seq") or 0))

    return AppResponse(success=True, data={
        "app_id": app_id,
        "session_id": session_id,
        "active_ops": active,
        "count": len(active),
        "terminal_states": sorted(terminal_names),
        "scanned_events": len(rows),
    })


@router.get(
    "/{app_id}/sessions/{session_id}/context-breakdown",
    response_model=AppResponse,
)
async def get_context_breakdown(
    request: Request, app_id: str, session_id: str,
) -> AppResponse:
    """Debug: what's eating the session's context window right now.

    Returns a token-estimate per injection surface:

    - ``system_prompt`` — the full prompt the LLM sees (identity, tool
      instructions, behavioral guidelines, setup_summary, skills,
      module sections).
    - ``tools_schema`` — JSON schema of every tool (in-schema tokens).
    - ``messages`` — everything in ``ConversationSession.messages``
      (system + user + assistant + tool).
    - ``memory_injected`` — the memory module's rendered prompt
      section (goal, todos, facts).
    - ``setup_summary`` — setup step outputs injected at bootstrap.
    - ``skills`` — skill .md content concatenated.
    - ``total`` — sum matching what ``_call_llm`` actually sends.

    Use this when hitting context overflows to identify the offender.
    """
    _validate_id(app_id)
    _validate_id(session_id, "session_id")
    manager = _get_manager(request)
    if not _is_deployed(request, app_id):
        _raise_not_deployed(request, app_id)

    # BUG-076: reject anonymous / cross-user context-breakdown peeking.
    session = await _require_session_access(request, app_id, session_id)

    deployed = _get_deployed(request, app_id)
    if deployed is None:
        _raise_not_deployed(request, app_id)

    from digitorn.core.runtime.compaction import estimate_tokens
    ctx = deployed.entry_context

    def _est(text: str | None) -> int:
        if not text:
            return 0
        return max(1, len(text) // 3)

    # 1. Messages (what's in the session)
    msg_tokens = estimate_tokens(session.messages or [])

    # 2. System prompt — reconstruct what the agent_loop uses
    try:
        from digitorn.core.runtime.messages import to_chat_messages
        sys_prompt = ctx.system_prompt or ""
    except Exception:
        sys_prompt = ""
    sys_tokens = _est(sys_prompt)

    # 3. Tools schema
    tools = ctx.tools or []
    tools_json = ""
    try:
        import json as _json
        tools_json = _json.dumps(tools, default=str)
    except Exception:
        pass
    tools_tokens = _est(tools_json)
    if not tools_tokens:
        # Fallback: 90 tokens per tool heuristic
        tools_tokens = len(tools) * 90

    # 4. Memory injected section
    mem_tokens = 0
    try:
        mem = ctx.memory_module
        if mem is not None and hasattr(mem, "get_prompt_sections"):
            sections = mem.get_prompt_sections(session_id=session_id) or []
            total_text = "\n".join(
                s.get("content", "") if isinstance(s, dict) else str(s)
                for s in sections
            )
            mem_tokens = _est(total_text)
    except Exception:
        pass

    # 5. Setup summary
    setup_text = ""
    try:
        setup = ctx.setup_summary or {}
        if isinstance(setup, dict):
            import json as _json
            setup_text = _json.dumps(setup, default=str)
    except Exception:
        pass
    setup_tokens = _est(setup_text)

    # 6. Skills content
    skills_text = ""
    try:
        agent = getattr(ctx, "agent_def", None)
        if agent is not None:
            skills_text = getattr(agent, "skills_content", "") or ""
    except Exception:
        pass
    skills_tokens = _est(skills_text)

    # 7. Context config
    cc = ctx.context_config
    max_tokens = cc.max_tokens if cc else 131072
    output_reserved = cc.output_reserved if cc else 8192
    effective = max_tokens - output_reserved

    total = sys_tokens + tools_tokens + msg_tokens
    pressure = total / max(effective, 1)

    return AppResponse(success=True, data={
        "session_id": session_id,
        "context_window": max_tokens,
        "output_reserved": output_reserved,
        "effective_max": effective,
        "total_estimated": total,
        "pressure": round(pressure, 4),
        "budget_remaining": max(0, effective - total),
        "will_overflow": total > effective,
        "breakdown": {
            "system_prompt": sys_tokens,
            "tools_schema": tools_tokens,
            "messages": msg_tokens,
            # Informational — already counted inside system_prompt:
            "_memory_injected": mem_tokens,
            "_setup_summary": setup_tokens,
            "_skills": skills_tokens,
        },
        "tool_count": len(tools),
        "message_count": len(session.messages or []),
        "advice": _context_advice(total, effective, sys_tokens, tools_tokens, msg_tokens, mem_tokens),
    })


@router.delete(
    "/{app_id}/sessions/{session_id}/queue/{entry_id}",
    response_model=AppResponse,
)
async def cancel_queued_message(
    request: Request, app_id: str, session_id: str, entry_id: str,
) -> AppResponse:
    """Cancel a queued (not yet running) message. Running messages must
    be aborted via ``POST /abort``."""
    _validate_id(app_id)
    _validate_id(session_id, "session_id")
    _validate_id(entry_id, "entry_id")
    from digitorn.core.app import message_queue as _mq
    ok = await _mq.cancel(session_id, entry_id)
    if ok:
        manager = _get_manager(request)
        _uid = getattr(request.state, "user_id", None) or "local"
        bus_key = manager.event_bus.session_key(app_id, session_id, _uid)
        try:
            from digitorn.core.events.envelope import OpState as _OS
            # The entry being cancelled may not have a correlation_id
            # (it was still queued, never dispatched). We use entry_id
            # as the op_id so the queued-but-cancelled entry stays
            # groupable even without a turn correlation.
            _uid = getattr(request.state, "user_id", "") or "local"
            await manager.event_bus.emit(_turn_event(
                "message_cancelled",
                app_id=app_id, session_id=session_id, user_id=_uid,
                correlation_id=entry_id,
                op_state=_OS.CANCELLED,
                payload={"entry_id": entry_id, "session_id": session_id},
            ))
        except Exception:
            pass
    return AppResponse(
        success=ok,
        data={"cancelled": ok, "entry_id": entry_id},
        error=None if ok else "entry not found or already running",
    )


@router.post(
    "/{app_id}/sessions/{session_id}/queue/clear",
    response_model=AppResponse,
)
async def clear_session_queue(
    request: Request, app_id: str, session_id: str,
) -> AppResponse:
    """Cancel every queued (non-running) message for this session."""
    _validate_id(app_id)
    _validate_id(session_id, "session_id")
    from digitorn.core.app import message_queue as _mq
    n = await _mq.clear(session_id)
    manager = _get_manager(request)
    _uid = getattr(request.state, "user_id", None) or "local"
    bus_key = manager.event_bus.session_key(app_id, session_id, _uid)
    try:
        from digitorn.core.events.envelope import OpState as _OS
        # ``queue_cleared`` is a session-level event, not tied to any
        # specific turn. Synthesise a stable op_id based on session so
        # repeated clears group together in the client timeline.
        _uid = getattr(request.state, "user_id", "") or "local"
        await manager.event_bus.emit(_turn_event(
            "queue_cleared",
            app_id=app_id, session_id=session_id, user_id=_uid,
            correlation_id=f"queue-{session_id}",
            op_state=_OS.COMPLETED,
            payload={"session_id": session_id, "cancelled": n},
        ))
    except Exception:
        pass
    return AppResponse(success=True, data={"cancelled": n})


@router.get("/{app_id}/sessions/{session_id}/state", response_model=AppResponse)
async def session_state(
    request: Request, app_id: str, session_id: str,
    since_seq: int = 0,
) -> AppResponse:
    """Authoritative session state envelope — client's source of truth.

    The envelope describes everything the client needs to render the
    chat UI correctly: whether a turn is running, its correlation_id,
    its phase / progress / timing, the queue of pending messages, and
    whether the session has been compacted.

    Clients treat the envelope as ground truth. They call this whenever
    they suspect drift:
    - On (re)mount of the chat panel
    - On Socket.IO reconnect
    - When a watchdog detects no events for >10s while a turn is active
    - On demand (debug / refresh button)

    Optional ``since_seq`` returns events emitted since that seq on the
    same session, alongside the envelope. The client stitches them in
    order and updates ``last_seen_seq`` to ``envelope.seq``.
    """
    _validate_id(app_id)
    _validate_id(session_id, "session_id")
    manager = _get_manager(request)
    if not _is_deployed(request, app_id):
        raise HTTPException(status_code=404, detail=f"App '{app_id}' not deployed")

    _uid = getattr(request.state, "user_id", None) or "local"
    envelope = await manager.build_state_envelope(app_id, session_id, _uid)

    # Optional gap-fill — replay events persisted after ``since_seq``.
    # Only events (kind='event') are replayed; messages live on the
    # history endpoint. Capped at 1000 rows — clients who lag further
    # than that should fetch the full history instead.
    gap_events: list[dict[str, Any]] = []
    if since_seq > 0:
        try:
            from digitorn.core.database import get_session_factory
            from digitorn.core.models import HistoryLog
            from sqlalchemy import select
            factory = get_session_factory()
            async with factory() as db:
                rows = (await db.execute(
                    select(HistoryLog)
                    .where(HistoryLog.kind == "event")
                    .where(HistoryLog.session_id == session_id)
                    .where(HistoryLog.seq > since_seq)
                    .order_by(HistoryLog.seq.asc())
                    .limit(1000)
                )).scalars().all()
                for r in rows:
                    gap_events.append({
                        "seq": int(r.seq),
                        "type": r.type,
                        "kind": r.kind,
                        "app_id": r.app_id,
                        "session_id": r.session_id,
                        "user_id": r.user_id,
                        "correlation_id": r.correlation_id or None,
                        "payload": r.payload or {},
                        "ts": r.ts.isoformat() if r.ts else None,
                    })
        except Exception as exc:
            logger.debug(
                "gap_fill_failed sid=%s since=%d: %s",
                session_id, since_seq, exc,
            )

    envelope["gap_events"] = gap_events
    return AppResponse(success=True, data=envelope)

