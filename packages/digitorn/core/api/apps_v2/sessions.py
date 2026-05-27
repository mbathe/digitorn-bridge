"""Routes for the sessions group."""

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


# token-count cache for cold-path context estimates; key includes message count so it auto-invalidates on append.
_CTX_TOKEN_CACHE: dict[tuple[str, int], tuple[int, int, int]] = {}

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, model_validator


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
    _serialise_widget_node,
    _serialise_widgets,
    _execute_widget_tool,
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


def _read_session_compactions(app_id: str, session_id: str) -> int:
    """Return the cumulative number of compactions that fired on a"""
    try:
        from digitorn.core.runtime.session_metrics import _sessions
    except Exception:
        return 0
    total = 0
    for try_app in (app_id, "default"):
        prefix = f"{try_app}:{session_id}:"
        for key, entry in _sessions.items():
            if key.startswith(prefix):
                try:
                    total += int(getattr(entry.context, "compactions", 0) or 0)
                except Exception as exc:
                    logger.debug("sessions best-effort block failed: %s", exc)
    return total


@router.post("/{app_id}/sessions", response_model=AppResponse)
async def create_session(
    request: Request, app_id: str,
    body: CreateSessionRequest,
) -> AppResponse:
    """Create a new conversation session AND dispatch its first message."""
    _validate_id(app_id)
    manager = _get_manager(request)
    if not _is_deployed(request, app_id):
        _raise_not_deployed(request, app_id)

    import uuid as _uuid
    from datetime import datetime, timezone
    from pathlib import Path as _Path

    user_id = getattr(request.state, "user_id", None) or "local"
    session_id = str(_uuid.uuid4())

    user_workdir = (body.workdir or body.workspace_path or "").strip()

    deployed_for_check = _get_deployed(request, app_id)
    workdir_mode = "auto"
    if deployed_for_check is not None:
        compiled_exec = deployed_for_check.compiled.execution
        workdir_mode = (
            getattr(compiled_exec, "workdir_mode", None)
            or getattr(compiled_exec, "workspace_mode", "auto")
        )
        if workdir_mode == "required" and not user_workdir:
            raise HTTPException(
                status_code=400,
                detail={
                    "error": "workdir_required",
                    "message": (
                        f"App '{app_id}' requires a working directory. "
                        f"Pass `workdir` (or legacy `workspace_path`) in "
                        f"the request body when creating the session."
                    ),
                    "workdir_mode": "required",
                },
            )

    if user_workdir:
        # Resolve to an absolute path: filesystem path → expanduser/resolve,
        # project slug → `~/.digitorn/workdirs/{app_id}/{user_id}/{slug}/`.
        from digitorn.core.workdirs import resolve_workdir
        try:
            p = resolve_workdir(app_id, user_id, user_workdir)
        except ValueError as exc:
            # Strict slug rejected - return a structured 400 the web
            # client can surface inline in the picker.
            raise HTTPException(
                status_code=400,
                detail={
                    "error": "invalid_workdir",
                    "message": str(exc),
                },
            )
        try:
            if not p.exists():
                p.mkdir(parents=True, exist_ok=True)
            if not p.is_dir():
                raise HTTPException(
                    status_code=400,
                    detail=f"workdir is not a directory: {p}",
                )
            user_workdir = str(p)
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(
                status_code=400,
                detail=f"workdir unusable: {exc}",
            )

    daemon_workspace = (
        _Path.home() / ".digitorn" / "workspaces" / app_id / session_id
    )
    try:
        daemon_workspace.mkdir(parents=True, exist_ok=True)
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"failed to create session workspace: {exc}",
        )
    workspace_str = str(daemon_workspace)
    effective_workdir = user_workdir or workspace_str

    # Create the session in the store so it appears in listings immediately
    from digitorn.core.app.sessions import ConversationSession
    session = ConversationSession(
        session_id=session_id,
        app_id=app_id,
        user_id=user_id,
        workspace=workspace_str,
        workdir=effective_workdir,
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

    context = {}
    if deployed:
        entry_ctx = deployed.entry_context
        system_prompt = entry_ctx.system_prompt or ""
        tools = entry_ctx.tools or []
        cc = entry_ctx.context_config
        provider = getattr(entry_ctx, "provider", None)
        model_name = getattr(provider, "model", None) if provider else None

        def _count_text(text: str) -> int:
            """Real token count for a free-form string."""
            if not text:
                return 0
            if provider is not None and hasattr(provider, "count_tokens"):
                try:
                    return int(provider.count_tokens(text))
                except Exception as exc:
                    logger.debug("sessions best-effort block failed: %s", exc)
            if model_name:
                try:
                    from litellm import token_counter
                    return int(token_counter(model=model_name, text=text))
                except Exception as exc:
                    logger.debug("sessions best-effort block failed: %s", exc)
            return max(1, len(text) // 4)

        sys_tokens = await asyncio.to_thread(_count_text, system_prompt)

        if tools:
            try:
                tools_json = _json.dumps(tools, ensure_ascii=False)
                tools_tokens = await asyncio.to_thread(_count_text, tools_json)
            except Exception:
                tools_tokens = 0
        else:
            tools_tokens = 0

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
            "compactions": _read_session_compactions(app_id, session_id),
            "model": getattr(provider, "model", "") if provider else "",
        }

    preview_url: str | None = None
    if deployed is not None:
        web_preview_mod = (deployed.modules or {}).get("web_preview")
        if web_preview_mod is not None:
            try:
                from digitorn.core.packages.resolver import resolve_app_install_dir
                pkg_registry = getattr(request.app.state, "package_registry", None)
                install_dir = await resolve_app_install_dir(
                    app_id, user_id=user_id if user_id != "local" else None,
                    registry=pkg_registry,
                )
                if install_dir is not None:
                    web_preview_mod.auto_attach_bundled_dist(
                        session_id=session_id,
                        app_id=app_id,
                        install_dir=str(install_dir),
                        user_id=user_id,
                    )
            except Exception as exc:
                logger.debug(
                    "auto_attach_bundled_dist skipped app=%s: %s",
                    app_id, exc,
                )

        has_preview = (
            "preview" in (deployed.modules or {})
            or web_preview_mod is not None
        )
        if has_preview:
            preview_url = (
                f"/api/apps/{app_id}/web-preview"
                f"?session_id={session_id}&name=default"
            )

    # Dispatch the first message through the same code path as a
    # follow-up message; tear down the session if dispatch fails.
    from digitorn.core.api.apps_v2.messages import session_send_message
    smr = SessionMessageRequest(
        message=body.message,
        workspace=effective_workdir or None,
        images=body.images,
        files=body.files,
        queue_mode=body.queue_mode,
        client_message_id=body.client_message_id,
        mode=body.mode,
        template_id=body.template_id,
        system_addendum=body.system_addendum,
    )
    try:
        dispatch_resp = await session_send_message(
            request, app_id, session_id, smr,
        )
    except HTTPException:
        try:
            await asyncio.to_thread(
                manager._session_store.delete, app_id, session_id,
                user_id=user_id,
            )
        except Exception as exc:
            logger.warning(
                "create_session rollback failed app=%s sid=%s: %s",
                app_id, session_id, exc,
            )
        raise

    dispatch_data = dispatch_resp.data if dispatch_resp.success else {}

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
        # `workspace` mirrors `workdir` so legacy clients keep
        # working; the daemon-private dir never surfaces to the UI.
        "workspace": effective_workdir,
        "workdir": effective_workdir,
        "first_message": {
            "correlation_id": dispatch_data.get("correlation_id"),
            "status": dispatch_data.get("status"),
            "position": dispatch_data.get("position"),
            "queue_depth": dispatch_data.get("queue_depth"),
            # Non-null only when the daemon dispatched immediately;
            # without a seq the client must not render the bubble.
            "seq": dispatch_data.get("seq"),
            "state": dispatch_data.get("state"),
        },
    })


