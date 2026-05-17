"""Routes for the lsp group, extracted from the legacy ``apps.py``.

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



@router.post(
    "/{app_id}/sessions/{session_id}/lsp/request",
    response_model=AppResponse,
)
async def lsp_rpc_request(
    request: Request, app_id: str, session_id: str, body: LspRpcRequest,
) -> AppResponse:
    """Forward an LSP RPC to the language server backing the given file.

    This is the sole entry point clients (Monaco, ``useLspRequest`` Flutter
    hook, custom tooling) use for hover / goto / references / completion /
    rename / signature help. The daemon doesn't reshape payloads - LSP
    spec semantics are the contract.

    Returns::

        {"success": true, "data": {"server": "pyright", "method": "...",
                                    "result": <lsp response>}}

    Error responses map cleanly to HTTP semantics:

    - 404 - app not deployed or has no LSP module
    - 400 - file extension has no registered server, or server not
             installed, or method unsupported (protocol=compiler/linter)
    - 504 - server responded with None (timeout)
    """
    _validate_id(app_id)
    _validate_id(session_id, "session_id")
    deployed = _get_deployed(request, app_id)
    if not deployed:
        _raise_not_deployed(request, app_id)
    lsp_module = deployed.modules.get("lsp") if hasattr(deployed, "modules") else None
    if lsp_module is None:
        raise HTTPException(status_code=404, detail="App has no LSP module")

    _uid = getattr(request.state, "user_id", None) or "local"
    sess = await _get_manager(request).get_session(app_id, session_id, user_id=_uid)
    if sess is None:
        raise HTTPException(status_code=404, detail="Session not found")

    # Resolve the request path to an absolute on-disk path. The LSP
    # module's ``request`` action passes ``path`` straight to the
    # protocol, which builds ``file://`` URIs and tries to read the
    # file from disk for didOpen. A workspace-relative path (the
    # canonical shape clients send) would be resolved against the
    # worker process's cwd -- not the session workspace -- and the
    # didOpen would silently skip, returning empty hover results.
    # Resolving here mirrors what ``workspace._run_lint`` does for
    # the lint pipeline; we activate the preview session first so
    # the workspace module's ``_resolve_sync_dir`` picks the right
    # per-session dir (otherwise it leaks the previously-active
    # session's workspace).
    resolved_path = body.path
    try:
        ws_module = deployed.modules.get("workspace") if hasattr(
            deployed, "modules",
        ) else None
        if ws_module is not None and hasattr(ws_module, "_resolve_disk_dir_for"):
            preview_module = deployed.modules.get("preview") if hasattr(
                deployed, "modules",
            ) else None
            if preview_module is not None:
                await _activate_preview_session(
                    request, app_id, session_id, preview_module,
                    user_id=_uid, set_active=True,
                )
            rel = ws_module._resolve_ws_path(body.path) if hasattr(
                ws_module, "_resolve_ws_path",
            ) else body.path
            disk_dir = ws_module._resolve_disk_dir_for(rel)
            if disk_dir and hasattr(ws_module, "_join_inside"):
                full = ws_module._join_inside(disk_dir, rel)
                if full:
                    resolved_path = full
    except Exception as exc:
        logger.debug(
            "lsp_rpc_path_resolve_failed app=%s path=%s err=%s",
            app_id, body.path, exc,
        )

    from digitorn.modules.lsp.params import LspRequestParams
    lsp_params = LspRequestParams(
        path=resolved_path,
        method=body.method,
        params=body.params,
        timeout_seconds=body.timeout_seconds,
        request_id=body.request_id,
        session_id=session_id,
        supersede_previous=body.supersede_previous,
    )

    # Stamp an ExecutionContext with the URL-derived ``app_id`` so the
    # daemon-side proxy (when LSP is workered) ships the right tenant
    # in the call envelope. Without this, the proxy reads an empty
    # ``_context_var`` (REST endpoints don't go through
    # ``module.execute()``) and the worker resolves the call against
    # whichever app last called ``on_config_update`` -- which means
    # one app's pyright/ruff bleeds into another's hover request.
    from digitorn.modules.base import (
        BaseModule as _BaseModule,
        ExecutionContext as _ExecutionContext,
    )
    _ctx_token = _BaseModule._context_var.set(
        _ExecutionContext(
            plan_id=f"lsp_rpc:{app_id}",
            action_id="lsp.request",
            app_id=app_id,
            session_id=session_id,
            user_id=_uid,
            workspace=getattr(sess, "workspace", None) if sess else None,
        ),
    )

    # Fire LSP request as a monitored task so we can react to HTTP
    # client disconnects (Phase 3: real server-side abort when the
    # client fetch is aborted). We poll `request.is_disconnected()`
    # in parallel and cancel the underlying LSP task if the fetch dies.
    lsp_task = asyncio.create_task(lsp_module.request(lsp_params))

    async def _watch_disconnect() -> None:
        while not lsp_task.done():
            try:
                if await request.is_disconnected():
                    logger.debug(
                        "lsp_rpc: client disconnected, cancelling LSP task",
                    )
                    lsp_task.cancel()
                    return
            except Exception:
                return
            await asyncio.sleep(0.1)

    watch_task = asyncio.create_task(_watch_disconnect())
    try:
        result = await lsp_task
    except asyncio.CancelledError:
        return AppResponse(
            success=False, error="request cancelled",
            data={"cancelled": True, "request_id": body.request_id},
        )
    finally:
        if not watch_task.done():
            watch_task.cancel()
        try:
            _BaseModule._context_var.reset(_ctx_token)
        except (ValueError, LookupError):
            # Token from a different task / context -- safe to ignore;
            # the context will fall out of scope with the request.
            pass

    if not result.success:
        err = result.error or "LSP request failed"
        # Cancellation comes back with `cancelled: true` in data.
        if (result.data or {}).get("cancelled"):
            return AppResponse(
                success=False, error=err, data=result.data,
            )
        if "timeout" in err.lower() or "no result" in err.lower():
            return AppResponse(
                success=False, error=err,
                data=result.data or {"timeout": True},
            )
        raise HTTPException(status_code=400, detail=err)
    return AppResponse(success=True, data=result.data)


@router.post(
    "/{app_id}/sessions/{session_id}/lsp/cancel",
    response_model=AppResponse,
)
async def lsp_rpc_cancel(
    request: Request, app_id: str, session_id: str, body: LspCancelRequest,
) -> AppResponse:
    """Cancel an in-flight LSP request by ``request_id``.

    Safe to call on already-completed requests (returns ``cancelled:
    false, already_done: true``). Returns ``request not found`` if the
    id never existed or was already cleaned up.

    Typical UX: Monaco fires an abort token on a completion request,
    the client catches it and hits this endpoint with the correlation
    id it sent in the original /lsp/request call.
    """
    _validate_id(app_id)
    _validate_id(session_id, "session_id")
    deployed = _get_deployed(request, app_id)
    if not deployed:
        _raise_not_deployed(request, app_id)
    lsp_module = deployed.modules.get("lsp") if hasattr(deployed, "modules") else None
    if lsp_module is None:
        raise HTTPException(status_code=404, detail="App has no LSP module")

    from digitorn.modules.lsp.params import LspCancelParams
    result = await lsp_module.cancel_request(LspCancelParams(
        request_id=body.request_id,
        session_id=session_id,
    ))
    if not result.success:
        return AppResponse(success=False, error=result.error, data=result.data)
    return AppResponse(success=True, data=result.data)

