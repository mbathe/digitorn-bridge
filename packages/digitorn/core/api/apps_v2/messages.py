"""Routes for the messages group, extracted from the legacy ``apps.py``.

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

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, model_validator

from digitorn.core.rate_limiter import RateLimitExceeded


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
    _get_workspace_status,
    _validate_id,
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

# Hard cap on the extracted text we keep on a FileRef for the
# direct-mode prepend path. Big contracts / books exceed this; the
# workspace channel still holds the full content for the tool-mode
# WsRead path. 200 KB of UTF-8 text is ~50k tokens.
_FULL_INJECT_CACHE_CAP = 200_000


router = APIRouter(tags=["apps"])



@router.post("/{app_id}/sessions/{session_id}/messages")
async def session_send_message(
    request: Request,
    app_id: str,
    session_id: str,
    body: SessionMessageRequest,
) -> AppResponse:
    """Send a message to a session. Events arrive via Socket.IO.

    **Queueing (Phase 3 - per-session FIFO queue)**

    When a turn is already running on this session, the message is
    enqueued instead of failing with ``session_busy``. The dispatcher
    picks the head of the queue as soon as the running turn finishes.
    The queue is persisted across daemon restarts.

    ``queue_mode`` controls the response:

    - ``async`` (default, recommended) - returns 202 immediately with
      ``{correlation_id, position, queue_depth}``. The client tracks the
      message via SSE events ``message_queued``, ``message_started``,
      ``message_done`` / ``message_cancelled``.
    - ``wait`` - legacy: block until the turn finishes, return the
      message data. Equivalent to the pre-queue behaviour for simple
      clients.

    Over-capacity (``session.queue.max_depth``) returns 429 + a
    ``queue_full`` event.
    """
    _validate_id(app_id)
    _validate_id(session_id, "session_id")
    manager = _get_manager(request)

    # Per-user sliding-window rate limit BEFORE any DB / session
    # creation work. Without this, a misbehaving client (or a stolen
    # token) can fire thousands of POST /messages per second; each
    # call pre-creates a session row, enqueues a message, kicks the
    # drain, and saturates the per-app queue. The RateLimiter is
    # already wired on app.state but was never called on this path.
    # Limit is per-(app, user) via the existing default quota; users
    # who legitimately need higher throughput get their per-(app,user)
    # quota raised via ``set_user_quota``. Anonymous / pre-auth
    # callers are skipped here because the auth middleware already
    # rejects them upstream; the check is cheap (one INCR) and pure
    # in-memory or Redis depending on backend.
    _rl_user_id = getattr(request.state, "user_id", None) or None
    try:
        _limiter = _get_rate_limiter(request)
    except HTTPException:
        # Limiter still booting -- fail open rather than 503 on a
        # legitimate request, but log so ops know rate limit is OFF.
        _limiter = None
        logger.warning(
            "messages_rate_limit_unavailable app=%s sid=%s user=%s",
            app_id, session_id, _rl_user_id,
        )
    if _limiter is not None and _rl_user_id:
        try:
            _limiter.check(app_id, user_id=_rl_user_id)
        except RateLimitExceeded as exc:
            raise HTTPException(
                status_code=429,
                detail={
                    "error": "rate_limit_exceeded",
                    "code": "rate_limited",
                    "category": "rate_limit",
                    "retry_after": exc.retry_after,
                    "limit": exc.limit,
                    "window_seconds": exc.window,
                    "message": str(exc),
                },
            ) from exc

    # Strong deploy check - not only does the manager know the app,
    # the DeployedApp must have a usable entry_context + modules. Apps
    # that survived a bootstrap crash can linger in `_deployed` with
    # a half-built state ("ghost apps"); POST /messages used to return
    # 200 for these but the dispatcher silently dropped everything.
    if not _is_deployed(request, app_id):
        _raise_not_deployed(request, app_id)
    _deployed_check = _get_deployed(request, app_id)
    if _deployed_check is None or getattr(_deployed_check, "entry_context", None) is None:
        raise HTTPException(
            status_code=503,
            detail=(
                f"App '{app_id}' is in a degraded state - deployed but "
                f"not fully initialized. Re-deploy to recover."
            ),
        )
    # BUG-072: reject cross-user message injection. New sessions still
    # pass (the handler creates them bound to the caller).
    _existing_session = await _require_session_create_or_owner(request, app_id, session_id)

    # Workdir liveness guard. For an existing session whose workdir
    # was deleted on disk (manual ``rm -rf``, future explicit project
    # delete, OS-level event), refuse to send new messages so the
    # agent doesn't end up writing into a non-existent cwd or, worse,
    # silently recreate the dir and lose the user's perception of
    # what was on disk. 410 Gone is the structured signal the web
    # client surfaces inline in the drawer.
    if _existing_session is not None:
        _wd = getattr(_existing_session, "workdir", "") or getattr(_existing_session, "workspace", "") or ""
        if _wd:
            from pathlib import Path as _Path
            if not _Path(_wd).exists():
                raise HTTPException(
                    status_code=410,
                    detail={
                        "error": "workdir_missing",
                        "code": "workdir_missing",
                        "category": "session",
                        "workdir": _wd,
                        "message": (
                            "This session's working directory no longer "
                            "exists on disk. The project may have been "
                            "deleted. Start a new session or pick another "
                            "project."
                        ),
                    },
                )

    _user_id = getattr(request.state, "user_id", None)
    _workspace = body.workspace
    _active_mode = body.mode  # NOTE: received only - merge layer pending
    if _active_mode:
        logger.info(
            "messages_mode_received app=%s sid=%s mode=%s",
            app_id, session_id, _active_mode,
        )

    # Pre-create the session row when the caller is sending the first
    # message on a brand-new sid. The contract is "POST /messages on a
    # fresh sid creates the session on the fly" - but the in-line create
    # in ``manager.chat()`` only fires when ``chat()`` is actually
    # invoked. Pre-chat PAUSED paths (credential_required gate, quota
    # rejected, exception thrown before ``manager.chat`` runs) skip
    # ``manager.chat`` entirely, leaving a phantom session_id: the POST
    # returned 200, the message was accepted, but a follow-up GET
    # returned 404 forever. Pre-creating here closes that gap.
    if _existing_session is None:
        try:
            from digitorn.core.app.sessions import ConversationSession
            from pathlib import Path as _Path
            # Mirror POST /sessions: every session gets a daemon-private
            # workspace under ~/.digitorn/workspaces/{app_id}/{sid}/ so the
            # agent's tools (Bash, Write, Read) always have a valid cwd.
            # Without this, the lazy-create path leaves ws/workdir empty,
            # shell._check_cwd's ctx fallback fails (ExecutionContext has
            # no app_id), and adapter.resolve_cwd rejects the call with
            # "No workspace resolved" before the bash command even runs.
            _daemon_ws = _Path.home() / ".digitorn" / "workspaces" / app_id / session_id
            try:
                _daemon_ws.mkdir(parents=True, exist_ok=True)
            except Exception:
                pass
            _ws_str = str(_daemon_ws)
            _new_session = ConversationSession(
                session_id=session_id,
                app_id=app_id,
                user_id=_user_id or "local",
                workspace=_workspace or _ws_str,
                workdir=_workspace or _ws_str,
            )
            _deployed_for_prompt = _get_deployed(request, app_id)
            if _deployed_for_prompt is not None:
                _sys_prompt = (
                    getattr(_deployed_for_prompt.entry_context, "system_prompt", "")
                    or ""
                )
                if _sys_prompt:
                    _new_session.add_system(_sys_prompt)
            await asyncio.to_thread(manager._session_store.put, _new_session)
        except Exception as _create_exc:
            logger.warning(
                "messages_pre_create_session_failed app=%s sid=%s: %s",
                app_id, session_id, _create_exc,
            )

    # Process images if provided
    _image_refs: list[dict[str, Any]] = []
    if body.images:
        try:
            from digitorn.core.image_store import get_image_store
            store = get_image_store()
            for img in body.images[:10]:  # Max 10 images
                mime = img.get("mime", "image/png")
                # BUG-092: some clients posted audio blobs through the
                # ``images`` field expecting the daemon to figure it
                # out. The blob then got stored as an ``image_ref``
                # with an audio MIME, which downstream vision
                # providers happily forwarded as a broken image. Refuse
                # non-image MIMEs here so the mistake surfaces.
                if mime and not mime.lower().startswith("image/"):
                    raise HTTPException(
                        status_code=415,
                        detail={
                            "error": "non_image_in_images_field",
                            "got": mime,
                            "message": (
                                "The ``images`` field only accepts "
                                "image/* blobs. For audio, POST to "
                                "/api/transcribe first and include "
                                "the returned text in ``message``."
                            ),
                        },
                    )
                data = img.get("data", "")
                name = img.get("name", "image")
                if data:
                    ref = await store.store_base64(
                        data, mime, session_id, alt_text=name,
                    )
                    _image_refs.append(ref.to_dict())
        except HTTPException:
            raise
        except Exception as exc:
            logger.warning("image_upload_failed: %s", exc)

    # Process non-image attachments (PDF / DOCX / CSV / code / …).
    # Two modes, picked by ``app.attachments_mode``:
    #
    #   * ``direct`` (default): extract the text and let the
    #     dispatch layer prepend it to the user message. Agent sees
    #     the content immediately, no tool call needed.
    #   * ``tool``: extract the text and mirror it into the
    #     workspace under ``attachments/<name>`` so the agent can
    #     call WsRead. The dispatch layer ONLY emits a manifest -
    #     the agent never sees the content until it asks for it.
    #
    # RAG indexing is intentionally NOT done here. Chat attachments
    # are a per-session concern, not a knowledge base. Apps that
    # want vector retrieval over a corpus load the rag module
    # explicitly and call its actions themselves.
    _file_refs: list[dict[str, Any]] = []
    if body.files:
        try:
            from digitorn.core.file_store import get_file_store
            from digitorn.core.file_extract import extract_text
            from ._attach_helpers import sniff_format
            file_store = get_file_store()
            ws_mod = None
            user_id = _caller_user_id(request) or None
            deployed = manager.get(app_id, user_id=user_id) if hasattr(manager, "get") else None
            if deployed is not None:
                ws_mod = deployed.modules.get("workspace")
            # Cap the number of attachments per message to keep the
            # daemon honest (matches the image_store cap above).
            for entry in body.files[:10]:
                mime = entry.get("mime", "application/octet-stream")
                name = entry.get("name", "attachment")
                data = entry.get("data", "")
                if not data:
                    continue
                ref = await file_store.store_base64(
                    data, mime,
                    session_id=session_id,
                    original_name=name,
                    message_id=body.client_message_id or "",
                    app_id=app_id,
                )

                # Sniff format from the bytes (magic-byte priority +
                # filename hint as tiebreaker). Persisted on the ref
                # so downstream callers (dispatch, /files endpoint)
                # use a single canonical answer.
                fmt = sniff_format(ref.path, filename_hint=name)
                file_store.update_index_status(
                    ref.file_id, session_id, format=fmt,
                )

                # Extract text via the format-aware ingestor stack.
                # Pure Python, no rag dependency, no embedding.
                raw_text = await extract_text(ref.path, format_hint=fmt)
                if not raw_text.strip():
                    file_store.update_index_status(
                        ref.file_id, session_id,
                        status="empty",
                        error="no extractable text",
                    )
                    _file_refs.append(ref.to_dict())
                    continue

                # Cache the extracted text on the ref for the
                # direct-mode prepend path. Hard-capped to keep
                # RAM bounded for huge contracts / books.
                cached_text = raw_text
                if len(cached_text) > _FULL_INJECT_CACHE_CAP:
                    cached_text = ""

                file_store.update_index_status(
                    ref.file_id, session_id,
                    status="indexed",
                    chunks=0,
                    extracted_text=cached_text,
                )

                # Mirror into the workspace under attachments/ so
                # the agent can call WsRead / WsGlob / WsGrep on
                # it. Required for tool mode, harmless for direct
                # mode (the agent simply won't need to call WsRead
                # because the text is already in its prompt). We
                # push ``raw_text`` (not the capped variant): the
                # workspace channel is the single source of truth
                # for tool-mode reads and WsRead's offset/limit
                # lets the agent paginate huge docs.
                if ws_mod is not None and raw_text:
                    try:
                        await ws_mod.register_attachment(
                            session_id,
                            ref.original_name or ref.file_id,
                            raw_text,
                            mime=ref.mime,
                            file_id=ref.file_id,
                        )
                    except Exception as exc:
                        logger.warning(
                            "attachment_mirror_failed file=%s err=%s",
                            ref.original_name or ref.file_id, exc,
                        )

                _file_refs.append(ref.to_dict())
        except Exception as exc:
            logger.warning("file_upload_failed: %s", exc)

    # ── Template attachment (one-turn system addendum + seed copy) ────
    #
    # When the user picked a template in the chat client's gallery and
    # attached it to this message, ``body.template_id`` carries the id.
    # We:
    #   1. Resolve the template entry from the app's compiled YAML.
    #   2. Copy ``install_dir/<seed_dir>/*`` into the session workspace
    #      (the agent will run ``npm install && npm run dev`` against
    #      the freshly-seeded project on its first turn).
    #   3. Stash ``template.system_prompt`` in ``_template_system_prompt``
    #      so the dispatcher passes it to ``manager.chat()`` and the
    #      agent loop prepends it as a ``role: system`` for this turn
    #      only. ``ctx`` is per-turn, so the directive doesn't leak to
    #      follow-up turns.
    _template_system_prompt: str = ""
    if body.template_id:
        deployed_for_tpl = _get_deployed(request, app_id)
        tpl_list = (
            getattr(deployed_for_tpl.compiled, "templates", None)
            if deployed_for_tpl is not None
            else None
        ) or []
        tpl_entry = next(
            (t for t in tpl_list if getattr(t, "id", None) == body.template_id),
            None,
        )
        if tpl_entry is None:
            raise HTTPException(
                status_code=404,
                detail=(
                    f"Template '{body.template_id}' not declared by app "
                    f"'{app_id}'. Add it under ``templates:`` in the "
                    f"app YAML."
                ),
            )

        # Resolve install_dir for the seed-source lookup. Same pattern
        # as ``apps_v2/templates.py``: prefer ``source_path`` parent
        # then fall back to the package registry resolver.
        from pathlib import Path as _Path
        from digitorn.core.packages.resolver import resolve_app_install_dir

        _install_dir: _Path | None = None
        _source_path = (
            getattr(deployed_for_tpl.compiled, "source_path", None)
            if hasattr(deployed_for_tpl, "compiled")
            else None
        )
        if _source_path is not None:
            _candidate = _Path(_source_path).parent
            if _candidate.is_dir():
                _install_dir = _candidate
        if _install_dir is None:
            _install_dir_str = await resolve_app_install_dir(
                app_id,
                user_id=_user_id,
                registry=getattr(request.app.state, "package_registry", None),
            )
            if _install_dir_str is not None:
                _candidate = _Path(_install_dir_str)
                if _candidate.is_dir():
                    _install_dir = _candidate

        if _install_dir is None:
            raise HTTPException(
                status_code=500,
                detail=(
                    f"App '{app_id}' install dir not resolvable; cannot "
                    f"apply template '{body.template_id}'."
                ),
            )

        # Resolve the session workspace. Mirrors ``manager._chat``'s
        # auto-default so the seeds land where the agent actually
        # operates. ``workspace_mode: auto`` defaults to
        # ``~/.digitorn/workspaces/<app_id>/<sid>/`` per the manager.
        _ws_mode = "auto"
        _yaml_ws = ""
        try:
            _exec = getattr(deployed_for_tpl.compiled, "execution", None)
            if _exec is not None:
                _ws_mode = getattr(_exec, "workspace_mode", "auto") or "auto"
                _yaml_ws = getattr(_exec, "workspace", "") or ""
        except Exception:
            pass
        _persisted_ws = ""
        try:
            _existing = await _require_session_create_or_owner(
                request, app_id, session_id,
            )
            if _existing is not None:
                _persisted_ws = getattr(_existing, "workspace", "") or ""
        except HTTPException:
            raise
        except Exception:
            pass

        _resolved_ws: _Path
        if _ws_mode == "fixed" and _yaml_ws:
            _resolved_ws = _Path(_yaml_ws).resolve()
        elif _workspace:
            _resolved_ws = _Path(_workspace).resolve()
        elif _persisted_ws:
            _resolved_ws = _Path(_persisted_ws).resolve()
        else:
            _resolved_ws = (
                _Path.home() / ".digitorn" / "workspaces" / app_id / session_id
            )

        _seed_src = (_install_dir / tpl_entry.seed_dir).resolve()
        try:
            _seed_src.relative_to(_install_dir.resolve())
        except ValueError:
            raise HTTPException(
                status_code=500,
                detail=(
                    f"Template '{body.template_id}' seed_dir escapes "
                    f"install_dir. Refusing to copy."
                ),
            )

        if not _seed_src.is_dir():
            raise HTTPException(
                status_code=500,
                detail=(
                    f"Template '{body.template_id}' seed_dir missing on "
                    f"disk: {tpl_entry.seed_dir}"
                ),
            )

        def _copy_seeds() -> int:
            import shutil
            _resolved_ws.mkdir(parents=True, exist_ok=True)
            count = 0
            for item in _seed_src.iterdir():
                target = _resolved_ws / item.name
                if item.is_dir():
                    shutil.copytree(item, target, dirs_exist_ok=True)
                    count += sum(1 for _ in target.rglob("*") if _.is_file())
                else:
                    shutil.copy2(item, target)
                    count += 1
            return count

        try:
            _copied = await asyncio.to_thread(_copy_seeds)
            logger.info(
                "template_seeds_copied app=%s sid=%s template=%s files=%d "
                "src=%s dst=%s",
                app_id, session_id, body.template_id, _copied,
                _seed_src, _resolved_ws,
            )
        except Exception as exc:
            logger.warning(
                "template_seeds_copy_failed app=%s sid=%s template=%s: %s",
                app_id, session_id, body.template_id, exc,
            )
            raise HTTPException(
                status_code=500,
                detail=f"Failed to copy template seeds: {exc}",
            )

        _template_system_prompt = tpl_entry.system_prompt or ""
        # Make sure the dispatcher uses the same workspace the seeds
        # were copied to, even when the caller didn't provide one.
        if not _workspace:
            _workspace = str(_resolved_ws)

        # ── Auto-register a ``template_preview`` attachment ────────────
        # Each lovable template ships a pre-built ``dist/`` alongside
        # its ``files/``. The agent will eventually re-publish from
        # the (customised) session workspace via ``PreviewPublish``,
        # but that takes 30-90 s on the first run. Meanwhile, point
        # the iframe at the pristine template snapshot so the user
        # sees something the moment they pick a card in the gallery
        # — no waiting on the agent's first turn. The agent's
        # ``published`` attachment will take priority once it lands
        # (see ``get_attachment`` resolution order).
        try:
            web_preview_mod = (
                deployed_for_tpl.modules.get("web_preview")
                if hasattr(deployed_for_tpl, "modules") else None
            )
            if web_preview_mod is not None:
                # Verify the template ships a pre-built dist before
                # registering; otherwise the iframe would 404.
                _tpl_dist = _install_dir / "templates" / body.template_id / "dist" / "index.html"
                if _tpl_dist.is_file():
                    web_preview_mod.register_template_preview(
                        session_id=session_id,
                        app_id=app_id,
                        template_id=body.template_id,
                        user_id=_user_id,
                    )
                    logger.info(
                        "template_preview_registered app=%s sid=%s template=%s",
                        app_id, session_id, body.template_id,
                    )
                else:
                    logger.info(
                        "template_preview_skipped (no dist) app=%s sid=%s "
                        "template=%s expected=%s",
                        app_id, session_id, body.template_id, _tpl_dist,
                    )
        except Exception as exc:
            # Non-fatal: template seeds are already copied, the
            # agent can still PreviewPublish on its own.
            logger.warning(
                "template_preview_register_failed app=%s sid=%s template=%s: %s",
                app_id, session_id, body.template_id, exc,
            )

    # ── /use_skill <name> <prompt> parser ─────────────────────────────
    #
    # User-facing skill invocation. Distinct from the agent-facing
    # ``use_skill`` tool: there the LLM decides when to load a skill;
    # here the user picks one from the composer and the daemon
    # forces the agent to follow its instructions for THIS turn.
    #
    # Resolution order:
    #   1. ``user_skills`` table (per-user, per-app) when the app
    #      has ``dev.allow_user_skills: true``. User-authored skills
    #      take precedence over app-declared ones so a user can
    #      override an app skill with their own variant.
    #   2. ``compiled.skills`` (app-declared, .md-backed).
    #
    # Found → strip the ``/use_skill <name>`` prefix from
    # ``body.message`` and concat the skill instructions into the
    # turn-scoped system prompt (same slot as ``template_id``). Not
    # found → 404 ``skill_not_found`` so the client surfaces a clean
    # error instead of letting the LLM silently mis-handle the
    # literal text.
    import re as _re
    _skill_match = _re.match(
        r"^\s*/use_skill\s+/?([a-zA-Z0-9_-]+)(?:\s+([\s\S]*))?$",
        body.message or "",
        flags=_re.IGNORECASE,
    )
    if _skill_match:
        _skill_name = _skill_match.group(1).strip().lower()
        _skill_prompt = (_skill_match.group(2) or "").strip()

        # Pull the deployed app for both user-flag check + app_skills lookup.
        _deployed_for_skill = _get_deployed(request, app_id)
        if _deployed_for_skill is None:
            _raise_not_deployed(request, app_id)

        _skill_body: str | None = None
        _skill_source = "unknown"

        # 1. user_skills table - only when the app opted in
        _allow_user = bool(getattr(
            _deployed_for_skill.compiled, "allow_user_skills", False,
        ))
        if _allow_user and _user_id:
            from digitorn.core.database import get_session_factory as _gsf
            from digitorn.core.models import UserSkill as _UserSkill
            from sqlalchemy import select as _select

            _factory = _gsf()
            async with _factory() as _db:
                _row = (
                    await _db.execute(
                        _select(_UserSkill.instructions)
                        .where(_UserSkill.user_id == _user_id)
                        .where(_UserSkill.app_id == app_id)
                        .where(_UserSkill.name == _skill_name)
                    )
                ).scalar_one_or_none()
                if _row:
                    _skill_body = _row
                    _skill_source = "user"

        # 2. compiled.skills (app-declared) as fallback
        if _skill_body is None:
            _app_skills_raw = list(
                getattr(_deployed_for_skill.compiled, "skills", []) or []
            )
            _wanted_cmd = f"/{_skill_name}"
            for _s in _app_skills_raw:
                _cmd = (_s.get("command") or "").strip().lower()
                if _cmd == _wanted_cmd or _cmd.lstrip("/") == _skill_name:
                    _skill_body = _s.get("content") or ""
                    _skill_source = "app"
                    break

        if _skill_body is None:
            raise HTTPException(
                status_code=404,
                detail=(
                    f"Skill '/{_skill_name}' not found for app '{app_id}'. "
                    f"Check ``GET /api/apps/{app_id}/skills`` for the "
                    f"available list."
                ),
            )

        # Wrap the skill body in a MANDATORY framing so the LLM treats
        # it as authoritative for this turn. The framing line is the
        # same regardless of source - the agent never needs to know
        # whether a skill came from YAML or the user table.
        _skill_directive = (
            f"# MANDATORY DIRECTIVE — Skill: /{_skill_name}\n\n"
            "Follow the instructions below to handle the user's next "
            "message. They take precedence over your default behaviour "
            "for this turn only.\n\n"
            "---\n\n"
            f"{_skill_body.strip()}"
        )
        # Concat with any pre-existing template_id directive. Template
        # comes first (template seeds + ambient instructions), skill
        # second (specific behaviour override). Separator matches the
        # framing convention.
        if _template_system_prompt:
            _template_system_prompt = (
                _template_system_prompt.rstrip()
                + "\n\n---\n\n"
                + _skill_directive
            )
        else:
            _template_system_prompt = _skill_directive

        # Strip the slash prefix so what the agent (and chat history)
        # actually sees is the user's real prompt, not the dispatch
        # command. Mutates the request body in place — ``extra="allow"``
        # on ``SessionMessageRequest`` keeps Pydantic from complaining.
        body.message = _skill_prompt
        logger.info(
            "skill_invoked app=%s sid=%s name=%s source=%s prompt_len=%d",
            app_id, session_id, _skill_name, _skill_source, len(_skill_prompt),
        )

    # ── Phase 3: per-session message queue ────────────────────────────
    #
    # Strategy:
    #   1. Always persist the message to the queue - gives us FIFO,
    #      crash-recovery, and cancellation for free.
    #   2. When the session has nothing in-flight, dispatch immediately.
    #      When it does, a post-turn hook drains the next queued msg
    #      (see _drain_queue_next below).
    #   3. ``queue_mode`` controls only the HTTP response shape:
    #      async = 202 with correlation_id, wait = block on awaiter.
    from digitorn.core.app import message_queue as _mq
    from digitorn.core.config import get_settings as _get_settings

    _qcfg = _get_settings().session.queue
    _mode = body.queue_mode or _qcfg.default_mode
    _uid = _user_id or "local"
    _bus_key = manager.event_bus.session_key(app_id, session_id, _uid)

    _skip_queue = True
    _reserved = False
    if _qcfg.enabled:
        _qdepth = await _mq.depth_for_session(session_id)
        _turn_running = await manager.is_turn_running(app_id, session_id)
        # A session with an approval pending still holds the turn's
        # future - `is_turn_running` returns False (the coroutine is
        # awaiting) but fast-pathing a new message would race with the
        # blocked turn and re-execute earlier logic. Treat pending
        # approvals as equivalent to a running turn so the new message
        # queues behind them.
        _has_pending_approval = False
        try:
            deployed_for_check = _get_deployed(request, app_id)
            aq = getattr(deployed_for_check, "approval_queue", None) if deployed_for_check else None
            if aq is not None:
                for r in aq.list_pending():
                    if r.get("session_id") == session_id:
                        _has_pending_approval = True
                        break
        except Exception:
            pass
        # Orphan-queue watchdog: when the session has queued messages
        # AND nothing's running AND no approval is holding - the drain
        # chain previously died (daemon crash mid-turn, task cancelled,
        # exception escaping the ``finally: _drain_queue_next``). Left
        # alone the queue sits forever and every new ``send_message``
        # appends to a stuck pile. We kick off a fresh drain task here
        # so the existing queue starts flowing again; the new message
        # this caller is about to enqueue will be picked up by the
        # same drain chain once those older entries finish.
        if (
            _qdepth > 0
            and not _turn_running
            and not _has_pending_approval
        ):
            logger.warning(
                "queue_orphan_detected app=%s session=%s depth=%d - "
                "restarting drain chain",
                app_id, session_id, _qdepth,
            )
            try:
                await _drain_queue_next(request, app_id, session_id, _uid)
            except Exception as exc:
                logger.warning("queue_orphan_drain_failed: %s", exc)
        if (
            _qdepth == 0
            and not _turn_running
            and not _has_pending_approval
            and body.queue_mode != "replace_last"
            and not _qcfg.auto_merge
        ):
            _reserved = manager.reserve_session(app_id, session_id)
            _skip_queue = _reserved
        else:
            _skip_queue = False

    # Authoritative seq attached to the user_message event when (and ONLY
    # when) the message is dispatched, NOT when it is queued. The chat
    # bubble on every client renders against this number; clients refuse
    # to add a user bubble to the chat without it (see the Flutter +
    # web composer logic). Stays ``0`` for queued (pending) messages -
    # those live only in the queue panel until the daemon dispatches
    # them, at which point a follow-up user_message event over the SSE
    # stream carries the canonical seq.
    _active_user_seq: int = 0

    if _qcfg.enabled and not _skip_queue:
        # Three enqueue strategies - the mode picks which helper runs.
        #
        # replace_last: if the tail of the queue is still queued,
        #   overwrite it with this new message in place. Client UX:
        #   "oops wrong message, use this one instead".
        #
        # auto_merge (config-driven): if a recent queued message from
        #   the same user is < auto_merge_window_s old, fold the new
        #   content into it - saves an LLM call when the user fires
        #   rapid follow-ups.
        #
        # default: plain append.
        merged = False
        replaced = False
        try:
            if body.queue_mode == "replace_last":
                entry, replaced = await _mq.replace_last_or_enqueue(
                    app_id=app_id, session_id=session_id, user_id=_uid,
                    message=body.message,
                    image_refs=_image_refs or [],
                    ttl_seconds=_qcfg.ttl_seconds,
                    max_depth=_qcfg.max_depth,
                    template_system_prompt=_template_system_prompt,
                )
            elif _qcfg.auto_merge:
                entry, merged = await _mq.merge_or_enqueue(
                    app_id=app_id, session_id=session_id, user_id=_uid,
                    message=body.message,
                    image_refs=_image_refs or [],
                    window_seconds=_qcfg.auto_merge_window_s,
                    ttl_seconds=_qcfg.ttl_seconds,
                    max_depth=_qcfg.max_depth,
                    template_system_prompt=_template_system_prompt,
                )
            else:
                entry = await _mq.enqueue(
                    app_id=app_id, session_id=session_id, user_id=_uid,
                    message=body.message,
                    image_refs=_image_refs or [],
                    ttl_seconds=_qcfg.ttl_seconds,
                    max_depth=_qcfg.max_depth,
                    template_system_prompt=_template_system_prompt,
                )
        except _mq.QueueFullError as exc:
            try:
                from digitorn.core.events.envelope import OpState as _OS
                # queue_full rejects a NEW message before it ever gets
                # a correlation_id - so the event is keyed by a fresh
                # synthetic op_id (there's no turn to correlate to).
                await manager.event_bus.emit(_turn_event(
                    "queue_full",
                    app_id=app_id, session_id=session_id,
                    user_id=_user_id or "local",
                    correlation_id="",
                    op_state=_OS.FAILED,
                    payload={
                        "depth": exc.depth, "max": exc.max_depth,
                        "session_id": session_id,
                    },
                ))
            except Exception:
                pass
            raise HTTPException(
                status_code=429,
                detail=(
                    f"Session queue full ({exc.depth}/{exc.max_depth}). "
                    "Cancel pending messages or wait before sending more."
                ),
            )

        # Stash the inbound JWT alongside the row so the drain worker
        # can re-publish it before a queued/replayed turn calls the
        # gateway. Without this, queued turns lose ``Authorization``.
        try:
            from digitorn.core.runtime.request_context import get_inbound_user_jwt
            _mq.attach_jwt(entry.id, get_inbound_user_jwt())
        except Exception:
            pass

        # ── Decide BEFORE emitting any event whether the message will
        #    actually wait in the queue, or whether it'll dispatch in
        #    < 1 ms (because the previous turn finished between our
        #    initial check and now). Re-checking here lets us emit the
        #    right event in either case, instead of always emitting a
        #    "queued" PENDING that the client briefly displays even when
        #    no real wait happens.
        _turn_active = await manager.is_turn_running(app_id, session_id)
        from digitorn.core.events.envelope import OpState as _OS

        if _turn_active:
            # ── True queue: emit PENDING events; client shows the queued
            #    badge until the running turn finishes and the drain
            #    chain emits ``message_started``.
            try:
                current_depth = await _mq.depth_for_session(session_id)
                if merged:
                    _evt = "message_merged"
                elif replaced:
                    _evt = "message_replaced"
                else:
                    _evt = "message_queued"
                await manager.event_bus.emit(_turn_event(
                    _evt,
                    app_id=app_id, session_id=session_id,
                    user_id=_user_id or "local",
                    correlation_id=entry.correlation_id,
                    op_state=_OS.PENDING,
                    payload={
                        "correlation_id": entry.correlation_id,
                        "position": entry.position,
                        "queue_depth": current_depth,
                        "message_preview": (entry.message or "")[:200],
                        "merged": merged,
                        "replaced": replaced,
                    },
                ))
            except Exception:
                current_depth = 0

            if not merged and not replaced:
                try:
                    await manager.event_bus.emit(_turn_event(
                        "user_message",
                        app_id=app_id, session_id=session_id,
                        user_id=_user_id or "local",
                        correlation_id=entry.correlation_id,
                        op_state=_OS.PENDING,
                        payload={
                            "session_id": session_id,
                            "role": "user",
                            "content": entry.message,
                            "images": [
                                img.get("id") or img.get("ref")
                                for img in (entry.image_refs or [])
                                if isinstance(img, dict)
                            ],
                            "correlation_id": entry.correlation_id,
                            "client_message_id": body.client_message_id or "",
                            "pending": True,
                        },
                    ))
                except Exception:
                    pass

            # ── Final drain-kick safety (UNCONDITIONAL). The
            #    turn-active check at line 359 may return a stale
            #    True even after the previous turn completed (the
            #    flag isn't always cleared synchronously, especially
            #    with the persist-in-bg change). _drain_queue_next is
            #    idempotent: it pops the head atomically and returns
            #    immediately if the queue is empty or another task
            #    already grabbed it. Calling it after every enqueue
            #    closes the warm-path gap where messages could sit
            #    queued forever.
            try:
                await _drain_queue_next(request, app_id, session_id, _uid)
            except Exception as exc:
                logger.debug("post_enqueue_drain_kick failed: %s", exc)

            if _mode == "wait":
                fut = _mq.awaiter_future(entry.correlation_id)
                try:
                    await fut
                except Exception as exc:
                    raise HTTPException(status_code=500, detail=str(exc))
                return AppResponse(
                    success=True,
                    data={
                        "session_id": session_id,
                        "status": "completed",
                        "correlation_id": entry.correlation_id,
                    },
                )
            return AppResponse(
                success=True,
                data={
                    "session_id": session_id,
                    "status": "queued",
                    "correlation_id": entry.correlation_id,
                    "position": entry.position,
                    "queue_depth": current_depth,
                    "merged": merged,
                    "replaced": replaced,
                },
            )

        # ── Fast-dispatch path: the row was just enqueued, but no turn
        #    is running anymore (the previous one finished between our
        #    initial check and this re-check, or our depth check tripped
        #    on an orphan row that was just drained). Pop the head and
        #    fall through to ``_run_turn`` - emit RUNNING events so the
        #    client UX matches the original fast-path (no queued flash).
        _head = await _mq.next_queued(session_id)
        if _head is None:
            # Rare race: the head was cancelled between our checks.
            return AppResponse(
                success=True,
                data={
                    "session_id": session_id,
                    "status": "queued",
                    "correlation_id": entry.correlation_id,
                    "position": entry.position,
                },
            )
        _active_correlation_id = _head.correlation_id
        _active_queue_row_id = _head.id
        if _head.correlation_id != entry.correlation_id:
            body.message = _head.message
            _image_refs = list(_head.image_refs or [])

        # Tell the client about merge/replace mutations even on the
        # fast-dispatch path so the existing message bubble updates in
        # place. RUNNING op_state because the dispatch is starting now.
        if merged or replaced:
            try:
                _evt = "message_merged" if merged else "message_replaced"
                await manager.event_bus.emit(_turn_event(
                    _evt,
                    app_id=app_id, session_id=session_id,
                    user_id=_user_id or "local",
                    correlation_id=_active_correlation_id,
                    op_state=_OS.RUNNING,
                    payload={
                        "correlation_id": _active_correlation_id,
                        "position": _head.position,
                        "merged": merged,
                        "replaced": replaced,
                    },
                ))
            except Exception:
                pass

        # Fresh-enqueue (not merged/replaced) → emit user_message RUNNING
        # mirroring the original fast-path branch below.
        if not merged and not replaced:
            try:
                _active_user_seq = await manager.event_bus.emit(_turn_event(
                    "user_message",
                    app_id=app_id, session_id=session_id,
                    user_id=_user_id or "local",
                    correlation_id=_active_correlation_id,
                    op_state=_OS.RUNNING,
                    payload={
                        "session_id": session_id,
                        "role": "user",
                        "content": body.message,
                        "images": [
                            img.get("id") or img.get("ref")
                            for img in (_image_refs or [])
                            if isinstance(img, dict)
                        ],
                        "correlation_id": _active_correlation_id,
                        "client_message_id": body.client_message_id or "",
                        "pending": False,
                    },
                )) or 0
            except Exception:
                pass

        # `message_started` is now emitted exclusively by
        # `dispatch_turn` (single source of truth). The position is
        # surfaced via the TurnEntry so the event payload still carries
        # the queue head's index when relevant.
        _active_position = _head.position
    else:
        import uuid as _uuid
        _active_correlation_id = f"fp-{_uuid.uuid4().hex[:12]}"
        _active_queue_row_id = ""
        _active_position = 0

        try:
            from digitorn.core.events.envelope import OpState as _OS
            _active_user_seq = await manager.event_bus.emit(_turn_event(
                "user_message",
                app_id=app_id, session_id=session_id,
                user_id=_user_id or "local",
                correlation_id=_active_correlation_id,
                op_state=_OS.RUNNING,
                payload={
                    "session_id": session_id,
                    "role": "user",
                    "content": body.message,
                    "images": [
                        img.get("id") or img.get("ref")
                        for img in (_image_refs or [])
                        if isinstance(img, dict)
                    ],
                    "correlation_id": _active_correlation_id,
                    "client_message_id": body.client_message_id or "",
                    "pending": False,
                },
            )) or 0
        except Exception:
            pass

    async def _run_turn():
        # Single source of truth for "run a chat turn end-to-end":
        # cred check, heartbeat, manager.chat(), event emission, error
        # classification all live inside `dispatch_turn`. We just hand
        # it the entry, await the outcome, then handle queue terminal
        # bookkeeping (chain to next, mark row done/failed/paused).
        from ._dispatch import (
            dispatch_turn, TurnEntry, TurnSource, TurnStatus,
        )
        outcome = await dispatch_turn(
            request, app_id, session_id,
            entry=TurnEntry(
                correlation_id=_active_correlation_id or "",
                message=body.message,
                workspace=_workspace,
                image_refs=_image_refs if _image_refs else None,
                client_message_id=body.client_message_id or "",
                queue_row_id=_active_queue_row_id or "",
                position=_active_position,
                template_system_prompt=_template_system_prompt,
            ),
            user_id=_user_id or "local",
            source=TurnSource.FAST,
        )
        # PAUSED: credential gate hit. Mark the row terminal with
        # `credential_required` so `is_turn_running` returns False;
        # otherwise the user's RETRY queues behind a stuck row. The
        # client already sees `credential_required` on the bubble; the
        # next dispatch happens when they click RETRY (fresh POST).
        if outcome.status == TurnStatus.PAUSED:
            if _active_queue_row_id:
                try:
                    await _mq.mark_failed(
                        _active_queue_row_id,
                        error_code="credential_required",
                    )
                except Exception:
                    pass
            # The pre-chat credential gate raised before ``manager.chat()``
            # ran, so its ``finally:`` never discarded the active key
            # we set via ``reserve_session``. Without this explicit
            # release, ``is_session_active`` keeps returning True forever
            # and ``abort`` cannot unstick the session (its ``task.cancel``
            # is a no-op when no task was ever registered).
            if _reserved:
                try:
                    manager.release_session(app_id, session_id)
                except Exception:
                    logger.debug(
                        "paused_release_session_failed", exc_info=True,
                    )
            return
        # COMPLETED / FAILED / CANCELLED: dispatch_turn already flipped
        # the queue row + scheduled the next chain dispatch internally
        # (Step 6 ordering: flip BEFORE emitting message_done so the
        # next POST sees a clean daemon). Nothing more for us to do.

    # ── Dispatch agent turn to a worker thread ────────────────────────
    # The turn runs in its own event loop inside a thread from the worker
    # pool. The main event loop stays free for HTTP/SSE at all times.
    # A semaphore caps concurrency - beyond _MAX_CONCURRENT_TURNS the
    # endpoint returns 503 immediately instead of starving the daemon.
    if _turn_semaphore.locked() and _turn_semaphore._value == 0:
        if _reserved:
            manager.release_session(app_id, session_id)
        return AppResponse(
            success=False,
            data={"error": "Server busy - too many concurrent agent turns", "retry": True},
        )

    async def _guarded_turn():
        async with _turn_semaphore:
            await _run_turn()

    task = asyncio.create_task(_guarded_turn())
    _active_turn_tasks.add(task)

    def _on_turn_done(t: asyncio.Task) -> None:
        _active_turn_tasks.discard(t)
        exc = t.exception() if not t.cancelled() else None
        if exc is not None:
            logger.error(
                "TURN_TASK_CRASHED app=%s session=%s: %s",
                app_id, session_id, exc, exc_info=exc,
            )

    task.add_done_callback(_on_turn_done)

    # Embed the authoritative state envelope in the POST response so
    # the client doesn't have to wait for the first SSE event to know
    # "a turn is running". Eliminates the race where a client misses
    # ``message_started`` and never animates the send button.
    try:
        state_envelope = await manager.build_state_envelope(
            app_id, session_id, _uid,
        )
    except Exception as exc:
        logger.debug("build_state_envelope failed on POST response: %s", exc)
        state_envelope = None

    return AppResponse(
        success=True,
        data={
            "session_id": session_id,
            "status": "accepted",
            "correlation_id": _active_correlation_id or None,
            "client_message_id": body.client_message_id,
            # Authoritative chat seq for the user_message just emitted.
            # Non-zero ONLY when the message was dispatched immediately
            # (fast-path or queue-head fresh-enqueue). For pure-queued
            # messages it stays ``null``: clients must NOT add a chat
            # bubble in that case - the seq arrives later via the SSE
            # ``user_message`` event when the daemon dispatches the
            # message from the queue. Single source of truth for
            # ordering and persistence.
            "seq": _active_user_seq if _active_user_seq > 0 else None,
            "state": state_envelope,
        },
    )