@router.get("/{app_id}/sessions", response_model=AppResponse)
async def list_sessions(
    request: Request,
    app_id: str,
    limit: int = 50,
    offset: int = 0,
) -> AppResponse:
    """List sessions for an app with pagination."""
    _validate_id(app_id)
    limit = max(0, min(limit, 200))
    offset = max(0, offset)
    manager = _get_manager(request)
    if not _is_deployed(request, app_id):
        _raise_not_deployed(request, app_id)
    user_id = getattr(request.state, "user_id", None)
    total = await manager.count_sessions(app_id, user_id=user_id)
    sessions = await manager.list_sessions(app_id, user_id=user_id, limit=limit, offset=offset)
    from pathlib import Path as _Path
    for s in sessions:
        s["is_active"] = manager.is_session_active(app_id, s.get("session_id", ""))
        wd = s.get("workdir") or s.get("workspace") or ""
        s["workdir_exists"] = bool(wd) and _Path(wd).exists()
    return AppResponse(success=True, data={
        "sessions": sessions,
        "total": total,
        "limit": limit,
        "offset": offset,
    })


@router.get("/{app_id}/projects", response_model=AppResponse)
async def list_app_projects(
    request: Request,
    app_id: str,
) -> AppResponse:
    """List the caller's named projects (slug workdirs) for this app."""
    _validate_id(app_id)
    if not _is_deployed(request, app_id):
        _raise_not_deployed(request, app_id)
    user_id = getattr(request.state, "user_id", None) or "local"
    from digitorn.core.workdirs import list_user_projects
    slugs = list_user_projects(app_id, user_id)
    return AppResponse(
        success=True,
        data={
            "projects": [{"slug": s} for s in slugs],
            "count": len(slugs),
        },
    )


@router.get("/{app_id}/sessions/search", response_model=AppResponse)
async def search_sessions(
    request: Request,
    app_id: str,
    q: str = "",
    limit: int = 20,
    offset: int = 0,
) -> AppResponse:
    """Search across all sessions - matches title and message content."""
    _validate_id(app_id)
    manager = _get_manager(request)
    if not _is_deployed(request, app_id):
        _raise_not_deployed(request, app_id)

    if not q or not q.strip():
        return AppResponse(success=True, data={"sessions": [], "total": 0, "query": q})

    query = q.strip().lower()
    user_id = getattr(request.state, "user_id", None)

    # Cap the candidate scan: the per-message `load_messages` call is
    # sequential and disk-bound; recent sessions cover the typical hits.
    _MAX_SESSIONS_SCANNED = 50
    if user_id:
        all_sessions = await manager.list_sessions(
            app_id, user_id=user_id, limit=_MAX_SESSIONS_SCANNED,
        )
    else:
        all_sessions = await manager.list_sessions(
            app_id, limit=_MAX_SESSIONS_SCANNED,
        )

    # Title pass first -- cheap, fully in-memory, never reads disk.
    matches: list[tuple[int, dict]] = []
    needs_message_scan: list[dict] = []
    for s in all_sessions:
        title = s.get("title", "")
        snippets: list[str] = []
        score = 0
        if query in title.lower():
            score = 10
            snippets.append(f"title: {title[:100]}")
        if score > 0:
            s["relevance"] = score
            s["snippets"] = snippets
            matches.append((score, s))
        needs_message_scan.append(s)

    async def _scan_session_messages(s: dict) -> dict | None:
        sid = s.get("session_id", "")
        uid = s.get("user_id", user_id or "local")
        try:
            messages = await asyncio.to_thread(
                manager._session_store.load_messages,
                app_id, sid, user_id=uid,
            )
        except Exception:
            return None
        if not messages:
            return None
        score_delta = 0
        snippets: list[str] = []
        for i, msg in enumerate(messages[:10]):
            content = (msg.get("content", "") or "")[:500].lower()
            if query in content:
                score_delta += 5 if i < 2 else 1
                idx = content.find(query)
                start = max(0, idx - 40)
                end = min(len(content), idx + len(query) + 40)
                snippets.append(f"message[{i}]: ...{content[start:end].strip()}...")
                if len(snippets) >= 3:
                    break
        if score_delta > 0:
            existing_score = int(s.get("relevance", 0) or 0)
            s["relevance"] = existing_score + score_delta
            s.setdefault("snippets", []).extend(snippets)
            s["snippets"] = s["snippets"][:3]
            return s
        return None

    msg_results = await asyncio.gather(*[
        _scan_session_messages(s) for s in needs_message_scan
    ], return_exceptions=True)
    for r in msg_results:
        if isinstance(r, dict):
            # Replace an existing title-only hit with the enriched one,
            # or add a new content-only match.
            existing = next(
                (m for m in matches if m[1].get("session_id") == r.get("session_id")),
                None,
            )
            if existing:
                matches.remove(existing)
            matches.append((int(r.get("relevance", 0)), r))

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
    """Get session status - metadata, live metrics, context pressure."""
    _validate_id(app_id)
    _validate_id(session_id, "session_id")
    manager = _get_manager(request)
    if not _is_deployed(request, app_id):
        _raise_not_deployed(request, app_id)
    # Uniform session-access gate: anonymous → 401, cross-user → 404,
    # owner → returns the session.
    session = await _require_session_access(request, app_id, session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found or expired")

    data = session.summary()

    # is_active: True if an agent turn is in progress right now
    data["is_active"] = manager.is_session_active(app_id, session_id)

    # Active composer mode so the client can rehydrate its picker.
    try:
        from digitorn.core.runtime.session_store import get_default_bridge
        _state = get_default_bridge().store.state(session_id)
        if _state is not None:
            data["active_mode_id"] = getattr(_state, "active_mode_id", None)
    except Exception as exc:
        logger.debug("sessions best-effort block failed: %s", exc)

    try:
        from digitorn.core.runtime.session_metrics import (
            get_session_metrics, _sessions, session_total,
        )
        # Find the main agent entry (used for context window snapshot
        # and turn_number - those are coordinator-side fields).
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

        rollup = session_total(app_id, session_id)
        if rollup.get("agents", 0) <= 1:
            data["tokens"] = {
                "prompt": sm.prompt_tokens,
                "completion": sm.completion_tokens,
                "total": sm.total_tokens,
            }
        else:
            # Multi-agent session: report cumulative + per-agent breakdown.
            data["tokens"] = {
                "prompt": rollup["tokens_prompt"],
                "completion": rollup["tokens_completion"],
                "total": rollup["tokens_total"],
            }
            data["agents_active"] = rollup["agents"]
            data["agents_breakdown"] = rollup.get("agent_ids", [])
            data["llm_calls_total"] = rollup["llm_calls"]
            data["turns_total"] = rollup["turns"]
        ctx_snapshot = sm.context.snapshot()

        # If context is empty (no turn yet), compute initial estimate from the deployed app
        if ctx_snapshot.get("total_estimated_tokens", 0) == 0:
            deployed = _get_deployed(request, app_id)
            if deployed:
                entry_ctx = deployed.entry_context
                system_prompt = entry_ctx.system_prompt or ""
                tools = entry_ctx.tools or []
                cc = entry_ctx.context_config
                _provider = getattr(entry_ctx, "provider", None)
                _model_name = getattr(_provider, "model", None) if _provider else None

                def _ct(text: str) -> int:
                    if not text:
                        return 0
                    if _provider is not None and hasattr(_provider, "count_tokens"):
                        try:
                            return int(_provider.count_tokens(text))
                        except Exception as exc:
                            logger.debug("sessions best-effort block failed: %s", exc)
                    if _model_name:
                        try:
                            from litellm import token_counter
                            return int(token_counter(model=_model_name, text=text))
                        except Exception as exc:
                            logger.debug("sessions best-effort block failed: %s", exc)
                    return max(1, len(text) // 4)

                _cache_key = (session_id, len(session.messages))
                _cached = _CTX_TOKEN_CACHE.get(_cache_key)
                if _cached is not None:
                    sys_tokens, tools_tokens, msg_tokens = _cached
                else:
                    def _ct_all() -> tuple[int, int, int]:
                        _sys = _ct(system_prompt)
                        if tools:
                            try:
                                _tt = _ct(_json.dumps(tools, ensure_ascii=False))
                            except Exception:
                                _tt = len(tools) * 90
                        else:
                            _tt = 0
                        _mt = sum(
                            _ct(m.get("content", ""))
                            for m in session.messages
                            if isinstance(m.get("content"), str)
                        )
                        return _sys, _tt, _mt
                    sys_tokens, tools_tokens, msg_tokens = await asyncio.to_thread(_ct_all)
                    # Bounded cache: 200 entries (multi-tab + multi-session
                    # bursts). Random-pop eviction when full -- cheap O(1).
                    if len(_CTX_TOKEN_CACHE) >= 200:
                        try:
                            _CTX_TOKEN_CACHE.pop(next(iter(_CTX_TOKEN_CACHE)))
                        except StopIteration:
                            pass
                    _CTX_TOKEN_CACHE[_cache_key] = (sys_tokens, tools_tokens, msg_tokens)
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
                    "compactions": _read_session_compactions(app_id, session_id),
                }

        data["context"] = ctx_snapshot
        # Tools / errors also aggregated when sub-agents are present so
        # the dashboard counter ticks up as Agent()-spawned workers run.
        if rollup.get("agents", 0) > 1:
            data["tools"] = {
                "total_calls": rollup["tool_calls_total"],
                "success": rollup["tool_calls_success"],
                "failed": rollup["tool_calls_failed"],
            }
            data["errors"] = {
                "total": rollup["errors"],
                "last": sm.last_error,  # last error stays from the main agent
            }
        else:
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
    cache = getattr(request.app.state, "workspace_cache", None)
    if cache is not None:
        try:
            cache.invalidate(session_id)
        except Exception as exc:
            logger.debug("workspace_cache_invalidate_failed sid=%s: %s", session_id, exc)
    return AppResponse(success=True, data={
        "session_id": session_id,
        "deleted": True,
    })


@router.post("/{app_id}/sessions/{session_id}/fork", response_model=AppResponse)
async def fork_session(request: Request, app_id: str, session_id: str) -> AppResponse:
    """Fork a session - create a new session with the same message history."""
    import uuid as _uuid
    _validate_id(app_id)
    _validate_id(session_id, "session_id")
    manager = _get_manager(request)
    if not _is_deployed(request, app_id):
        _raise_not_deployed(request, app_id)
    # reject cross-user fork (session theft).
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
        forked_from=session_id,
    )
    await asyncio.to_thread(manager._session_store.put, new_session)

    return AppResponse(success=True, data={
        "session_id": new_id,
        "source_session_id": session_id,
        "forked_from": session_id,
        "new_session_id": new_id,
        "forked": True,
        "message_count": len(new_session.messages),
    })


