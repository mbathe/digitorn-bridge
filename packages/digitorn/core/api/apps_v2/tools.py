"""Routes for the tools group, extracted from the legacy ``apps.py``.

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



@router.get("/{app_id}/tools/search", response_model=AppResponse)
async def search_tools(
    request: Request, app_id: str,
    query: str = "", max_results: int = 10,
) -> AppResponse:
    """Semantic + keyword search over all tools available to the app.

    Returns ranked results with scores, descriptions, and parameter schemas.
    """
    _validate_id(app_id)
    manager = _get_manager(request)
    if not _is_deployed(request, app_id):
        _raise_not_deployed(request, app_id)

    deployed = _get_deployed(request, app_id)
    cb = deployed.context_builder if deployed else None
    if cb is None:
        return AppResponse(success=True, data={"results": []})

    if not query or not query.strip():
        return AppResponse(success=True, data={"tools": [], "count": 0, "query": query})

    from digitorn.modules.context_builder.params import SearchToolsParams
    result = await cb.search_tools(SearchToolsParams(
        query=query, max_results=max_results,
    ))
    return AppResponse(success=True, data=result.data if result.success else {"results": []})


@router.get("/{app_id}/tools/categories", response_model=AppResponse)
async def list_tool_categories(request: Request, app_id: str) -> AppResponse:
    """List all tool categories (modules) with tool counts."""
    _validate_id(app_id)
    manager = _get_manager(request)
    if not _is_deployed(request, app_id):
        _raise_not_deployed(request, app_id)

    deployed = _get_deployed(request, app_id)
    cb = deployed.context_builder if deployed else None
    if cb is None:
        return AppResponse(success=True, data={"categories": []})

    from digitorn.modules.context_builder.params import ListCategoriesParams
    result = await cb.list_categories(ListCategoriesParams())
    return AppResponse(success=True, data=result.data if result.success else {"categories": []})


@router.get("/{app_id}/tools/categories/{category}", response_model=AppResponse)
async def browse_tool_category(
    request: Request, app_id: str, category: str, page: int = 1,
) -> AppResponse:
    """Browse tools in a category (paginated)."""
    _validate_id(app_id)
    manager = _get_manager(request)
    if not _is_deployed(request, app_id):
        _raise_not_deployed(request, app_id)

    deployed = _get_deployed(request, app_id)
    cb = deployed.context_builder if deployed else None
    if cb is None:
        return AppResponse(success=True, data={"tools": []})

    from digitorn.modules.context_builder.params import BrowseCategoryParams
    result = await cb.browse_category(BrowseCategoryParams(
        category=category, page=page,
    ))
    return AppResponse(success=True, data=result.data if result.success else {"tools": []})


@router.get("/{app_id}/tools/{tool_name:path}", response_model=AppResponse)
async def get_tool_schema(request: Request, app_id: str, tool_name: str) -> AppResponse:
    """Get full schema for a tool by qualified name (e.g. filesystem.read).

    Returns parameters, description, examples, aliases, side effects.
    """
    _validate_id(app_id)
    manager = _get_manager(request)
    if not _is_deployed(request, app_id):
        _raise_not_deployed(request, app_id)

    deployed = _get_deployed(request, app_id)
    cb = deployed.context_builder if deployed else None
    if cb is None:
        raise HTTPException(status_code=404, detail="No context_builder")

    from digitorn.modules.context_builder.params import GetToolParams
    result = await cb.get_tool(GetToolParams(name=tool_name))
    if not result.success:
        raise HTTPException(status_code=404, detail=result.error)
    return AppResponse(success=True, data=result.data)


@router.post("/{app_id}/tools/{tool_name:path}/execute", response_model=AppResponse)
async def execute_tool(
    request: Request, app_id: str, tool_name: str, body: ToolExecuteRequest,
) -> AppResponse:
    """Execute a tool directly by qualified name.

    Bypasses the agent - runs the tool and returns the raw result.
    Security policies (grant/approve/deny) still apply.
    """
    _validate_id(app_id)
    manager = _get_manager(request)
    if not _is_deployed(request, app_id):
        _raise_not_deployed(request, app_id)

    deployed = _get_deployed(request, app_id)
    cb = deployed.context_builder if deployed else None
    if cb is None:
        raise HTTPException(status_code=404, detail="No context_builder")

    # Activate the preview/workspace session so mutations (set_resource,
    # set_state) bind to the right session and schedule their debounced
    # persist. Without this, tool-execute calls bypass the session wiring
    # the agent loop normally sets up and writes land on a "_default_"
    # session that never gets flushed to the right row.
    sid = body.session_id
    if sid:
        _uid = _caller_user_id(request) or ""
        preview_module = deployed.modules.get("preview") if hasattr(deployed, "modules") else None
        # ``set_active=True`` - this endpoint is about to run a mutating
        # tool; the write path reads ``preview._active_session_id`` to
        # decide which session's state to update.
        await _activate_preview_session(
            request, app_id, sid, preview_module,
            user_id=_uid, set_active=True,
        )

    from digitorn.modules.context_builder.params import ExecuteToolParams
    result = await cb.execute_tool(ExecuteToolParams(
        name=tool_name, params=body.params,
    ))
    if not result.success:
        return AppResponse(success=False, error=result.error, data=result.data)
    return AppResponse(success=True, data=result.data)

