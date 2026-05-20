"""Routes for the watchers group, extracted from the legacy `apps.py`."""

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


@router.get("/{app_id}/watchers", response_model=AppResponse)
async def list_watchers(request: Request, app_id: str) -> AppResponse:
    """List all watchers (running + paused)."""
    _validate_id(app_id)
    manager = _get_manager(request)
    if not _is_deployed(request, app_id):
        _raise_not_deployed(request, app_id)

    deployed = _get_deployed(request, app_id)
    cb = deployed.context_builder if deployed else None
    if cb is None:
        return AppResponse(success=True, data={"watchers": []})

    from digitorn.modules.context_builder.params import WatchListParams
    result = await cb.watch_list(WatchListParams())
    return AppResponse(success=True, data=result.data if result.success else {"watchers": []})


@router.get("/{app_id}/watchers/{watcher_id}", response_model=AppResponse)
async def get_watcher(request: Request, app_id: str, watcher_id: str) -> AppResponse:
    """Get watcher status, metrics, and recent history."""
    _validate_id(app_id)
    manager = _get_manager(request)
    if not _is_deployed(request, app_id):
        _raise_not_deployed(request, app_id)

    deployed = _get_deployed(request, app_id)
    cb = deployed.context_builder if deployed else None
    if cb is None:
        raise HTTPException(status_code=404, detail="No context_builder")

    from digitorn.modules.context_builder.params import WatcherIdParams
    result = await cb.watch_status(WatcherIdParams(watcher_id=watcher_id))
    if not result.success:
        raise HTTPException(status_code=404, detail=result.error)
    return AppResponse(success=True, data=result.data)


@router.post("/{app_id}/watchers", response_model=AppResponse)
async def create_watcher(
    request: Request, app_id: str, body: WatcherCreateRequest,
) -> AppResponse:
    """Create a persistent watcher that periodically executes a tool."""
    _validate_id(app_id)
    manager = _get_manager(request)
    if not _is_deployed(request, app_id):
        _raise_not_deployed(request, app_id)

    deployed = _get_deployed(request, app_id)
    cb = deployed.context_builder if deployed else None
    if cb is None:
        raise HTTPException(status_code=400, detail="App has no context_builder")

    from digitorn.modules.context_builder.params import WatchStartParams
    result = await cb.watch_start(WatchStartParams(
        name=body.tool,
        params=body.params,
        interval=body.interval,
        label=body.label,
        notify_when=body.notify_when,
        notify_config=body.notify_config,
    ))
    if not result.success:
        return AppResponse(success=False, error=result.error)
    return AppResponse(success=True, data=result.data)


@router.post("/{app_id}/watchers/{watcher_id}/pause", response_model=AppResponse)
async def pause_watcher(request: Request, app_id: str, watcher_id: str) -> AppResponse:
    """Pause a running watcher (keeps history, skips checks)."""
    _validate_id(app_id)
    manager = _get_manager(request)
    if not _is_deployed(request, app_id):
        _raise_not_deployed(request, app_id)

    deployed = _get_deployed(request, app_id)
    cb = deployed.context_builder if deployed else None
    if cb is None:
        raise HTTPException(status_code=404, detail="No context_builder")

    from digitorn.modules.context_builder.params import WatcherIdParams
    result = await cb.watch_pause(WatcherIdParams(watcher_id=watcher_id))
    if not result.success:
        if "not found" in (result.error or "").lower():
            raise HTTPException(status_code=404, detail=result.error)
        return AppResponse(success=False, error=result.error)
    return AppResponse(success=True, data=result.data)


@router.post("/{app_id}/watchers/{watcher_id}/resume", response_model=AppResponse)
async def resume_watcher(request: Request, app_id: str, watcher_id: str) -> AppResponse:
    """Resume a paused watcher."""
    _validate_id(app_id)
    manager = _get_manager(request)
    if not _is_deployed(request, app_id):
        _raise_not_deployed(request, app_id)

    deployed = _get_deployed(request, app_id)
    cb = deployed.context_builder if deployed else None
    if cb is None:
        raise HTTPException(status_code=404, detail="No context_builder")

    from digitorn.modules.context_builder.params import WatcherIdParams
    result = await cb.watch_resume(WatcherIdParams(watcher_id=watcher_id))
    if not result.success:
        if "not found" in (result.error or "").lower():
            raise HTTPException(status_code=404, detail=result.error)
        return AppResponse(success=False, error=result.error)
    return AppResponse(success=True, data=result.data)


@router.delete("/{app_id}/watchers/{watcher_id}", response_model=AppResponse)
async def stop_watcher(request: Request, app_id: str, watcher_id: str) -> AppResponse:
    """Stop and remove a watcher."""
    _validate_id(app_id)
    manager = _get_manager(request)
    if not _is_deployed(request, app_id):
        _raise_not_deployed(request, app_id)

    deployed = _get_deployed(request, app_id)
    cb = deployed.context_builder if deployed else None
    if cb is None:
        raise HTTPException(status_code=404, detail="No context_builder")

    from digitorn.modules.context_builder.params import WatcherIdParams
    result = await cb.watch_stop(WatcherIdParams(watcher_id=watcher_id))
    if not result.success:
        if "not found" in (result.error or "").lower():
            raise HTTPException(status_code=404, detail=result.error)
        return AppResponse(success=False, error=result.error)
    return AppResponse(success=True, data=result.data)