@router.post("/{app_id}/sessions/{session_id}/abort", response_model=AppResponse)
async def abort_session_turn(
    request: Request, app_id: str, session_id: str,
    purge_queue: bool = False,
) -> AppResponse:
    """Abort the currently running agent turn for a session."""
    _validate_id(app_id)
    _validate_id(session_id, "session_id")
    manager = _get_manager(request)
    if not _is_deployed(request, app_id):
        _raise_not_deployed(request, app_id)
    # reject cross-user aborts (destructive remote DoS).
    await _require_session_access(request, app_id, session_id)

    _uid = getattr(request.state, "user_id", None) or "local"

    was_active = manager.is_session_active(app_id, session_id)

    # Cancel the running asyncio task - this triggers CancelledError
    # inside _chat_locked which saves state + marks session.interrupted
    active_key = f"{app_id}:{session_id}"
    task = manager._session_tasks.get(active_key)
    task_cancelled = False
    if task is not None and not task.done():
        task.cancel()
        task_cancelled = True

    cooperative_signaled = 0
    try:
        active_ctxs = getattr(manager, "_active_contexts", None) or {}
        ctx_obj = active_ctxs.get(active_key)
        if ctx_obj is None:
            # Fallback to the deployed app's per-session context map.
            dep = _get_deployed(request, app_id)
            if dep is not None:
                sess_map = getattr(dep, "_session_contexts", None) or {}
                ctx_obj = sess_map.get(session_id)
        evt = getattr(ctx_obj, "cancel_event", None) if ctx_obj else None
        if evt is not None and not evt.is_set():
            try:
                ctx_obj.cancel_reason = "user_abort"
            except Exception as exc:
                logger.debug("sessions best-effort block failed: %s", exc)
            evt.set()
            cooperative_signaled = 1
    except Exception:
        logger.debug("abort: cooperative cancel set failed", exc_info=True)

    pending_approvals_cancelled = 0
    deployed = _get_deployed(request, app_id)
    if deployed:
        approval_queue = getattr(deployed, "approval_queue", None)
        if approval_queue is not None and hasattr(approval_queue, "cancel_all"):
            try:
                pending_approvals_cancelled = approval_queue.cancel_all()
            except Exception:
                logger.debug("abort: approval_queue.cancel_all failed", exc_info=True)

    bg_killed = 0
    agents_killed = 0
    if deployed:
        shell_mod = deployed.modules.get("shell")
        if shell_mod is not None and hasattr(shell_mod, "cleanup_session"):
            try:
                await shell_mod.cleanup_session(session_id)
                bg_killed = 1  # flag that cleanup ran
            except Exception:
                logger.debug("abort: shell cleanup_session failed", exc_info=True)

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

    queue_purged = 0
    from digitorn.core.app import message_queue as _mq
    try:
        entries = await _mq.list_for_session(session_id)
        for e in entries:
            if e.status == "running":
                try:
                    await _mq.mark_cancelled(e.id)
                    _mq.fail_awaiter(
                        e.correlation_id,
                        RuntimeError("aborted by user"),
                    )
                except Exception as exc:
                    logger.debug("sessions best-effort block failed: %s", exc)
                break
        if purge_queue:
            queue_purged = await _mq.clear(session_id)
    except Exception:
        logger.debug("abort: queue cleanup failed", exc_info=True)

    # Force-release the reservation: idempotent, covers the case
    # where `manager.chat()` never ran (pre-chat credential gate).
    try:
        manager.release_session(app_id, session_id)
    except Exception:
        logger.debug("abort: release_session failed", exc_info=True)

    # Mark the session interrupted so the next message takes the
    # smart-resume path (orphaned tool calls get synthetic results).
    if not task_cancelled:
        try:
            sess = await manager.get_session(
                app_id, session_id, user_id=_uid,
            )
            if sess is not None and not getattr(sess, "interrupted", False):
                sess.interrupted = True
                try:
                    import time as _time
                    sess.interrupted_at = _time.time()
                except Exception as exc:
                    logger.debug("sessions best-effort block failed: %s", exc)
                await asyncio.to_thread(
                    manager._session_store.put, sess,
                )
        except Exception:
            logger.debug("abort: persist interrupted flag failed", exc_info=True)

    # Signal abort via the event bus (Socket.IO clients see it immediately)
    try:
        from digitorn.core.events.envelope import OpState as _OS
        await manager.event_bus.emit(_turn_event(
            "abort",
            app_id=app_id, session_id=session_id, user_id=_uid or "local",
            correlation_id=f"abort-{session_id}",
            op_state=_OS.CANCELLED,
            payload={
                "session_id": session_id,
                "queue_purged": queue_purged,
                "queue_preserved": not purge_queue,
                "task_cancelled": task_cancelled,
                "approvals_cancelled": pending_approvals_cancelled,
            },
        ))
    except Exception as exc:
        logger.debug("sessions best-effort block failed: %s", exc)

    if not purge_queue:
        try:
            async def _resume() -> None:
                await asyncio.sleep(0.2)
                try:
                    await _drain_queue_next(
                        request, app_id, session_id, _uid,
                    )
                except Exception as exc:
                    logger.warning("abort_resume_failed: %s", exc)
            _resume_task = asyncio.create_task(_resume())
            _active_turn_tasks.add(_resume_task)
            _resume_task.add_done_callback(_active_turn_tasks.discard)
        except Exception as exc:
            logger.debug("sessions best-effort block failed: %s", exc)

    return AppResponse(success=True, data={
        "session_id": session_id,
        "was_active": was_active,
        "aborted": True,
        "task_cancelled": task_cancelled,
        "bg_tasks_cleaned": bg_killed > 0,
        "agents_cleaned": agents_killed > 0,
        "approvals_cancelled": pending_approvals_cancelled,
        "queue_purged": queue_purged,
        "queue_preserved": not purge_queue,
        "interrupted": True,
    })


