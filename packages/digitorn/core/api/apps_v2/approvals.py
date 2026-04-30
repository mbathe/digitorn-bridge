"""Routes for the approvals group, extracted from the legacy ``apps.py``.

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



@router.get("/{app_id}/approvals", response_model=AppResponse)
async def list_approvals(request: Request, app_id: str) -> AppResponse:
    """List pending approval requests for an app."""
    _validate_id(app_id)
    manager = _get_manager(request)
    if not _is_deployed(request, app_id):
        _raise_not_deployed(request, app_id)

    user_id = getattr(request.state, "user_id", None) or "local"
    deployed = _get_deployed(request, app_id)
    aq = getattr(deployed, "approval_queue", None) if deployed else None
    if aq is None:
        return AppResponse(success=True, data={"pending": []})
    return AppResponse(success=True, data={"pending": aq.list_pending_for_user(user_id)})


@router.post("/{app_id}/approve", response_model=AppResponse)
async def resolve_approval(
    request: Request, app_id: str, body: ApprovalResolveRequest,
) -> AppResponse:
    """Approve or deny a pending approval request."""
    _validate_id(app_id)
    manager = _get_manager(request)
    if not _is_deployed(request, app_id):
        _raise_not_deployed(request, app_id)

    user_id = getattr(request.state, "user_id", None) or "local"
    deployed = _get_deployed(request, app_id)
    aq = getattr(deployed, "approval_queue", None) if deployed else None
    if aq is None:
        return AppResponse(success=False, error="No approval queue for this app")

    payload = body.resolved_payload()
    logger.info(
        "approve_request: id=%s approved=%s payload_len=%d payload_preview=%r",
        body.request_id, body.approved, len(payload), payload[:120],
    )
    resolved = aq.resolve(
        body.request_id, body.approved, message=payload, user_id=user_id,
    )
    if not resolved:
        return AppResponse(
            success=False,
            error="Request not found, already resolved, or not authorized",
        )
    return AppResponse(
        success=True,
        data={
            "request_id": body.request_id,
            "approved": body.approved,
            "payload_received": bool(payload),
        },
    )

