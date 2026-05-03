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


@router.api_route(
    "/{app_id}/preview/{buffer_key:path}",
    methods=["GET", "HEAD"],
)
async def preview_buffer(request: Request, app_id: str, buffer_key: str):
    """Serve the app's preview UI.

    Single entry point for the iframe. ``_proxy_preview_http`` is the
    single source of truth and handles all three cases uniformly:
      1. Session attachment from web_preview (proxy or static).
      2. App's own web/dist/ at install dir (declarative case).
      3. 404 with a helpful hint pointing to PreviewProxy / PreviewStatic.

    HEAD is accepted alongside GET because the web/Flutter availability
    probes use ``HEAD`` to test ``/preview/`` without pulling the full
    body. Without explicit HEAD support, FastAPI returns 405 and the
    probe always reports unavailable.

    Query params:
        session_id: Identifies the session for attachment lookup.
        name: Optional attachment name (default 'default') for
              multi-preview apps.
    """
    _validate_id(app_id)
    deployed = _get_deployed(request, app_id)
    if not deployed:
        _raise_not_deployed(request, app_id)
    return await _proxy_preview_http(request, app_id, buffer_key or "")