@router.post("/{app_id}/sessions/{session_id}/resume", response_model=AppResponse)
async def resume_session(request: Request, app_id: str, session_id: str) -> AppResponse:
    """Resume an interrupted session automatically."""
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
    await asyncio.to_thread(_Path(latest_path).write_bytes, content)
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
        result = await asyncio.wait_for(
            emergency_compact(
                ctx, messages,
                reason="manual",
                event_bus=manager.event_bus,
                app_id=app_id,
                session_id=session_id,
                user_id=_uid,
            ),
            timeout=60.0,
        )
    except asyncio.TimeoutError:
        raise HTTPException(
            status_code=504,
            detail="Compaction timed out after 60s -- check daemon logs",
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Compaction failed: {exc}")

    durable_result: dict[str, Any] = {
        "compact_session_called": False,
        "cutoff_seq": 0,
    }
    if result.get("compacted") and result.get("to_compact_count", 0) > 0:
        try:
            from digitorn.core.runtime.session_store.bridge import (
                get_default_bridge,
            )
            bridge = get_default_bridge()
            if bridge is not None:
                store_state = bridge.store.state(session_id)
                if store_state is not None and store_state.messages:
                    keep_count = int(result["to_keep_count"])
                    state_msgs = store_state.messages
                    cutoff_idx = max(0, len(state_msgs) - keep_count)
                    if cutoff_idx > 0:
                        cutoff_seq = int(state_msgs[cutoff_idx - 1].seq)
                        await bridge.store.compact_session(
                            session_id,
                            cutoff_seq=cutoff_seq,
                            summary=(
                                f"[Context compacted: "
                                f"{result['to_compact_count']} older "
                                f"messages removed, "
                                f"{int(result.get('tokens_before', 0))} -> "
                                f"{int(result.get('tokens_after', 0))} tokens]"
                            ),
                            strategy="truncate",
                            tokens_estimate=int(result.get("tokens_after", 0)),
                            model=str(getattr(ctx, "model", "") or ""),
                        )
                        durable_result["compact_session_called"] = True
                        durable_result["cutoff_seq"] = cutoff_seq
        except Exception as exc:
            logger.warning(
                "compact_session durable write failed sid=%s err=%s",
                session_id, exc,
            )

    after = len(messages)
    return AppResponse(success=True, data={
        "before": before,
        "after": after,
        "freed": before - after,
        "tokens_before": int(result.get("tokens_before", 0)),
        "tokens_after": int(result.get("tokens_after", 0)),
        "reason": str(result.get("reason", "manual")),
        "durable": durable_result,
    })


@router.get("/{app_id}/sessions/{session_id}/export", response_model=AppResponse)
async def export_session(
    request: Request,
    app_id: str,
    session_id: str,
    format: str = "markdown",
) -> AppResponse:
    """Export a session as Markdown (or other formats)."""
    _validate_id(app_id)
    _validate_id(session_id, "session_id")
    manager = _get_manager(request)
    if not _is_deployed(request, app_id):
        _raise_not_deployed(request, app_id)

    # reject anonymous or cross-user export (data exfil).
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
    before_seq: int | None = None,
    events_limit: int = 50000,
) -> AppResponse:
    """Full chronological history for a session - every message AND"""
    _validate_id(app_id)
    _validate_id(session_id, "session_id")
    manager = _get_manager(request)
    if not _is_deployed(request, app_id):
        _raise_not_deployed(request, app_id)
    user_id = getattr(request.state, "user_id", None)
    session = await manager.get_session(app_id, session_id, user_id=user_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found or expired")

    try:
        from digitorn.core.runtime.session_store.bridge import (
            get_default_bridge,
        )
        from digitorn.core.runtime.session_store.history_view import (
            render_history_payload,
        )
        bridge = get_default_bridge()
        store_state = await bridge.store.open(
            session_id, app_id=app_id, user_id=user_id or "",
            create_if_missing=False, pin=False,
        ) if bridge is not None else None
    except KeyError:
        store_state = None
    except Exception as exc:
        logger.debug("history_view load failed sid=%s: %s", session_id, exc)
        store_state = None

    if store_state is not None:
        history_data = render_history_payload(
            store_state,
            include_system=include_system,
            since_seq=since_seq,
            before_seq=before_seq,
            events_limit=events_limit,
        )
        # Stamp the daemon instance id so clients detect a restart
        # (the UUID changes on every Python process spawn) and reseed.
        from digitorn.core.instance import get_instance_id as _get_inst
        history_data["instance_id"] = _get_inst()
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
            **history_data,
            "turn_active": turn_active,
            "pending_queue": [e.to_dict() for e in pending_entries],
        }
        try:
            from digitorn.core.app.manager_v2._session import (
                _aggregate_gateway_usage,
            )
            totals = await _aggregate_gateway_usage(app_id, [session_id])
            t = totals.get(session_id)
            if t:
                data["tokens"] = {
                    "prompt": t["prompt_tokens"],
                    "completion": t["completion_tokens"],
                    "total": t["prompt_tokens"] + t["completion_tokens"],
                }
                data["cost_usd"] = float(t["cost_usd"])
        except Exception:
            logger.debug("history token hydrate failed", exc_info=True)
        if session.memory_snapshot:
            data["memory_snapshot"] = session.memory_snapshot
        return AppResponse(success=True, data=data)

    db_messages: list[dict[str, Any]] | None = None
    try:
        from digitorn.core.runtime.persist_worker import get_default_worker

        async def _load_messages_from_history_log() -> list[dict[str, Any]]:
            from digitorn.core.database import get_session_factory
            from digitorn.core.models import HistoryLog
            from sqlalchemy import select as _select
            _factory = get_session_factory()
            async with _factory() as _db:
                _rows = (await _db.execute(
                    _select(HistoryLog)
                    .where(HistoryLog.kind == "message")
                    .where(HistoryLog.session_id == session_id)
                    .order_by(HistoryLog.seq.asc(), HistoryLog.ts.asc())
                )).scalars().all()
                out: list[dict[str, Any]] = []
                for _r in _rows:
                    _payload = _r.payload or {}
                    _msg: dict[str, Any] = {
                        "role": _r.role or "",
                        "content": _r.content or "",
                        "seq": _r.seq,
                        "tool_call_id": _r.tool_call_id,
                        "tool_calls": _r.tool_calls or [],
                    }
                    _thinking = _payload.get("thinking")
                    if isinstance(_thinking, str) and _thinking:
                        _msg["thinking"] = _thinking
                    out.append(_msg)
                return out

        db_messages = await get_default_worker().run_async(
            _load_messages_from_history_log,
        )
    except Exception as exc:
        logger.debug(
            "history seq-aware load failed, falling back to in-memory "
            "session.messages (turns will not carry seq): %s", exc,
        )
        db_messages = None

    if include_system:
        turns = db_messages if db_messages is not None else session.messages
    else:
        # Prefer DB-sourced messages (seq-bearing); fall back to in-memory.
        turns = _build_history_turns(
            db_messages if db_messages is not None else session.messages,
        )

    # Full chronology from the unified `history_log` table: one SQL
    # query returns messages + events interleaved in temporal order.
    events = []
    events_total = 0
    events_next_seq = int(since_seq or 0)
    events_has_more = False
    events_prev_seq = 0
    events_has_more_back = False
    # Backward pagination: `before_seq` (0 = sentinel for "end")
    # returns the latest events first. Powers infinite-scroll-up.
    backward_mode = before_seq is not None
    # Route the entire history_log query block through persist_worker
    # so asyncpg never touches the main loop (Windows + SSL stalls).
    try:
        from digitorn.core.runtime.persist_worker import get_default_worker

        async def _load_history_log_events() -> dict[str, Any]:
            from digitorn.core.database import get_session_factory
            from digitorn.core.models import HistoryLog
            from sqlalchemy import select, func, and_
            factory = get_session_factory()
            _events: list[dict[str, Any]] = []
            _events_total = 0
            _events_next_seq = int(since_seq or 0)
            _events_has_more = False
            _events_prev_seq = 0
            _events_has_more_back = False
            async with factory() as db:
                _events_total = int((await db.execute(
                    select(func.count())
                    .select_from(HistoryLog)
                    .where(HistoryLog.kind == "event")
                    .where(HistoryLog.session_id == session_id)
                )).scalar() or 0)

                if backward_mode:
                    upper = int(before_seq or 0)
                    count_stmt = (
                        select(HistoryLog.seq)
                        .where(HistoryLog.kind == "event")
                        .where(HistoryLog.session_id == session_id)
                    )
                    if upper > 0:
                        count_stmt = count_stmt.where(HistoryLog.seq < upper)
                    count_stmt = (
                        count_stmt.order_by(HistoryLog.seq.desc())
                        .limit(int(events_limit))
                    )
                    naive_seqs = (await db.execute(count_stmt)).scalars().all()
                    if not naive_seqs:
                        rows = []
                    else:
                        raw_min = int(naive_seqs[-1])
                        boundary_stmt = (
                            select(HistoryLog.seq)
                            .where(HistoryLog.kind == "event")
                            .where(HistoryLog.session_id == session_id)
                            .where(HistoryLog.type == "user_message")
                            .where(HistoryLog.seq <= raw_min)
                            .order_by(HistoryLog.seq.desc())
                            .limit(1)
                        )
                        boundary_row = (await db.execute(boundary_stmt)).scalar()
                        boundary = int(boundary_row) if boundary_row else raw_min
                        bstmt = (
                            select(HistoryLog)
                            .where(HistoryLog.kind == "event")
                            .where(HistoryLog.session_id == session_id)
                            .where(HistoryLog.seq >= boundary)
                        )
                        if upper > 0:
                            bstmt = bstmt.where(HistoryLog.seq < upper)
                        bstmt = bstmt.order_by(
                            HistoryLog.seq.asc(), HistoryLog.ts.asc(),
                        )
                        rows = (await db.execute(bstmt)).scalars().all()
                else:
                    stmt = (
                        select(HistoryLog)
                        .where(HistoryLog.kind == "event")
                        .where(HistoryLog.session_id == session_id)
                        .where(HistoryLog.seq > int(since_seq or 0))
                        .order_by(HistoryLog.seq.asc(), HistoryLog.ts.asc())
                        .limit(int(events_limit))
                    )
                    rows = (await db.execute(stmt)).scalars().all()
                _events = []
                for r in rows:
                    payload = dict(r.payload or {})
                    env_event_id = payload.get("event_id") or ""
                    env_op_id = payload.get("op_id") or ""
                    env_op_type = payload.get("op_type") or ""
                    env_op_state = payload.get("op_state") or ""
                    _events.append({
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
                if _events:
                    _events_next_seq = int(_events[-1]["seq"] or 0) + 1
                    _events_has_more = (
                        len(_events) >= int(events_limit)
                        or (_events_next_seq - int(since_seq or 0)) < _events_total
                    )
                    _events_prev_seq = int(_events[0]["seq"] or 0)
                    if _events_prev_seq > 0:
                        remaining_back = int((await db.execute(
                            select(func.count())
                            .select_from(HistoryLog)
                            .where(HistoryLog.kind == "event")
                            .where(HistoryLog.session_id == session_id)
                            .where(HistoryLog.seq < _events_prev_seq)
                        )).scalar() or 0)
                        _events_has_more_back = remaining_back > 0
            return {
                "events": _events,
                "events_total": _events_total,
                "events_next_seq": _events_next_seq,
                "events_has_more": _events_has_more,
                "events_prev_seq": _events_prev_seq,
                "events_has_more_back": _events_has_more_back,
            }

        _h = await get_default_worker().run_async(_load_history_log_events)
        events = _h["events"]
        events_total = _h["events_total"]
        events_next_seq = _h["events_next_seq"]
        events_has_more = _h["events_has_more"]
        events_prev_seq = _h["events_prev_seq"]
        events_has_more_back = _h["events_has_more_back"]
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
        "events_prev_seq": events_prev_seq,
        "events_has_more_back": events_has_more_back,
        "turn_active": turn_active,
        "pending_queue": [e.to_dict() for e in pending_entries],
    }

    try:
        from digitorn.core.app.manager_v2._session import (
            _aggregate_gateway_usage,
        )
        totals = await _aggregate_gateway_usage(app_id, [session_id])
        t = totals.get(session_id)
        if t:
            data["tokens"] = {
                "prompt": t["prompt_tokens"],
                "completion": t["completion_tokens"],
                "total": t["prompt_tokens"] + t["completion_tokens"],
            }
            data["cost_usd"] = float(t["cost_usd"])
    except Exception:
        logger.debug("history token hydrate failed", exc_info=True)
    if session.memory_snapshot:
        data["memory_snapshot"] = session.memory_snapshot
    return AppResponse(success=True, data=data)


@router.get(
    "/{app_id}/sessions/{session_id}/snapshot",
    response_model=AppResponse,
)
async def get_session_snapshot(
    request: Request, app_id: str, session_id: str,
    since: int = 0,
) -> AppResponse:
    """Daemon-resource protocol: instance id + replay since seq."""
    _validate_id(app_id)
    _validate_id(session_id, "session_id")
    if not _is_deployed(request, app_id):
        _raise_not_deployed(request, app_id)
    await _require_session_access(request, app_id, session_id)

    user_id = getattr(request.state, "user_id", "") or ""

    from digitorn.core.instance import get_instance_id

    instance_id = get_instance_id()
    bus = getattr(request.app.state, "session_bus", None)
    current_seq = 0
    events_out: list[dict[str, Any]] = []
    full = True

    if bus is not None:
        try:
            current_seq = bus.user_latest_seq(user_id, session_id)
        except Exception:
            current_seq = 0
        if since > 0 and current_seq > 0 and since <= current_seq:
            try:
                events_out = bus.user_replay(
                    user_id, since, session_id=session_id,
                )
                if events_out:
                    first_seq = int(events_out[0].get("seq") or 0)
                    if first_seq == since + 1:
                        full = False
                else:
                    full = False
            except Exception:
                events_out = []
                full = True

    return AppResponse(success=True, data={
        "instance_id": instance_id,
        "current_seq": current_seq,
        "session_id": session_id,
        "since": since,
        "full": full,
        "events": events_out,
        "event_count": len(events_out),
    })


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
    """Fetch the persistent event log for a session."""
    _validate_id(app_id)
    _validate_id(session_id, "session_id")
    if not _is_deployed(request, app_id):
        _raise_not_deployed(request, app_id)
    # refuse anonymous + cross-user access.
    await _require_session_access(request, app_id, session_id)

    limit = max(1, min(limit, 5000))
    user_id = getattr(request.state, "user_id", "") or ""

    from digitorn.core.runtime.session_store.bridge import (
        get_default_bridge,
    )
    from digitorn.core.runtime.session_store.history_view import (
        paginate_events_forward, _filter_events, _event_to_dict,
    )
    bridge = get_default_bridge()
    state = None
    if bridge is not None:
        try:
            state = await bridge.store.open(
                session_id, app_id=app_id, user_id=user_id,
                create_if_missing=False, pin=False,
            )
        except KeyError:
            state = None
        except Exception as exc:
            logger.debug("session_store_open_failed sid=%s: %s", session_id, exc)
            state = None

    events_out: list[dict[str, Any]] = []
    if state is not None:
        # Apply user filter at the event level (matches the legacy
        # `HistoryLog.user_id == user_id` predicate).
        filtered = _filter_events(state.events)
        if user_id:
            filtered = [e for e in filtered if (e.user_id or "") == user_id]
        if since_seq:
            filtered = [e for e in filtered if e.seq > int(since_seq)]
        if since_ts:
            try:
                cutoff = since_ts
                filtered = [e for e in filtered if (e.ts or "") > cutoff]
            except Exception as exc:
                logger.debug("sessions best-effort block failed: %s", exc)
        # snapshot the full filtered count BEFORE slicing so the client's count<total pagination cursor stays valid.
        total_filtered = len(filtered)
        events_out = [_event_to_dict(e) for e in filtered[:limit]]
        for env in events_out:
            for k in ("id", "role", "content", "tool_call_id", "tool_calls"):
                env.pop(k, None)
    else:
        total_filtered = 0

    return AppResponse(success=True, data={
        "session_id": session_id,
        "count": len(events_out),
        "total": total_filtered,
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
    """Get the current memory state for a session (goal, todos, facts)."""
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
            semantic = getattr(store, "semantic", None)
            if semantic is not None:
                memory["facts"] = [
                    f.to_dict() if hasattr(f, "to_dict") else {"content": str(f)}
                    for f in getattr(semantic, "facts", [])
                ]
            if working is not None:
                memory["key_facts"] = list(getattr(working, "key_facts", []))

    return AppResponse(success=True, data=memory)


@router.get("/{app_id}/sessions/{session_id}/agents", response_model=AppResponse)
async def get_session_agents(
    request: Request, app_id: str, session_id: str,
) -> AppResponse:
    """Snapshot of every sub-agent tracked for this session."""
    _validate_id(app_id)
    _validate_id(session_id, "session_id")
    if not _is_deployed(request, app_id):
        _raise_not_deployed(request, app_id)

    deployed = _get_deployed(request, app_id)
    if deployed is None:
        return AppResponse(success=True, data={"agents": [], "total": 0, "running": 0})

    spawn_mod = deployed.modules.get("agent_spawn")
    if spawn_mod is None:
        return AppResponse(success=True, data={"agents": [], "total": 0, "running": 0})

    tracked_map: dict[str, Any] = {}
    try:
        tracked_map = dict(spawn_mod._agents.get(session_id, {}))  # type: ignore[attr-defined]
    except Exception as exc:
        logger.debug("get_session_agents: tracked map read failed: %s", exc)

    metrics_map = getattr(spawn_mod, "_agent_metrics", {}) or {}

    agents: list[dict[str, Any]] = []
    running_count = 0
    for agent_id, tracked in tracked_map.items():
        if tracked.result is not None:
            status = tracked.result.status
        elif tracked.asyncio_task and not tracked.asyncio_task.done():
            status = "running"
            running_count += 1
        else:
            status = "unknown"

        # Common fields available pre/post-completion.
        entry: dict[str, Any] = {
            "agent_id": agent_id,
            "status": status,
            "specialist": tracked.specialist or "",
            "task": (tracked.task or "")[:200],
            "description": getattr(tracked, "description", "") or "",
        }

        # Post-completion fields from AgentResult.
        if tracked.result is not None:
            r = tracked.result
            entry["duration_seconds"] = r.duration_seconds
            entry["tool_calls_count"] = len(r.tool_calls or [])
            # First non-empty line of content is the standard
            # result_summary shape used by the live notify_fn.
            summary = ""
            content = r.content or ""
            for line in content.strip().split("\n"):
                line = line.strip()
                if line:
                    summary = line[:120]
                    break
            entry["preview"] = content[:200]
            entry["result_summary"] = summary
            entry["error"] = "; ".join(r.errors[:3]) if r.errors else None
        else:
            # Live agent: derive duration from started_at, leave
            # tool_calls_count from the live metrics map.
            try:
                entry["duration_seconds"] = round(
                    time.monotonic() - tracked.started_at, 1,
                )
            except Exception:
                entry["duration_seconds"] = 0.0
            entry["tool_calls_count"] = (
                metrics_map.get(agent_id, {}).get("tool_calls", 0)
            )
            entry["preview"] = ""
            entry["result_summary"] = ""
            entry["error"] = None

        # Live metrics (always present even after completion).
        m = metrics_map.get(agent_id)
        if m:
            entry["metrics"] = {
                "tokens_in": m.get("tokens_in", 0),
                "tokens_out": m.get("tokens_out", 0),
                "tool_calls": m.get("tool_calls", 0),
                "turns": m.get("turns", 0),
            }

        agents.append(entry)

    return AppResponse(
        success=True,
        data={
            "agents": agents,
            "total": len(agents),
            "running": running_count,
        },
    )


@router.post(
    "/{app_id}/sessions/{session_id}/agents/{agent_id}/cancel",
    response_model=AppResponse,
)
async def cancel_session_agent(
    request: Request, app_id: str, session_id: str, agent_id: str,
) -> AppResponse:
    """Cancel a running sub-agent."""
    _validate_id(app_id)
    _validate_id(session_id, "session_id")
    if not _is_deployed(request, app_id):
        _raise_not_deployed(request, app_id)
    await _require_session_access(request, app_id, session_id)

    deployed = _get_deployed(request, app_id)
    if deployed is None:
        raise HTTPException(status_code=404, detail="App not deployed")

    spawn_mod = deployed.modules.get("agent_spawn")
    if spawn_mod is None:
        raise HTTPException(
            status_code=404, detail="agent_spawn module not loaded",
        )

    session_agents = getattr(spawn_mod, "_agents", {}).get(session_id, {})
    tracked = session_agents.get(agent_id)
    if tracked is None:
        return AppResponse(
            success=True,
            data={"agent_id": agent_id, "status": "not_found"},
        )

    # Already terminal - no-op.
    if tracked.result is not None:
        return AppResponse(
            success=True,
            data={
                "agent_id": agent_id,
                "status": "already_terminal",
                "terminal_status": tracked.result.status,
            },
        )

    try:
        tracked.cancel_reason = "user-cancelled via API"
        if tracked.cancel_event is not None:
            tracked.cancel_event.set()
        if tracked.asyncio_task and not tracked.asyncio_task.done():
            tracked.asyncio_task.cancel()
    except Exception as exc:
        logger.warning(
            "agent_cancel failed for %s: %s", agent_id, exc,
        )

    return AppResponse(
        success=True,
        data={"agent_id": agent_id, "status": "cancelling"},
    )


@router.get("/{app_id}/sessions/{session_id}/preview", response_model=AppResponse)
async def get_session_preview(request: Request, app_id: str, session_id: str) -> AppResponse:
    """Get the current preview snapshot for a session."""
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

    workspace_path = ""
    sess = None
    manager = _get_manager(request)
    try:
        sess = await manager.get_session(app_id, session_id, user_id=user_id)
        if sess:
            workspace_path = (
                getattr(sess, "workdir", "")
                or getattr(sess, "workspace", "")
                or ""
            )
    except Exception as exc:
        logger.debug(
            "preview_get_session_failed sid=%s: %s", session_id, exc,
        )

    ws_mod = deployed.modules.get("workspace")

    async def _hydrate() -> dict[str, Any]:
        """Cold-path: re-read state.json + workspace files from disk."""
        if hasattr(preview_mod, "hydrate_session"):
            try:
                await preview_mod.hydrate_session(
                    session_id, user_id=user_id,
                    workspace=workspace_path or None,
                )
            except Exception as exc:
                logger.debug(
                    "preview_hydrate_session_failed sid=%s: %s",
                    session_id, exc,
                )
        if ws_mod is not None and hasattr(ws_mod, "hydrate_files_from_disk"):
            try:
                await ws_mod.hydrate_files_from_disk(session_id)
            except Exception as exc:
                logger.debug(
                    "workspace_hydrate_files_failed sid=%s: %s",
                    session_id, exc,
                )
        return preview_mod.snapshot_for(session_id, user_id=user_id)

    cache = getattr(request.app.state, "workspace_cache", None)
    if cache is not None and workspace_path:
        snapshot = await cache.get_or_hydrate(
            session_id=session_id,
            workspace_path=workspace_path,
            hydrate_fn=_hydrate,
        )
    else:
        # No cache wired or no workspace bound: degrade to direct
        # hydration. Same code path as before the cache existed.
        snapshot = await _hydrate()

    try:
        if isinstance(snapshot, dict):
            sess_obj = sess if sess else None
            if sess_obj is None:
                sess_obj = await manager.get_session(
                    app_id, session_id, user_id=user_id,
                )
            if sess_obj is not None:
                msgs = list(getattr(sess_obj, "messages", []) or [])
                turn_count = sum(
                    1 for m in msgs
                    if isinstance(m, dict) and m.get("role") in ("user", "assistant")
                )
                snapshot["session"] = {
                    "session_id": session_id,
                    "app_id": app_id,
                    "created_at": float(getattr(sess_obj, "created_at", 0) or 0),
                    "last_active_at": float(getattr(sess_obj, "last_active", 0) or 0),
                    "turn_count": turn_count,
                    "is_first_visit": (
                        turn_count == 0
                        and not (snapshot.get("state") or {})
                        and not any(
                            (snapshot.get("resources") or {}).values()
                        )
                    ),
                    "title": getattr(sess_obj, "title", "") or "",
                    "workspace": (
                        getattr(sess_obj, "workdir", "")
                        or getattr(sess_obj, "workspace", "")
                        or ""
                    ),
                    "workdir": (
                        getattr(sess_obj, "workdir", "")
                        or getattr(sess_obj, "workspace", "")
                        or ""
                    ),
                    "daemon_workspace": getattr(sess_obj, "workspace", "") or "",
                }
    except Exception as exc:
        logger.warning(
            "preview_meta_inject_failed sid=%s: %s",
            session_id, exc, exc_info=True,
        )

    return AppResponse(success=True, data=snapshot)


@router.get("/{app_id}/sessions/{session_id}/queue", response_model=AppResponse)
async def list_session_queue(
    request: Request, app_id: str, session_id: str,
    include_finished: bool = False,
) -> AppResponse:
    """List pending + running messages for this session."""
    _validate_id(app_id)
    _validate_id(session_id, "session_id")
    if not _is_deployed(request, app_id):
        _raise_not_deployed(request, app_id)
    # reject anonymous / cross-user queue peeking.
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
    """List non-terminal operations for a session."""
    _validate_id(app_id)
    _validate_id(session_id, "session_id")
    if not _is_deployed(request, app_id):
        _raise_not_deployed(request, app_id)
    # Cross-user / anonymous access → 404, same contract as other
    # per-session endpoints.
    await _require_session_access(request, app_id, session_id)

    from digitorn.core.events.envelope import TERMINAL_STATES, OpState
    from digitorn.core.runtime.session_store.bridge import (
        get_default_bridge,
    )

    user_id = getattr(request.state, "user_id", "") or ""
    terminal_names = {s.value for s in TERMINAL_STATES}

    ops: dict[str, dict[str, Any]] = {}
    bridge = get_default_bridge()
    rows: list[Any] = []
    if bridge is not None:
        try:
            state = await bridge.store.open(
                session_id, app_id=app_id, user_id=user_id,
                create_if_missing=False, pin=False,
            )
            rows = [
                e for e in state.events
                if e.kind == "event" and (e.user_id or "") == user_id
            ]
        except KeyError:
            rows = []
        except Exception as exc:
            logger.debug("active_ops_load_failed sid=%s: %s", session_id, exc)
            rows = []

    for row in rows:
        payload = row.payload or {}
        op_id = payload.get("op_id") or row.correlation_id
        if not op_id:
            continue
        op_type = payload.get("op_type")
        op_state = payload.get("op_state")
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
    """Debug: what's eating the session's context window right now."""
    _validate_id(app_id)
    _validate_id(session_id, "session_id")
    manager = _get_manager(request)
    if not _is_deployed(request, app_id):
        _raise_not_deployed(request, app_id)

    # reject anonymous / cross-user context-breakdown peeking.
    session = await _require_session_access(request, app_id, session_id)

    deployed = _get_deployed(request, app_id)
    if deployed is None:
        _raise_not_deployed(request, app_id)

    from digitorn.core.runtime.compaction import aestimate_tokens, estimate_tokens
    ctx = deployed.entry_context
    _provider = getattr(ctx, "provider", None)
    _model_name = getattr(_provider, "model", None) if _provider else None

    def _est(text: str | None) -> int:
        """Real token count for a free-form string. Uses the provider's"""
        if not text:
            return 0
        if _provider is not None and hasattr(_provider, "count_tokens"):
            try:
                return int(_provider.count_tokens(text))
            except Exception as exc:
                logger.debug("sessions best-effort block failed: %s", exc)
        if _model_name:
            try:
                from litellm import token_counter
                return int(token_counter(model=_model_name, text=text))
            except Exception as exc:
                logger.debug("sessions best-effort block failed: %s", exc)
        return max(1, len(text) // 3)

    msg_tokens = await aestimate_tokens(
        session.messages or [], provider=_provider,
    )

    # 2-6. Bundle the remaining 5 tokenizer calls into a single thread
    # hop so we pay the off-loop overhead once instead of five times.
    try:
        from digitorn.core.runtime.messages import to_chat_messages  # noqa: F401
        sys_prompt = ctx.system_prompt or ""
    except Exception:
        sys_prompt = ""

    tools = ctx.tools or []
    try:
        import json as _json
        tools_json = _json.dumps(tools, default=str)
    except Exception:
        tools_json = ""

    mem_text = ""
    try:
        mem = ctx.memory_module
        if mem is not None and hasattr(mem, "get_prompt_sections"):
            sections = mem.get_prompt_sections(session_id=session_id) or []
            mem_text = "\n".join(
                s.get("content", "") if isinstance(s, dict) else str(s)
                for s in sections
            )
    except Exception as exc:
        logger.debug("sessions best-effort block failed: %s", exc)

    setup_text = ""
    try:
        setup = ctx.setup_summary or {}
        if isinstance(setup, dict):
            import json as _json
            setup_text = _json.dumps(setup, default=str)
    except Exception as exc:
        logger.debug("sessions best-effort block failed: %s", exc)

    skills_text = ""
    try:
        agent = getattr(ctx, "agent_def", None)
        if agent is not None:
            skills_text = getattr(agent, "skills_content", "") or ""
    except Exception as exc:
        logger.debug("sessions best-effort block failed: %s", exc)

    def _est_all() -> tuple[int, int, int, int, int]:
        return (
            _est(sys_prompt),
            _est(tools_json),
            _est(mem_text),
            _est(setup_text),
            _est(skills_text),
        )
    sys_tokens, tools_tokens, mem_tokens, setup_tokens, skills_tokens = (
        await asyncio.to_thread(_est_all)
    )
    if not tools_tokens:
        # Fallback: 90 tokens per tool heuristic
        tools_tokens = len(tools) * 90

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
        "compactions": _read_session_compactions(app_id, session_id),
        "breakdown": {
            "system_prompt": sys_tokens,
            "tools_schema": tools_tokens,
            "messages": msg_tokens,
            # Informational - already counted inside system_prompt:
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
    """Cancel a queued (not yet running) message. Running messages must"""
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
            _uid = getattr(request.state, "user_id", "") or "local"
            await manager.event_bus.emit(_turn_event(
                "message_cancelled",
                app_id=app_id, session_id=session_id, user_id=_uid,
                correlation_id=entry_id,
                op_state=_OS.CANCELLED,
                payload={"entry_id": entry_id, "session_id": session_id},
            ))
        except Exception as exc:
            logger.debug("sessions best-effort block failed: %s", exc)
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
        _uid = getattr(request.state, "user_id", "") or "local"
        await manager.event_bus.emit(_turn_event(
            "queue_cleared",
            app_id=app_id, session_id=session_id, user_id=_uid,
            correlation_id=f"queue-{session_id}",
            op_state=_OS.COMPLETED,
            payload={"session_id": session_id, "cancelled": n},
        ))
    except Exception as exc:
        logger.debug("sessions best-effort block failed: %s", exc)
    return AppResponse(success=True, data={"cancelled": n})


@router.get("/{app_id}/sessions/{session_id}/state", response_model=AppResponse)
async def session_state(
    request: Request, app_id: str, session_id: str,
    since_seq: int = 0,
) -> AppResponse:
    """Authoritative session state envelope - client's source of truth."""
    _validate_id(app_id)
    _validate_id(session_id, "session_id")
    manager = _get_manager(request)
    if not _is_deployed(request, app_id):
        raise HTTPException(status_code=404, detail=f"App '{app_id}' not deployed")

    _uid = getattr(request.state, "user_id", None) or "local"
    envelope = await manager.build_state_envelope(app_id, session_id, _uid)

    gap_events: list[dict[str, Any]] = []
    if since_seq > 0:
        try:
            from digitorn.core.runtime.session_store.bridge import (
                get_default_bridge,
            )
            bridge = get_default_bridge()
            if bridge is not None:
                state = await bridge.store.open(
                    session_id, app_id=app_id, user_id=_uid,
                    create_if_missing=False, pin=False,
                )
                # Capped at 1000 rows -- clients lagging further should
                # fetch the full /history endpoint instead.
                tail = [
                    e for e in state.events
                    if e.kind == "event" and e.seq > int(since_seq)
                ][:1000]
                for r in tail:
                    gap_events.append({
                        "seq": int(r.seq),
                        "type": r.type,
                        "kind": r.kind,
                        "app_id": r.app_id,
                        "session_id": r.session_id,
                        "user_id": r.user_id,
                        "correlation_id": r.correlation_id or None,
                        "payload": r.payload or {},
                        "ts": r.ts,
                    })
        except Exception as exc:
            logger.debug(
                "gap_fill_failed sid=%s since=%d: %s",
                session_id, since_seq, exc,
            )

    envelope["gap_events"] = gap_events
    return AppResponse(success=True, data=envelope)

