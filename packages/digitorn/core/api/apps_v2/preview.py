"""Routes for the preview group, extracted from the legacy ``apps.py``.

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



@router.api_route(
    "/{app_id}/preview-server/proxy/{path:path}",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"],
)
async def preview_server_proxy(request: Request, app_id: str, path: str = ""):
    """Reverse-proxy HTTP traffic to the app's dev server.

    Flutter points an iframe at ``/api/apps/{app_id}/preview-server/proxy/``
    to render the dev UI inside the Digitorn admin panel. HMR websockets
    use the matching ``/preview-server/ws/{path:path}`` upgrade route.
    """
    _validate_id(app_id)
    return await _proxy_preview_http(request, app_id, path)


@router.websocket("/{app_id}/preview-server/ws/{path:path}")
async def preview_server_ws(websocket: Any, app_id: str, path: str = ""):
    """Upgrade + bridge a WebSocket to the app's dev server for HMR.

    Dev servers (Vite, Next.js, Remix) push hot-reload messages over
    a WebSocket. This handler accepts the client upgrade, connects a
    matching upstream WebSocket to ``ws://127.0.0.1:{port}/{path}``, and
    pumps frames bidirectionally until either side closes.
    """
    try:
        import websockets
    except ImportError:
        await websocket.close(code=1011, reason="websockets library unavailable")
        return

    _validate_id(app_id)

    # Resolve deployed app via the same helper as the HTTP routes.
    # We need an explicit request-like object to call _get_deployed; the
    # websocket scope doesn't expose request.state directly in all
    # FastAPI versions, so we read user_id + manager off the app state.
    try:
        manager = websocket.app.state.app_manager
    except AttributeError:
        await websocket.close(code=1011, reason="daemon not ready")
        return

    # Per-user scoping: the websocket upgrade carries the bearer token
    # in query params (browsers cannot set Authorization on WS upgrades
    # — the standard workaround is ``?token=``). Fall back to
    # anonymous/local in dev mode.
    user_id = "local"
    try:
        token = websocket.query_params.get("token") if hasattr(websocket, "query_params") else None
        auth = getattr(websocket.app.state, "auth_service", None)
        if token and auth is not None:
            payload = auth.verify_access_token(token)
            user_id = payload.user_id
    except Exception as exc:
        logger.debug("preview_ws_auth_skipped: %s", exc)

    deployed = manager.get(app_id, owner_user_id=user_id) if hasattr(manager, "get") else None
    if deployed is None:
        await websocket.close(code=1008, reason="app not deployed")
        return
    pm = getattr(deployed, "preview_manager", None)
    if pm is None or not pm.enabled:
        await websocket.close(code=1008, reason="no preview dev server")
        return

    upstream_url = f"ws://127.0.0.1:{pm.port}/{path}"
    if hasattr(websocket, "query_params"):
        query_pairs = [
            f"{k}={v}" for k, v in websocket.query_params.items() if k != "token"
        ]
        if query_pairs:
            upstream_url = f"{upstream_url}?{'&'.join(query_pairs)}"

    await websocket.accept()

    try:
        async with websockets.connect(
            upstream_url,
            subprotocols=list(websocket.scope.get("subprotocols", []) or []) or None,
            open_timeout=5.0,
            ping_interval=None,
        ) as upstream:
            async def _client_to_upstream():
                try:
                    while True:
                        msg = await websocket.receive()
                        if msg["type"] == "websocket.disconnect":
                            return
                        if "text" in msg and msg["text"] is not None:
                            await upstream.send(msg["text"])
                        elif "bytes" in msg and msg["bytes"] is not None:
                            await upstream.send(msg["bytes"])
                except Exception:
                    return

            async def _upstream_to_client():
                try:
                    async for frame in upstream:
                        if isinstance(frame, bytes):
                            await websocket.send_bytes(frame)
                        else:
                            await websocket.send_text(frame)
                except Exception:
                    return

            done, pending = await asyncio.wait(
                [
                    asyncio.create_task(_client_to_upstream()),
                    asyncio.create_task(_upstream_to_client()),
                ],
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in pending:
                task.cancel()
    except Exception as exc:
        logger.debug("preview_ws_bridge_error app=%s: %s", app_id, exc)
    finally:
        try:
            await websocket.close()
        except Exception:
            pass


@router.get("/{app_id}/preview-server/logs")
async def preview_server_logs(request: Request, app_id: str, limit: int = 200):
    """Return the last ``limit`` log lines captured from the dev server."""
    _validate_id(app_id)
    deployed = _get_deployed(request, app_id)
    if not deployed:
        _raise_not_deployed(request, app_id)
    pm = getattr(deployed, "preview_manager", None)
    if pm is None:
        raise HTTPException(
            status_code=404,
            detail=f"App '{app_id}' has no preview dev server",
        )
    return {"data": {"lines": pm.get_logs(limit=limit)}}


@router.get("/{app_id}/preview-server/status")
async def preview_server_status(request: Request, app_id: str):
    """Return the current state of an app's preview dev server.

    Response shape matches :meth:`PreviewManager.status().as_dict()` with
    the tailing log buffer. Flutter polls (or opens an SSE stream) to
    reflect the status in the admin panel.

    When the app ships a pre-built ``web/dist/`` (production mode) the
    endpoint reports ``enabled: true`` + ``state: "running"`` even if
    no Vite dev server is alive — the proxy will serve the static
    bundle directly. This makes the Flutter client display the iframe
    instead of "no preview block declared".
    """
    _validate_id(app_id)
    deployed = _get_deployed(request, app_id)
    if not deployed:
        _raise_not_deployed(request, app_id)
    pm = getattr(deployed, "preview_manager", None)
    if pm is not None:
        return {"data": {"enabled": True, "mode": "dev_server", **pm.status().as_dict()}}

    has_static = await _has_static_dist(request, app_id)
    if has_static:
        return {
            "data": {
                "enabled": True,
                "mode": "static",
                "state": "running",
                "pid": None,
                "port": None,
                "version": None,
                "started_at": None,
                "last_exit_code": None,
                "restart_count": 0,
                "last_error": None,
                "logs_tail": [],
            }
        }
    return {"data": {"enabled": False, "state": "disabled"}}


@router.post("/{app_id}/preview-server/restart")
async def preview_server_restart(request: Request, app_id: str):
    """Restart the dev server (stop + start). Resets the crash budget."""
    _validate_id(app_id)
    deployed = _get_deployed(request, app_id)
    if not deployed:
        _raise_not_deployed(request, app_id)
    pm = getattr(deployed, "preview_manager", None)
    if pm is None:
        raise HTTPException(
            status_code=404,
            detail=f"App '{app_id}' has no preview dev server",
        )
    try:
        await pm.restart()
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Restart failed: {exc}",
        )
    return {"data": pm.status().as_dict()}


@router.get("/{app_id}/preview/{buffer_key:path}")
async def preview_buffer(request: Request, app_id: str, buffer_key: str):
    """Serve the app's static preview (web/dist/).

    When ``buffer_key`` is empty (e.g. ``/preview/?session_id=...``), serves
    the static dist bundle so the Flutter client can load the app's web UI
    in an iframe at ``/api/apps/{app_id}/preview/``.

    Query params:
        session_id: Forwarded to the static dist's index.html.
    """
    from fastapi.responses import Response

    _validate_id(app_id)
    manager = _get_manager(request)
    deployed = _get_deployed(request, app_id)
    if not deployed:
        _raise_not_deployed(request, app_id)

    # Try serving static dist first (same as preview-server/proxy).
    # This lets Flutter load /api/apps/{app_id}/preview/?session_id=xxx
    # and also serves sub-paths like /preview/assets/index-xxx.js.
    static = await _try_serve_static_dist(request, app_id, buffer_key or "")
    if static is not None:
        return static

    raise HTTPException(status_code=404, detail="No static preview available for this app")

