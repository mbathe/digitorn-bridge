"""Routes for the messages group."""

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

_FULL_INJECT_CACHE_CAP = 200_000


router = APIRouter(tags=["apps"])


@router.post("/{app_id}/sessions/{session_id}/messages")
async def session_send_message(
    request: Request,
    app_id: str,
    session_id: str,
    body: SessionMessageRequest,
) -> AppResponse:
    """Send a message to a session. Events arrive via Socket.IO."""
    _validate_id(app_id)
    _validate_id(session_id, "session_id")
    manager = _get_manager(request)

    # Per-(app, user) rate limit before any DB / session work.
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

    # Strong deploy check: reject ghost apps where `entry_context` is missing.
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
    # reject cross-user message injection; new sessions still pass (handler creates them bound to the caller).
    _existing_session = await _require_session_create_or_owner(request, app_id, session_id)

    # refuse new messages when the session's workdir vanished on disk; 410 lets the web client surface the gap inline.
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
    _active_mode = body.mode
    if _active_mode:
        logger.info(
            "messages_mode_received app=%s sid=%s mode=%s",
            app_id, session_id, _active_mode,
        )

    # pre-create the session row so PAUSED paths (credential_required, quota) don't leave a phantom sid that GET 404s.
    if _existing_session is None:
        try:
            from digitorn.core.app.sessions import ConversationSession
            from pathlib import Path as _Path
            # every session gets a daemon-private workspace so the agent's tools always have a valid cwd.
            _daemon_ws = _Path.home() / ".digitorn" / "workspaces" / app_id / session_id
            try:
                _daemon_ws.mkdir(parents=True, exist_ok=True)
            except Exception as exc:
                logger.debug("messages best-effort block failed: %s", exc)
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
                # refuse non-image MIMEs so audio/etc. don't get smuggled into the vision pipeline.
                if mime and not mime.lower().startswith("image/"):
                    raise HTTPException(
                        status_code=415,
                        detail={
                            "error": "non_image_in_images_field",
                            "got": mime,
                            "message": (
                                "The `images` field only accepts "
                                "image/* blobs. For audio, POST to "
                                "/api/transcribe first and include "
                                "the returned text in `message`."
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

                cached_text = raw_text
                if len(cached_text) > _FULL_INJECT_CACHE_CAP:
                    cached_text = ""

                file_store.update_index_status(
                    ref.file_id, session_id,
                    status="indexed",
                    chunks=0,
                    extracted_text=cached_text,
                )

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

    # Template attachment: seed the workspace and inject the template's
    # system prompt for this turn only.
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
                    f"'{app_id}'. Add it under `templates:` in the "
                    f"app YAML."
                ),
            )

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

        _ws_mode = "auto"
        _yaml_ws = ""
        try:
            _exec = getattr(deployed_for_tpl.compiled, "execution", None)
            if _exec is not None:
                _ws_mode = getattr(_exec, "workspace_mode", "auto") or "auto"
                _yaml_ws = getattr(_exec, "workspace", "") or ""
        except Exception as exc:
            logger.debug("messages best-effort block failed: %s", exc)
        _persisted_ws = ""
        try:
            _existing = await _require_session_create_or_owner(
                request, app_id, session_id,
            )
            if _existing is not None:
                _persisted_ws = getattr(_existing, "workspace", "") or ""
        except HTTPException:
            raise
        except Exception as exc:
            logger.debug("messages best-effort block failed: %s", exc)

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
                    f"Check `GET /api/apps/{app_id}/skills` for the "
                    f"available list."
                ),
            )

        _skill_directive = (
            f"# MANDATORY DIRECTIVE - Skill: /{_skill_name}\n\n"
            "Follow the instructions below to handle the user's next "
            "message. They take precedence over your default behaviour "
            "for this turn only.\n\n"
            "---\n\n"
            f"{_skill_body.strip()}"
        )
        if _template_system_prompt:
            _template_system_prompt = (
                _template_system_prompt.rstrip()
                + "\n\n---\n\n"
                + _skill_directive
            )
        else:
            _template_system_prompt = _skill_directive

        body.message = _skill_prompt
        logger.info(
            "skill_invoked app=%s sid=%s name=%s source=%s prompt_len=%d",
            app_id, session_id, _skill_name, _skill_source, len(_skill_prompt),
        )

    # Client-supplied turn addendum: appended to the same one-turn
    # system slot as template / skill directives.
    if body.system_addendum:
        _addendum = body.system_addendum.strip()
        if _addendum:
            _framed = (
                "# Client-side context (one-turn)\n\n"
                "The iframe surfaces this signal because it observed state "
                "the user can't easily restate. Honour it for THIS turn "
                "only - do not echo it back unless the user asks.\n\n"
                "---\n\n"
                f"{_addendum}"
            )
            if _template_system_prompt:
                _template_system_prompt = (
                    _template_system_prompt.rstrip()
                    + "\n\n---\n\n"
                    + _framed
                )
            else:
                _template_system_prompt = _framed

    from digitorn.core.api.apps_v2.slash_dispatch import (
        SLASH_CMD_RE as _SLASH_CMD_RE,
        lookup_slash_action as _lookup_slash_action,
        dispatch as _slash_dispatch_run,
    )
    _slash_m = _SLASH_CMD_RE.match(body.message or "")
    if _slash_m:
        _slash_cmd = _slash_m.group(1).strip().lower()
        _slash_args = (_slash_m.group(2) or "").strip()
        _deployed_for_slash = _get_deployed(request, app_id)
        if _deployed_for_slash is not None:
            _slash_action = _lookup_slash_action(
                _deployed_for_slash.compiled, _slash_cmd,
            )
            if _slash_action is not None:
                import uuid as _uuid_slash
                from digitorn.core.events.envelope import OpState as _OS_SLASH

                _slash_corr = f"slash-{_uuid_slash.uuid4().hex[:12]}"

                # Synthetic user_message - render the slash bubble in
                # the chat so the user sees what they typed.
                try:
                    await manager.event_bus.emit(_turn_event(
                        "user_message",
                        app_id=app_id, session_id=session_id,
                        user_id=_user_id or "local",
                        correlation_id=_slash_corr,
                        op_state=_OS_SLASH.COMPLETED,
                        payload={
                            "session_id": session_id,
                            "role": "user",
                            "content": body.message,
                            "correlation_id": _slash_corr,
                            "client_message_id": body.client_message_id or "",
                            "pending": False,
                        },
                    ))
                except Exception as exc:
                    logger.debug("slash user_message emit failed: %s", exc)

                _slash_result = await _slash_dispatch_run(
                    _slash_action,
                    deployed=_deployed_for_slash,
                    app_id=app_id,
                    session_id=session_id,
                    user_id=_user_id,
                    args=_slash_args,
                    manager=manager,
                )

                try:
                    await manager.event_bus.emit(_turn_event(
                        "message_started",
                        app_id=app_id, session_id=session_id,
                        user_id=_user_id or "local",
                        correlation_id=_slash_corr,
                        op_state=_OS_SLASH.RUNNING,
                        payload={
                            "session_id": session_id,
                            "correlation_id": _slash_corr,
                        },
                    ))
                except Exception as exc:
                    logger.debug("slash message_started emit failed: %s", exc)

                try:
                    await manager.event_bus.emit(_turn_event(
                        "token",
                        app_id=app_id, session_id=session_id,
                        user_id=_user_id or "local",
                        correlation_id=_slash_corr,
                        op_state=_OS_SLASH.RUNNING,
                        payload={
                            "session_id": session_id,
                            "correlation_id": _slash_corr,
                            "delta": _slash_result.message,
                            "count": len(_slash_result.message),
                            "slash_synthetic": True,
                        },
                    ))
                except Exception as exc:
                    logger.debug("slash token emit failed: %s", exc)

                try:
                    await manager.event_bus.emit(_turn_event(
                        "message_done",
                        app_id=app_id, session_id=session_id,
                        user_id=_user_id or "local",
                        correlation_id=_slash_corr,
                        op_state=_OS_SLASH.COMPLETED,
                        payload={
                            "session_id": session_id,
                            "correlation_id": _slash_corr,
                            "slash_synthetic": True,
                        },
                    ))
                except Exception as exc:
                    logger.debug("slash message_done emit failed: %s", exc)

                # Close the synthetic turn; the client uses
                # `turn_terminal` to flip `isSending` off.
                try:
                    await manager.event_bus.emit(_turn_event(
                        "turn_terminal",
                        app_id=app_id, session_id=session_id,
                        user_id=_user_id or "local",
                        correlation_id=_slash_corr,
                        op_state=_OS_SLASH.COMPLETED,
                        payload={
                            "session_id": session_id,
                            "correlation_id": _slash_corr,
                            "status": "completed",
                        },
                    ))
                except Exception as exc:
                    logger.debug("slash turn_terminal emit failed: %s", exc)

                logger.info(
                    "slash_dispatched app=%s sid=%s cmd=/%s args_len=%d",
                    app_id, session_id, _slash_cmd, len(_slash_args),
                )

                return AppResponse(
                    success=True,
                    data={
                        "session_id": session_id,
                        "status": "slash_handled",
                        "command": f"/{_slash_cmd}",
                        "correlation_id": _slash_corr,
                        "client_message_id": body.client_message_id,
                    },
                )

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
        # Treat pending approvals as a running turn so the new message
        # queues behind them (the awaiting turn would otherwise race).
        _has_pending_approval = False
        try:
            deployed_for_check = _get_deployed(request, app_id)
            aq = getattr(deployed_for_check, "approval_queue", None) if deployed_for_check else None
            if aq is not None:
                for r in aq.list_pending():
                    if r.get("session_id") == session_id:
                        _has_pending_approval = True
                        break
        except Exception as exc:
            logger.debug("messages best-effort block failed: %s", exc)
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

    _active_user_seq: int = 0

    if _qcfg.enabled and not _skip_queue:
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
            except Exception as exc:
                logger.debug("messages best-effort block failed: %s", exc)
            raise HTTPException(
                status_code=429,
                detail=(
                    f"Session queue full ({exc.depth}/{exc.max_depth}). "
                    "Cancel pending messages or wait before sending more."
                ),
            )

        try:
            from digitorn.core.runtime.request_context import get_inbound_user_jwt
            _mq.attach_jwt(entry.id, get_inbound_user_jwt())
        except Exception as exc:
            logger.debug("messages best-effort block failed: %s", exc)

        # Re-check before emitting so we send PENDING only when the message
        # actually waits, not when the prior turn already finished.
        _turn_active = await manager.is_turn_running(app_id, session_id)
        from digitorn.core.events.envelope import OpState as _OS

        if _turn_active:
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
                except Exception as exc:
                    logger.debug("messages best-effort block failed: %s", exc)

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

        # Fast-dispatch path: queued but the prior turn finished mid-check.
        # Pop the head and emit RUNNING events for a clean fast-path UX.
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
            except Exception as exc:
                logger.debug("messages best-effort block failed: %s", exc)

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
            except Exception as exc:
                logger.debug("messages best-effort block failed: %s", exc)

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
        except Exception as exc:
            logger.debug("messages best-effort block failed: %s", exc)

    async def _run_turn():
        # `dispatch_turn` owns the end-to-end run; we only chain the
        # queue terminal bookkeeping (next entry, row status).
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
                mode_id=_active_mode or "",
            ),
            user_id=_user_id or "local",
            source=TurnSource.FAST,
        )
        # PAUSED (credential gate): mark the row terminal so retries
        # aren't queued behind a stuck row.
        if outcome.status == TurnStatus.PAUSED:
            if _active_queue_row_id:
                try:
                    await _mq.mark_failed(
                        _active_queue_row_id,
                        error_code="credential_required",
                    )
                except Exception as exc:
                    logger.debug("messages best-effort block failed: %s", exc)
            # Explicit release: `manager.chat()` never ran so its
            # `finally` did not drop the `reserve_session` key.
            if _reserved:
                try:
                    manager.release_session(app_id, session_id)
                except Exception:
                    logger.debug(
                        "paused_release_session_failed", exc_info=True,
                    )
            return
        # COMPLETED / FAILED / CANCELLED: `dispatch_turn` already
        # flipped the queue row + scheduled the next chain dispatch.

    # caps concurrency so the daemon returns 503 instead of starving.
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
            # Non-zero only for immediately-dispatched messages;
            # queued messages get their seq via the SSE event later.
            "seq": _active_user_seq if _active_user_seq > 0 else None,
            "state": state_envelope,
        },
    )

