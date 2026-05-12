"""Routes for the preview group, extracted from the legacy ``apps.py``.

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



@router.get("/{app_id}/preview-bootstrap")
async def preview_bootstrap(request: Request, app_id: str, session_id: str = ""):
    """Bootstrap config for an embedded preview iframe.

    Apps that prefer a single round-trip over parsing query params
    can fetch this at startup. Returns everything needed to wire the
    Digitorn preview SDK:

    ```json
    {
        "app_id": "my-app",
        "session_id": "abc-123",
        "base_url": "http://127.0.0.1:8000",
        "socket_url": "http://127.0.0.1:8000/events",
        "workspace_url": "/api/apps/my-app/sessions/abc-123/workspace",
        "capabilities": {
            "preview_dev_server": true,
            "preview_static_dist": true,
            "git": false
        }
    }
    ```

    Query params:
        session_id: Optional - the iframe's active session.
    """
    _validate_id(app_id)
    deployed = _get_deployed(request, app_id)
    if not deployed:
        _raise_not_deployed(request, app_id)

    has_static_dist = False
    try:
        source_path = getattr(deployed.compiled, "source_path", None)
        if source_path is not None:
            if (Path(source_path).parent / "web" / "dist" / "index.html").is_file():
                has_static_dist = True
        if not has_static_dist:
            pkg_registry = getattr(request.app.state, "package_registry", None)
            if pkg_registry is not None:
                row = await pkg_registry.get(app_id)
                install_dir = (
                    row.get("install_dir") if isinstance(row, dict)
                    else getattr(row, "install_dir", None)
                ) if row is not None else None
                if install_dir and (
                    Path(install_dir) / "web" / "dist" / "index.html"
                ).is_file():
                    has_static_dist = True
    except Exception:
        pass

    has_attachment = False
    try:
        web_preview_mod = (
            deployed.modules.get("web_preview")
            if hasattr(deployed, "modules") else None
        )
        if web_preview_mod is not None and session_id:
            has_attachment = bool(web_preview_mod.list_session(session_id))
    except Exception:
        pass

    base_url = str(request.base_url).rstrip("/")
    workspace_url = (
        f"/api/apps/{app_id}/sessions/{session_id}/workspace" if session_id else ""
    )

    return {
        "app_id": app_id,
        "session_id": session_id,
        "base_url": base_url,
        "socket_url": f"{base_url}/events",
        "workspace_url": workspace_url,
        "capabilities": {
            "preview_attached": has_attachment,
            "preview_static_dist": has_static_dist,
        },
    }


@router.get("/{app_id}/web-preview")
async def web_preview_lookup(
    request: Request, app_id: str, session_id: str, name: str = "default",
):
    """Return the URL the iframe should load for this session's preview.

    The frontend hits this on session join (and as a poll fallback when
    a Socket.IO ``web_preview:attached`` event was missed). Two flavours:

      - ``proxy`` attachment: ``url`` is the dev-server's direct-connect
        URL. Browser hits it straight, daemon stays out of the data
        path.
      - ``bundled`` attachment: ``url`` points at the daemon's
        ``/web-static/`` route. The app ships its own ``web/dist/``
        and uses the @digitorn/preview-sdk - auto-registered at
        session create.

    Response (200): ``{url, type, name, session_id, port?, host?}``.
    Response (404): no attachment for this (session, name).
    """
    _validate_id(app_id)
    deployed = _get_deployed(request, app_id)
    if not deployed:
        _raise_not_deployed(request, app_id)
    mod = (
        deployed.modules.get("web_preview")
        if hasattr(deployed, "modules") else None
    )
    # ``no preview yet'' is a valid state, NOT an HTTP error. The web
    # client polls this endpoint every few seconds while the user is
    # on the workspace tab; returning 404 would flood the browser
    # console with red ``Failed to load resource`` lines that aren't
    # real failures. Always 200 — the caller checks ``attached``
    # and ``url`` to decide what to render.
    if mod is None:
        return {
            "attached": False,
            "url": None,
            "reason": f"web_preview module not loaded for app '{app_id}'",
        }
    att = mod.get_attachment(session_id, name)
    if att is None:
        return {"attached": False, "url": None}
    out: dict[str, Any] = {
        "attached": True,
        "url": mod.attachment_url(att),
        "type": att.type,
        "name": att.name,
        "session_id": att.session_id,
    }
    if att.type == "proxy":
        out["port"] = att.port
        out["host"] = att.host
    return out


@router.api_route(
    "/{app_id}/web-static/{path:path}",
    methods=["GET", "HEAD"],
)
async def web_static(request: Request, app_id: str, path: str):
    """Serve a file from the app's bundled ``web/dist/`` directory.

    Used by SDK apps that ship a pre-built UI (digitorn-builder,
    digitorn-react-sandbox, ...). The bundle MUST be built with
    ``base: './'`` so relative URLs resolve under this route.

    Sandbox: refuses any path that walks outside ``install_dir/web/dist``.
    """
    from pathlib import Path as _Path
    from starlette.responses import FileResponse, Response

    _validate_id(app_id)
    deployed = _get_deployed(request, app_id)
    if not deployed:
        _raise_not_deployed(request, app_id)

    from digitorn.core.packages.resolver import resolve_app_install_dir
    user_id = getattr(request.state, "user_id", None)
    pkg_registry = getattr(request.app.state, "package_registry", None)

    install_dir = None
    source_path = (
        getattr(deployed.compiled, "source_path", None)
        if hasattr(deployed, "compiled") else None
    )
    if source_path is not None:
        install_dir = _Path(source_path).parent
    else:
        install_dir = await resolve_app_install_dir(
            app_id, user_id=user_id, registry=pkg_registry,
        )

    if install_dir is None:
        raise HTTPException(status_code=404, detail="install dir not found")

    dist_root = _Path(install_dir) / "web" / "dist"
    if not dist_root.is_dir():
        raise HTTPException(status_code=404, detail="web/dist not built")

    rel = (path or "").lstrip("/")
    if not rel:
        target = dist_root / "index.html"
    else:
        target = (dist_root / rel).resolve()
        try:
            target.relative_to(dist_root.resolve())
        except ValueError:
            return Response(status_code=403)
        if target.is_dir():
            target = target / "index.html"
        if not target.is_file():
            target = dist_root / "index.html"

    headers: dict[str, str] = {}
    p = str(target).lower()
    if p.endswith(".html") or p.endswith(".htm"):
        headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    return FileResponse(str(target), headers=headers)

