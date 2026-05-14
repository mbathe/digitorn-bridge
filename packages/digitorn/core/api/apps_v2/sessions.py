"""Routes for the sessions group, extracted from the legacy ``apps.py``.

This module is part of the ``apps_v2`` refactoring - same paths,
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


# Token-count cache for cold-path context estimates in ``get_session``.
# Keys: ``(session_id, message_count)``. Value: ``(sys_tokens,
# tools_tokens, msg_tokens)``. Naturally invalidates when a new turn
# appends a message (count changes -> miss -> recompute). 200-entry
# soft cap with random eviction for memory bound.
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



@router.post("/{app_id}/sessions", response_model=AppResponse)
async def create_session(
    request: Request, app_id: str,
    body: CreateSessionRequest,
) -> AppResponse:
    """Create a new conversation session AND dispatch its first message.

    Atomic contract: a session is born with a first user message - there
    is no way to create an empty session. This guarantees the listing in
    ``GET /sessions`` only ever shows sessions the user actually wrote
    something in (no "ghost rows" left behind by clients that opened a
    blank pane and walked away).

    Body (JSON):
      - ``message`` (required, ≤ ~1 MiB)
      - ``workspace_path`` (required when the app declares
        ``execution.workspace_mode: required``; optional otherwise)
      - ``images`` (optional ``[{data, mime, name}]``)
      - ``queue_mode`` (optional async/wait, default from config)
      - ``client_message_id`` (optional idempotency key)

    Returns the session metadata (session_id, greeting, context
    estimate, preview_url, workspace) plus a ``first_message`` block
    with the dispatch correlation_id + status. Subsequent messages MUST
    be sent via ``POST /sessions/{session_id}/messages`` - this endpoint
    is for the very first turn only.
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

    # ── Workdir vs Workspace split ──────────────────────────────
    # The WORKSPACE is the daemon-private dir under
    # ``~/.digitorn/workspaces/{app}/{sid}/``. ALWAYS auto-created.
    # Holds state.json, baselines, SDK-private files. The agent never
    # operates on it directly.
    #
    # The WORKDIR is the agent's working directory. User-supplied via
    # the request body (``workdir`` field, with legacy alias
    # ``workspace_path``). If absent and the app's ``workdir_mode``
    # isn't ``required``, ``workdir`` falls back to the workspace -
    # the legacy single-tree behaviour, so old apps keep working.
    user_workdir = (body.workdir or body.workspace_path or "").strip()

    deployed_for_check = _get_deployed(request, app_id)
    workdir_mode = "auto"
    if deployed_for_check is not None:
        # Read the new ``runtime.workdir_mode`` (renamed from
        # ``execution.workspace_mode``). Both names supported during
        # the transition - we also fall back to the legacy attr.
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
        try:
            p = _Path(user_workdir).expanduser().resolve()
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

    # Always create the daemon-private workspace under ~/.digitorn/.
    # This is where state.json + baselines + SDK-private files live,
    # never visible to the agent.
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
    # Resolve effective workdir: user-supplied OR fall back to the
    # daemon workspace (legacy single-tree mode for apps that don't
    # care about the split).
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

    # Compute initial context estimate using the provider's actual
    # tokenizer (via litellm) - gives a real number for the model the
    # session will use, not a rough char/4 heuristic. The provider
    # knows its own model; we just delegate.
    context = {}
    if deployed:
        entry_ctx = deployed.entry_context
        system_prompt = entry_ctx.system_prompt or ""
        tools = entry_ctx.tools or []
        cc = entry_ctx.context_config
        provider = getattr(entry_ctx, "provider", None)
        model_name = getattr(provider, "model", None) if provider else None

        def _count_text(text: str) -> int:
            """Real token count for a free-form string.

            Cascade: provider tokenizer → litellm direct → char/4 last
            resort. The crude path used to be the default for the
            create-session path - that meant the very first
            ``message_history_pct`` users saw was a guess. Now it's the
            real number unless every tokenizer route fails.
            """
            if not text:
                return 0
            if provider is not None and hasattr(provider, "count_tokens"):
                try:
                    return int(provider.count_tokens(text))
                except Exception:
                    pass
            if model_name:
                try:
                    from litellm import token_counter
                    return int(token_counter(model=model_name, text=text))
                except Exception:
                    pass
            return max(1, len(text) // 4)

        # Off-load tokenizer calls. ``_count_text`` runs the provider's
        # tokenizer (litellm + tiktoken / Anthropic offline / HF) which
        # is CPU-bound and on first call also loads the tokenizer model
        # (10s+ for HF). Keep the loop responsive.
        sys_tokens = await asyncio.to_thread(_count_text, system_prompt)

        # Tool schemas - serialize the full list and count. This is
        # what the LLM API charges you for on the prompt side when
        # tools are passed in the request. Each tool dict carries
        # name + description + JSON-schema for params.
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
            "compactions": 0,
            "model": getattr(provider, "model", "") if provider else "",
        }

    preview_url: str | None = None
    if deployed is not None:
        # Auto-register a bundled attachment for SDK apps that ship
        # ``web/dist/index.html``. The iframe will mount the SDK UI
        # as soon as the session is alive - no agent action needed.
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

        # The Flutter / web client polls ``GET /api/apps/{id}/web-preview``
        # to discover the URL for whichever attachment is registered
        # (bundled OR proxy). Expose that endpoint on session_create
        # so the client can render an iframe right away.
        has_preview = (
            "preview" in (deployed.modules or {})
            or web_preview_mod is not None
        )
        if has_preview:
            preview_url = (
                f"/api/apps/{app_id}/web-preview"
                f"?session_id={session_id}&name=default"
            )

    # Dispatch the first message through the same per-session FIFO
    # queue used by ``POST /sessions/{sid}/messages``. We delegate so
    # the queueing, image upload, orphan-watchdog, fast-path reservation,
    # and event emission are all identical to a follow-up message - one
    # code path, one contract. If dispatch fails (queue full, app
    # degraded, etc.) we tear down the session we just persisted so the
    # caller gets a clean failure with no ghost row left behind.
    from digitorn.core.api.apps_v2.messages import session_send_message
    smr = SessionMessageRequest(
        message=body.message,
        # Send the WORKDIR (agent-facing path). The daemon's private
        # workspace lives under ~/.digitorn/ and is set on the session
        # row directly - it doesn't propagate through the message
        # request envelope.
        workspace=effective_workdir or None,
        images=body.images,
        files=body.files,
        queue_mode=body.queue_mode,
        client_message_id=body.client_message_id,
        mode=body.mode,
        template_id=body.template_id,
    )
    try:
        dispatch_resp = await session_send_message(
            request, app_id, session_id, smr,
        )
    except HTTPException:
        # Roll back the session - a failed first message means the
        # session was never usable. Deleting keeps GET /sessions free
        # of half-born rows.
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
        # ``workspace`` is the path the FRONTEND renders (file tree,
        # editor, status bar). That's the agent-facing ``workdir`` -
        # the daemon-private dir under ``~/.digitorn/`` is internal
        # state and must not surface in the UI. Both keys carry the
        # same value here so legacy clients reading ``workspace`` keep
        # working with the new split semantics.
        "workspace": effective_workdir,
        "workdir": effective_workdir,
        "first_message": {
            "correlation_id": dispatch_data.get("correlation_id"),
            "status": dispatch_data.get("status"),
            "position": dispatch_data.get("position"),
            "queue_depth": dispatch_data.get("queue_depth"),
            # Authoritative chat seq for the first user_message. Same
            # contract as the standalone POST /messages: non-null only
            # when the daemon dispatched the message immediately. The
            # client uses this to render the bubble - no seq, no chat
            # entry (single source of truth for chat ordering).
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
    """Search across all sessions - matches title and message content.

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

    # Hard cap on candidate sessions. Was 200 -- and the loop below
    # did one sequential ``await to_thread(load_messages)`` PER
    # session, plus a per-message ``find()`` over 10 msgs each. On a
    # power user with hundreds of sessions that's 200 sequential disk
    # reads with GIL-bound string ops -- main loop choked for seconds.
    # 50 is enough to cover the user's recent sessions where most
    # title / content matches live.
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
        # Defer the (disk-heavy) message scan to a second pass so we
        # can run them all in parallel via ``asyncio.gather``. That
        # way 50 sessions become ~1 disk roundtrip instead of 50.
        if score > 0:
            s["relevance"] = score
            s["snippets"] = snippets
            matches.append((score, s))
        needs_message_scan.append(s)

    # Parallel message scan. Each ``load_messages`` is a sync disk
    # read off-loop via ``to_thread``; gather lets them all run on
    # threadpool workers concurrently. ``asyncio.gather`` returns when
    # the slowest one finishes, which is bounded by the max file size,
    # not by the count of sessions.
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
    """Get session status - metadata, live metrics, context pressure.

    Single endpoint for clients to get the full session state on load.
    All numbers come from the SessionMetrics counters (populated by
    agent_loop during execution) - no estimation or speculation.
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

        # Tokens: aggregate across the main agent + every sub-agent
        # spawned in this session. Without the rollup, ``tokens`` only
        # reflected the main coordinator's own LLM calls and a 50-agent
        # fleet looked idle from the dashboard. ``rollup.agents`` >= 1
        # tells the UI how many agents are pooled into the totals.
        rollup = session_total(app_id, session_id)
        if rollup.get("agents", 0) <= 1:
            # No sub-agents in this session - keep the legacy shape
            # (matches what clients expect today). Use the picked
            # ``sm`` totals so we never under-report when both default-
            # scoped and app-scoped entries exist for the same session.
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

                # Real tokenizer (provider → litellm → char/4 last resort)
                # so the very first ``context.pressure`` shown to the
                # client is accurate, not a 4-char estimate that drifts
                # by 30%+ on tool-heavy schemas.
                def _ct(text: str) -> int:
                    if not text:
                        return 0
                    if _provider is not None and hasattr(_provider, "count_tokens"):
                        try:
                            return int(_provider.count_tokens(text))
                        except Exception:
                            pass
                    if _model_name:
                        try:
                            from litellm import token_counter
                            return int(token_counter(model=_model_name, text=text))
                        except Exception:
                            pass
                    return max(1, len(text) // 4)

                # All tokenizer work off-loop. With long sessions
                # (200+ messages) the sum() across messages can take
                # seconds the first time the tokenizer loads -- the
                # tokenizer holds the GIL throughout (pure-Python
                # regex / vocab lookup), so even a threadpool worker
                # blocks the main loop via GIL contention.
                #
                # Cache key = (session_id, message_count). When a new
                # turn appends a message, the count changes and the
                # cache is naturally invalidated. Multi-tab refresh
                # (most common cause of repeat calls) hits the cache.
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
                    "compactions": 0,
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
    # Drop the workspace-cache entry + stop its FS watcher so a deleted
    # session doesn't keep file-descriptors open or serve a stale
    # snapshot if the same session id is ever re-created.
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
        # Match the workspace-fork response shape (``source_session_id`` +
        # ``session_id``) so clients using both endpoints don't have to
        # branch on field names. ``forked_from`` + ``new_session_id`` are
        # kept as legacy aliases.
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
    """Abort the currently running agent turn for a session.

    **Default behavior**: cancels only the currently running turn. The
    rest of the message queue is **preserved** and the dispatcher
    picks up the next message automatically - the user gets
    ``message_started`` for ``next_correlation_id`` within seconds.

    ``?purge_queue=true`` drops every queued message along with the
    abort - use when the user clicks "Stop everything" rather than
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

    # Cancel the running asyncio task - this triggers CancelledError
    # inside _chat_locked which saves state + marks session.interrupted
    active_key = f"{app_id}:{session_id}"
    task = manager._session_tasks.get(active_key)
    task_cancelled = False
    if task is not None and not task.done():
        task.cancel()
        task_cancelled = True

    # Cooperative cancel signal: in addition to ``task.cancel()`` (which
    # is best-effort and propagates only on the next await), set the
    # AgentContext's cancel_event so the agent_loop bails at the next
    # checkpoint -- including INSIDE a tool-call loop (the digitorn-
    # lovable zombie ran 1947 retries within one turn, never reaching
    # an await that would observe the CancelledError). Idempotent and
    # safe when no active context exists.
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
            except Exception:
                pass
            evt.set()
            cooperative_signaled = 1
    except Exception:
        logger.debug("abort: cooperative cancel set failed", exc_info=True)

    # Cancel any pending approvals for this app. ApprovalQueue.cancel_all
    # resolves every awaiting future with (approved=False, "") so the
    # ``await ctx.approval_queue.enqueue(...)`` inside the agent loop
    # unblocks on the same tick - without this, an approval-blocked turn
    # can outlive the abort if the caller hadn't yet hit the await.
    pending_approvals_cancelled = 0
    deployed = _get_deployed(request, app_id)
    if deployed:
        approval_queue = getattr(deployed, "approval_queue", None)
        if approval_queue is not None and hasattr(approval_queue, "cancel_all"):
            try:
                pending_approvals_cancelled = approval_queue.cancel_all()
            except Exception:
                logger.debug("abort: approval_queue.cancel_all failed", exc_info=True)

    # Kill background shell tasks for this session so they don't orphan.
    # Each killed task sends a 'cancelled' notification to the agent queue
    # so the agent knows on resume.
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

    # Queue handling - default is "keep the rest". Explicit opt-in
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

    # Force-release the session reservation. Normally, ``manager.chat()``'s
    # ``finally`` discards the active key when the cancelled task unwinds.
    # But when the turn was paused before ``manager.chat()`` ran (e.g.
    # ``credential_required`` raised by the pre-chat gate), no task was
    # ever registered, ``task.cancel()`` above was a no-op, and the
    # session would otherwise stay ``is_active=True`` forever. Discard
    # is idempotent, so calling it after a cancelled task already cleared
    # it is harmless.
    try:
        manager.release_session(app_id, session_id)
    except Exception:
        logger.debug("abort: release_session failed", exc_info=True)

    # Persist ``session.interrupted=True`` so the next message triggers
    # the smart-resume path (orphaned ``tool_call_id`` entries get synthetic
    # ``{"interrupted": true}`` results, the LLM picks up cleanly). When a
    # task was cancelled, ``_chat_locked``'s finally already set this flag.
    # We set it again here as a safety net for the no-task case.
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
                except Exception:
                    pass
                await asyncio.to_thread(
                    manager._session_store.put, sess,
                )
        except Exception:
            logger.debug("abort: persist interrupted flag failed", exc_info=True)

    # Signal abort via the event bus (Socket.IO clients see it immediately)
    try:
        from digitorn.core.events.envelope import OpState as _OS
        # abort is session-scoped - every currently-active op in the
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
                "task_cancelled": task_cancelled,
                "approvals_cancelled": pending_approvals_cancelled,
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
            _resume_task = asyncio.create_task(_resume())
            _active_turn_tasks.add(_resume_task)
            _resume_task.add_done_callback(_active_turn_tasks.discard)
        except Exception:
            pass

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
    # Off-loop write -- restored content can be multi-MB for files
    # that were heavily edited before the snapshot. Holding the main
    # loop on a sync ``write_bytes`` blocks every other request.
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
        # Hard 60s timeout on the manual compaction call. The legacy
        # ``emergency_compact`` has been observed to hang for 3+ hours
        # under specific edge cases (tokenizer first-call download,
        # event-bus deadlock). With ``wait_for`` the daemon refuses
        # cleanly instead of stalling its asyncio loop indefinitely.
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

    # Wire the durable side of compaction. ``emergency_compact`` only
    # mutates the in-flight ``messages`` list -- without this second
    # call, ``state.events`` + ``state.messages`` in the SessionStore
    # never drop, ``/history`` keeps returning the full transcript,
    # and the cold-reload code that reads ``compaction.json`` is dead
    # (the file never gets written). Mirror the trim into the
    # SessionStore so the compaction survives daemon restart.
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
                    # Compute cutoff: drop everything before the last
                    # ``keep_count`` projected messages. If state.messages
                    # is shorter than expected (race / drift) the loop
                    # naturally degrades to compact-nothing.
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
            # Don't fail the HTTP request just because the durable
            # write hiccupped -- the in-flight compaction already
            # succeeded (agent will use trimmed context on next turn).
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
    before_seq: int | None = None,
    events_limit: int = 50000,
) -> AppResponse:
    """Full chronological history for a session - every message AND
    every event the daemon has ever recorded.

    The response carries **two parallel streams** the client stitches
    together to render the full timeline:

    - ``messages``: user↔assistant exchanges (reconstructed turns
      when ``include_system=false``, raw otherwise) - pulled from
      ``session_messages`` (append-only, durable).
    - ``events``: every envelope emitted on the session bus - user
      messages, tool_start / tool_call / tool_result, thinking
      (started/delta/complete), tokens, agent_event, hook_notification,
      quota_exceeded, compaction, abort, memory_update, context
      warnings, approval_request/resolve, anything the runtime
      fires. Pulled from ``session_events`` (append-only, durable,
      ordered by ``seq``). Nothing is filtered out by type - the
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
    the behavior engine's system directives) - for the dev SDK
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

    # Single-source-of-truth read from the in-memory SessionStore. The
    # state already carries (a) messages projected from kind='message'
    # events with their canonical seq, and (b) the full event journal
    # with seq-monotone ordering. Pagination is O(log N) bisect on the
    # in-memory seq array -- no Postgres round-trip per request.
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
        # Sync contract: stamp the daemon's process-wide instance id on
        # every history response. The client compares this against its
        # stored value to detect a daemon restart (UUID changes on
        # every Python process spawn). On mismatch the client wipes
        # its local timeline + persisted lastSeq and does a full
        # re-seed, because seq numbers from the old process are
        # meaningless against the new one. See
        # ``digitorn_web/src/stores/chat.ts`` ``_loadHistory``.
        from digitorn.core.instance import get_instance_id as _get_inst
        history_data["instance_id"] = _get_inst()
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

    # Fallback: legacy Postgres history_log path (kept for sandbox /
    # tests where the SessionStore isn't available). Will be removed
    # in a follow-up once every test daemon runs the new store.
    # Phase-4c remnant: legacy ``history_log`` fallback for sandbox /
    # tests where the SessionStore isn't available. Routed through
    # the ``persist_worker`` so the main loop never touches asyncpg
    # directly -- on Windows + SSL the cross-loop lock acquisition
    # in asyncpg can stall the main loop for 5-25s.
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
                        # Preserve fields the turn-builder reads. Tool
                        # results live in ``tool_call_id`` rows; assistant
                        # tool_calls live on the assistant row's
                        # ``tool_calls`` JSON column.
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

    # Load full chronology from the unified ``history_log`` table
    # (authoritative bank-grade ledger). One SQL query returns
    # messages + events entrelacés dans l'ordre temporel exact.
    events = []
    events_total = 0
    events_next_seq = int(since_seq or 0)
    events_has_more = False
    events_prev_seq = 0
    events_has_more_back = False
    # Backward pagination mode. When the client passes ``before_seq``
    # (incl. 0 = sentinel meaning "from the end"), we return the most
    # recent events first instead of paging forward from since_seq.
    # Used by the web client's lazy infinite-scroll-up: initial load
    # = before_seq=0 (last N events), each scroll-up = before_seq=
    # oldest_seq_loaded.
    backward_mode = before_seq is not None
    # Route the ENTIRE history_log query block through the
    # persist_worker. Same SQL, same outputs, same fallback path on
    # exception -- but every asyncpg call runs on the worker's
    # dedicated psycopg3 loop, immune to the cross-loop locking
    # stalls that hit the main loop with asyncpg + SSL on Windows.
    # See ``_activation_sweeper`` in core/server.py for the matching
    # pattern. The inner coroutine returns a result dict that we
    # unpack into the local vars on the main loop.
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
                # Total count of events for this session. MUST filter on
                # kind='event' so the count matches the page query below
                # (which also filters kind='event'). Without this filter
                # the total counted message + audit rows too, leaving
                # ``events_has_more`` permanently True for sessions where
                # the unfiltered total > the event-filtered page sum: the
                # web client then paginates fruitlessly until the daemon
                # returns zero events on the next page (sole fallback that
                # stops the loop). Filtering here makes the math right and
                # spares the extra round-trip.
                _events_total = int((await db.execute(
                    select(func.count())
                    .select_from(HistoryLog)
                    .where(HistoryLog.kind == "event")
                    .where(HistoryLog.session_id == session_id)
                )).scalar() or 0)

                # Fetch rows strictly after since_seq, ordered by seq then
                # ts for determinism. **Filter on kind='event'** - messages
                # and audit rows live in the same table but DON'T belong in
                # the client's events[] stream. Without this filter a
                # kind='message' row leaks as a second ``user_message``
                # event (no event_id, no correlation_id) → frontend dedup
                # fails → duplicate user bubbles. Messages are served by
                # the ``messages`` array above.
                if backward_mode:
                    # Turn-aligned reverse pagination. See original
                    # comment block below the function for the full
                    # algorithm rationale.
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
                    # Promote contract fields from payload to envelope
                    # top-level so the client's reducer can read them
                    # directly. ``event_id`` in particular is the dedup
                    # key - MUST be present.
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
        "events_prev_seq": events_prev_seq,
        "events_has_more_back": events_has_more_back,
        "turn_active": turn_active,
        "pending_queue": [e.to_dict() for e in pending_entries],
    }

    # Hydrate tokens/cost from the gateway's usage_events table. The
    # daemon doesn't accumulate per-session totals locally; the gateway
    # is the source of truth. Both share Postgres so this is one cheap
    # SUM() query - failures are logged + leave the default 0/0/0.
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
    """Daemon-resource protocol: instance id + replay since seq.

    The lightweight reconcile endpoint that every ``useDaemonResource``
    (web) / ``DaemonResourceController`` (Flutter) calls on:

    - mount (no ``since`` ⇒ flag ``full: true`` so the client re-seeds
      its per-resource state via dedicated endpoints).
    - socket reconnect (``since=lastSeq`` ⇒ replay the in-memory
      ring buffer; if ``since`` is below the buffer's tail, ``full:
      true`` is returned and the client re-seeds).
    - tab focus after ``stale`` heartbeat (same ``?since=`` path).

    Always carries ``instance_id`` so the client can detect daemon
    restart even when the buffer happens to have valid replay data
    for ``since`` (rare, but possible if seq seed-from-db survives a
    restart). On instance mismatch the client wipes regardless of
    the events array.

    Cheaper than ``/events`` (in-memory, no DB hit). Use ``/events``
    for older history past the ring-buffer window.
    """
    _validate_id(app_id)
    _validate_id(session_id, "session_id")
    if not _is_deployed(request, app_id):
        _raise_not_deployed(request, app_id)
    await _require_session_access(request, app_id, session_id)

    user_id = getattr(request.state, "user_id", "") or ""

    from digitorn.core.instance import get_instance_id

    instance_id = get_instance_id()
    # SessionBus singleton lives on app.state (wired in lifespan).
    # Fall back to None when the bus isn't ready yet (very early
    # boot) — the client just sees current_seq=0 and re-seeds.
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
                # If the oldest event in the replay is contiguous
                # with ``since`` (first.seq == since + 1) the delta
                # is complete - flag ``full: false`` so the client
                # applies events without wiping. If there is a gap
                # (since predates the buffer's tail), force a full
                # re-seed to avoid silent state divergence.
                if events_out:
                    first_seq = int(events_out[0].get("seq") or 0)
                    if first_seq == since + 1:
                        full = False
                else:
                    # No events past ``since`` — caller is up-to-date.
                    # Mark full=false so the client doesn't wipe;
                    # current_seq tells them they're caught up.
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

    1. *Durable replay* - the same ``session_events`` rows this HTTP
       route returns (identical shape, identical filter).
    2. *Preview/workspace hydration* - ``preview:snapshot``,
       ``workspace:snapshot`` emitted once at join time from the
       in-memory preview module (NOT persisted to ``session_events``).
    3. *Bootstrap side-channels* - occasional channel/widget snapshots.

    So ``count(/events) <= count(socket events)``: if the two numbers
    disagree, the difference is hydration, not a missing durable row.
    To verify parity, compare only the envelopes whose ``seq`` is set.

    Events are scoped to the caller's ``user_id`` - an admin querying
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

    # Read from the in-memory SessionStore (canonical source). Falls
    # through to the legacy Postgres path only when the bridge is
    # absent (sandbox / pre-bootstrap).
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
        # ``HistoryLog.user_id == user_id`` predicate).
        filtered = _filter_events(state.events)
        if user_id:
            filtered = [e for e in filtered if (e.user_id or "") == user_id]
        if since_seq:
            filtered = [e for e in filtered if e.seq > int(since_seq)]
        if since_ts:
            try:
                cutoff = since_ts
                filtered = [e for e in filtered if (e.ts or "") > cutoff]
            except Exception:
                pass
        # Snapshot the full filtered count BEFORE slicing -- the client
        # needs ``total`` to know whether to paginate. Previously this
        # was ``len(events_out)`` (post-slice), silently breaking the
        # ``count < total`` pagination cursor: the client always saw
        # ``total == count`` and assumed it had everything, even after
        # a 500-row page from a 5000-row session.
        total_filtered = len(filtered)
        events_out = [_event_to_dict(e) for e in filtered[:limit]]
        # Strip a few legacy DB-only fields the HTTP shape didn't carry
        # (id, role, content, tool_call_id, tool_calls live on the
        # /history endpoint, not /events).
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
    """Get the current memory state for a session (goal, todos, facts).

    Lighter than full history - only the working memory snapshot.
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
            # ``facts`` are the structured entries the Remember tool
            # writes (via ``store.semantic.add_fact``). ``working.
            # key_facts`` is a separate rolling buffer of bg-task one-
            # liners and must NOT be served as ``facts`` -- that was
            # the bug behind the smoke test seeing empty / polluted
            # facts after a Remember call.
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
    """Snapshot of every sub-agent tracked for this session.

    The frontend's ``AgentGroup`` widget is driven by streamed SSE
    events (``spawn_agent`` / ``agent_progress`` / ``agent_result`` /
    ``agent_cancel``). Streaming alone is fragile: a dropped socket,
    a session switch, or a refreshed tab leaves the user looking at
    stale "running" rows even though the daemon long since finalized
    those agents (or they crashed without ever emitting a terminal
    event).

    This endpoint returns the daemon's CURRENT in-memory snapshot of
    every TrackedAgent for the session — running, completed, failed,
    cancelled — so the client can reconcile its local timeline on
    reconnect / resume / focus-change. The watchdog installed in
    ``agent_spawn.module._install_agent_watchdog`` guarantees that
    every terminal task transitions ``tracked.result`` away from
    None even if the runner crashed pre-finalize, so the snapshot is
    always authoritative.

    Response shape mirrors the in-flight ``agent_event`` payload so
    the client can pipe each entry through the same ``AgentEventData``
    decoder it already uses for SSE:

        {
          "agents": [
            {
              "agent_id": "...",
              "status": "running" | "completed" | "failed" | "cancelled" | "timeout",
              "specialist": "...",
              "task": "...",
              "description": "...",
              "duration_seconds": float,
              "tool_calls_count": int,
              "preview": "",        # latest stdout chunk (best-effort)
              "result_summary": "", # first non-empty line of content
              "error": "..." | null,
              "metrics": { tokens_in, tokens_out, tool_calls, turns },
            },
            ...
          ],
          "total": N,
          "running": N,
        }
    """
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

    # Pull the per-session ``_agents`` dict directly. The action-
    # mode ``_mode_list`` would also work but it's session-scoped to
    # whatever session is set on the module's contextvar - which
    # isn't populated when called from a plain HTTP request handler.
    # Reading the dict by session_id is the safe path.
    tracked_map: dict[str, Any] = {}
    try:
        tracked_map = dict(spawn_mod._agents.get(session_id, {}))  # type: ignore[attr-defined]
    except Exception as exc:
        logger.debug("get_session_agents: tracked map read failed: %s", exc)

    metrics_map = getattr(spawn_mod, "_agent_metrics", {}) or {}

    agents: list[dict[str, Any]] = []
    running_count = 0
    for agent_id, tracked in tracked_map.items():
        # Resolve a status from the tracked struct + asyncio task.
        # tracked.result is set by the runner OR by the watchdog,
        # so the snapshot is consistent with the SSE stream.
        if tracked.result is not None:
            status = tracked.result.status
        elif tracked.asyncio_task and not tracked.asyncio_task.done():
            status = "running"
            running_count += 1
        else:
            # Task done but no result - watchdog will land any
            # moment. Report unknown so the client doesn't lock
            # the row to a misleading state.
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
    """Cancel a running sub-agent.

    The Activity panel's Cancel button (web + Flutter) hits this
    endpoint. Delegates to the ``agent_spawn`` module's cancel mode
    which sets the cooperative ``cancel_event`` BEFORE issuing the
    hard ``Task.cancel()`` so the loop bails at the next turn
    boundary even if asyncio cancel is swallowed.

    Idempotent: cancelling an already-terminal agent is a no-op
    that returns ``success=true`` with ``status: "already_terminal"``.
    """
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

    # Already terminal — no-op.
    if tracked.result is not None:
        return AppResponse(
            success=True,
            data={
                "agent_id": agent_id,
                "status": "already_terminal",
                "terminal_status": tracked.result.status,
            },
        )

    # Cooperative cancel + hard cancel. Mirror of agent_spawn's
    # ``_mode_cancel`` semantics so the API path is identical to
    # the agent-tool path.
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
    """Get the current preview snapshot for a session.

    Hydration is delegated to ``WorkspaceCacheService`` which keeps the
    snapshot in memory and invalidates it on disk changes (mtime + size
    + ``.git/HEAD`` + ``.git/index`` signature, plus an optional
    ``watchfiles`` real-time watcher for the most-recently-used N
    sessions). Hot path is sub-millisecond; warm path is a stat walk
    (~5-15 ms); cold path re-reads everything from disk via
    ``preview.hydrate_session`` + ``workspace.hydrate_files_from_disk``.

    Replaces the inline disk hydration that used to run inside
    ``socketio_bus.on_join_session`` (preview:snapshot block) and was
    blocking the first message of every session for 200-800 ms.
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

    # Resolve the WORKDIR (agent-facing path) - the cache service keys
    # its signature scan + optional watcher off the user-visible dir
    # where files actually land. Falls back to ``workspace`` (the
    # daemon-private path) when no separate workdir was set.
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

    # Inject session metadata so the SDK can drive lifecycle UX
    # (first-visit wizard, resume vs new, idle indicator, etc.).
    # We pull from the in-memory ConversationSession which has
    # ``created_at`` / ``last_active`` (epoch seconds), and count
    # message turns to derive an ``is_first_visit`` flag.
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
                    # Heuristic: a session with zero user messages and no
                    # persisted state has never been used. Useful for
                    # ``onFirstVisit`` SDK hooks.
                    "is_first_visit": (
                        turn_count == 0
                        and not (snapshot.get("state") or {})
                        and not any(
                            (snapshot.get("resources") or {}).values()
                        )
                    ),
                    "title": getattr(sess_obj, "title", "") or "",
                    # Frontend reads ``workspace`` as the agent-facing
                    # path (file tree, editor, status bar). Send the
                    # ``workdir`` value here - the daemon-private
                    # ``~/.digitorn/workspaces/...`` dir holds internal
                    # state (baselines, ``__sdk__/``) and must not
                    # surface in the UI. ``workdir`` is also exposed
                    # explicitly so newer clients can be unambiguous.
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
                    # ``daemon_workspace`` exposes the daemon-private
                    # ``~/.digitorn/workspaces/{app}/{sid}/`` path for
                    # diagnostics + tooling (test harness, admin UI).
                    # Frontends should NOT render files from this dir.
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

    from digitorn.core.events.envelope import TERMINAL_STATES, OpState
    from digitorn.core.runtime.session_store.bridge import (
        get_default_bridge,
    )

    user_id = getattr(request.state, "user_id", "") or ""
    terminal_names = {s.value for s in TERMINAL_STATES}

    # Scan persisted events from the in-memory SessionStore, group by
    # op_id, keep the latest. Same logic as before -- the data source
    # is now state.events instead of a Postgres scan.
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
        # Backward-compat: old rows without the contract - infer from
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

    - ``system_prompt`` - the full prompt the LLM sees (identity, tool
      instructions, behavioral guidelines, setup_summary, skills,
      module sections).
    - ``tools_schema`` - JSON schema of every tool (in-schema tokens).
    - ``messages`` - everything in ``ConversationSession.messages``
      (system + user + assistant + tool).
    - ``memory_injected`` - the memory module's rendered prompt
      section (goal, todos, facts).
    - ``setup_summary`` - setup step outputs injected at bootstrap.
    - ``skills`` - skill .md content concatenated.
    - ``total`` - sum matching what ``_call_llm`` actually sends.

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

    from digitorn.core.runtime.compaction import aestimate_tokens, estimate_tokens
    ctx = deployed.entry_context
    _provider = getattr(ctx, "provider", None)
    _model_name = getattr(_provider, "model", None) if _provider else None

    def _est(text: str | None) -> int:
        """Real token count for a free-form string. Uses the provider's
        tokenizer first (via litellm under the hood), falls back to
        litellm direct, then crude char/3 only when both fail.
        """
        if not text:
            return 0
        if _provider is not None and hasattr(_provider, "count_tokens"):
            try:
                return int(_provider.count_tokens(text))
            except Exception:
                pass
        if _model_name:
            try:
                from litellm import token_counter
                return int(token_counter(model=_model_name, text=text))
            except Exception:
                pass
        return max(1, len(text) // 3)

    # 1. Messages (what's in the session) - real tokenizer via provider.
    # Off-loop: tokenizer is CPU-bound and the first call may load a
    # multi-MB tokenizer model.
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
    except Exception:
        pass

    setup_text = ""
    try:
        setup = ctx.setup_summary or {}
        if isinstance(setup, dict):
            import json as _json
            setup_text = _json.dumps(setup, default=str)
    except Exception:
        pass

    skills_text = ""
    try:
        agent = getattr(ctx, "agent_def", None)
        if agent is not None:
            skills_text = getattr(agent, "skills_content", "") or ""
    except Exception:
        pass

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
    """Authoritative session state envelope - client's source of truth.

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

    # Optional gap-fill - replay events persisted after ``since_seq``.
    # Only events (kind='event') are replayed; messages live on the
    # history endpoint. Capped at 1000 rows - clients who lag further
    # than that should fetch the full history instead.
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

