"""Routes for the quota group, extracted from the legacy ``apps.py``.

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



@router.get("/{app_id}/quota", response_model=AppResponse)
async def get_app_quota(request: Request, app_id: str) -> AppResponse:
    """Return the app-level quota definition + effective limits + usage.

    Admin-only. The response carries three blocks:
        - ``quota``     : what the admin has explicitly set (null if
                          never set - falls back to global defaults).
        - ``effective`` : the merged result after inheritance (what is
                          actually enforced).
        - ``usage``     : current rolling counters (request RPM today;
                          token/cost counters are placeholders until
                          provider-side hooks land).
    """
    _require_admin_for_quota(request)
    _validate_id(app_id)
    limiter = _get_rate_limiter(request)
    store = _get_quota_store(request)

    logger.warning(
        "DEBUG get_app_quota: store type=%s module=%s has_get_app_quota=%s",
        type(store).__name__, type(store).__module__,
        hasattr(store, "get_app_quota"),
    )
    envelope = store.get_app_quota(app_id) or {}
    quota_def = envelope.get("quota")

    # Merge with global default for the effective view.
    from digitorn.core.config import get_settings
    settings = get_settings()
    global_rpm = int(getattr(settings.server, "rate_limit_rpm", 60))
    effective = store.effective_quota(app_id, global_default_rpm=global_rpm)

    return AppResponse(success=True, data={
        "app_id": app_id,
        "scope": "app",
        "quota": quota_def,                # null if never set
        "effective": effective,             # always populated
        "usage": store.snapshot_usage(app_id, global_default_rpm=global_rpm),
        "updated_at": envelope.get("updated_at"),
        "updated_by": envelope.get("updated_by"),
    })


@router.put("/{app_id}/quota", response_model=AppResponse)
async def set_app_quota(
    request: Request, app_id: str, body: "QuotaPutRequest",
) -> AppResponse:
    """Set or replace the app-level quota. Admin-only.

    Accepts both the legacy ``{"rpm": N}`` shape (backward-compat) and
    the rich ``{"quota": {...}}`` shape documented in ``core/quota.py``.
    The rich shape supports per-model overrides, tokens, cost, concurrent
    sessions, and messages-per-session - all optional.
    """
    _require_admin_for_quota(request)
    _validate_id(app_id)
    store = _get_quota_store(request)
    limiter = _get_rate_limiter(request)

    # Snapshot BEFORE state for the audit trail.
    before_envelope = store.get_app_quota(app_id) or {}
    envelope = store.set_app_quota(
        app_id,
        body.quota,
        updated_by=_caller_user_id(request),
    )

    # Mirror the RPM into the legacy rate_limiter slot so the HTTP
    # middleware throttle picks up the change without any extra wiring.
    if body.quota.requests and body.quota.requests.per_minute is not None:
        limiter.set_quota(app_id, body.quota.requests.per_minute)

    # BANK-GRADE audit: record who changed what, when, from what to what.
    from digitorn.core.audit import audit_log
    await audit_log(
        request, event_type="quota.set_app", target_app_id=app_id,
        before={"quota": before_envelope.get("quota")},
        after={"quota": envelope["quota"]},
        message=f"admin set app-level quota on {app_id}",
    )

    return AppResponse(success=True, data={
        "app_id": app_id,
        "scope": "app",
        "quota": envelope["quota"],
        "updated_at": envelope["updated_at"],
        "updated_by": envelope["updated_by"],
    })


@router.delete("/{app_id}/quota", response_model=AppResponse)
async def remove_app_quota(request: Request, app_id: str) -> AppResponse:
    """Clear the app-level quota. Falls back to global defaults. Admin-only."""
    _require_admin_for_quota(request)
    _validate_id(app_id)
    store = _get_quota_store(request)
    limiter = _get_rate_limiter(request)

    before_envelope = store.get_app_quota(app_id) or {}
    had = store.remove_app_quota(app_id)
    limiter.remove_quota(app_id)

    from digitorn.core.audit import audit_log
    await audit_log(
        request, event_type="quota.delete_app", target_app_id=app_id,
        before={"quota": before_envelope.get("quota")},
        after={"quota": None},
        success=had,
        message=f"admin cleared app-level quota on {app_id}",
    )

    return AppResponse(success=had, data={
        "app_id": app_id,
        "scope": "app",
        "removed": had,
    })


@router.get("/{app_id}/quota/user/{user_id}", response_model=AppResponse)
async def get_user_quota(
    request: Request, app_id: str, user_id: str,
) -> AppResponse:
    """Return the user-level quota definition + effective limits + usage.

    Admin-only. ``effective`` is the merge of global → app → user.
    """
    _require_admin_for_quota(request)
    _validate_id(app_id)
    limiter = _get_rate_limiter(request)
    store = _get_quota_store(request)

    envelope = store.get_user_quota(app_id, user_id) or {}
    quota_def = envelope.get("quota")

    from digitorn.core.config import get_settings
    settings = get_settings()
    global_rpm = int(getattr(settings.server, "rate_limit_rpm", 60))
    effective = store.effective_quota(
        app_id, user_id=user_id, global_default_rpm=global_rpm,
    )

    return AppResponse(success=True, data={
        "app_id": app_id,
        "user_id": user_id,
        "scope": "user",
        "quota": quota_def,
        "effective": effective,
        "usage": store.snapshot_usage(
            app_id, user_id=user_id, global_default_rpm=global_rpm,
        ),
        "updated_at": envelope.get("updated_at"),
        "updated_by": envelope.get("updated_by"),
    })


@router.put("/{app_id}/quota/user/{user_id}", response_model=AppResponse)
async def set_user_quota(
    request: Request, app_id: str, user_id: str, body: "QuotaPutRequest",
) -> AppResponse:
    """Set or replace the per-user quota override. Admin-only.

    The user override takes precedence over the app-level definition for
    every field it sets; unset fields inherit from the app-level default,
    which itself inherits from the global default.
    """
    _require_admin_for_quota(request)
    _validate_id(app_id)
    store = _get_quota_store(request)
    limiter = _get_rate_limiter(request)

    before_envelope = store.get_user_quota(app_id, user_id) or {}
    envelope = store.set_user_quota(
        app_id, user_id, body.quota,
        updated_by=_caller_user_id(request),
    )

    # Legacy mirror so the HTTP middleware throttle picks it up too.
    if body.quota.requests and body.quota.requests.per_minute is not None:
        limiter.set_user_quota(app_id, user_id, body.quota.requests.per_minute)

    from digitorn.core.audit import audit_log
    await audit_log(
        request, event_type="quota.set_user",
        target_app_id=app_id, target_user_id=user_id,
        before={"quota": before_envelope.get("quota")},
        after={"quota": envelope["quota"]},
        message=f"admin set per-user quota override for {user_id} on {app_id}",
    )

    return AppResponse(success=True, data={
        "app_id": app_id,
        "user_id": user_id,
        "scope": "user",
        "quota": envelope["quota"],
        "updated_at": envelope["updated_at"],
        "updated_by": envelope["updated_by"],
    })


@router.delete("/{app_id}/quota/user/{user_id}", response_model=AppResponse)
async def remove_user_quota(
    request: Request, app_id: str, user_id: str,
) -> AppResponse:
    """Clear the per-user quota override. User falls back to the app
    default (or the global default if no app default is set). Admin-only."""
    _require_admin_for_quota(request)
    _validate_id(app_id)
    store = _get_quota_store(request)
    limiter = _get_rate_limiter(request)

    before_envelope = store.get_user_quota(app_id, user_id) or {}
    had = store.remove_user_quota(app_id, user_id)
    limiter.remove_user_quota(app_id, user_id)

    from digitorn.core.audit import audit_log
    await audit_log(
        request, event_type="quota.delete_user",
        target_app_id=app_id, target_user_id=user_id,
        before={"quota": before_envelope.get("quota")},
        after={"quota": None},
        success=had,
        message=f"admin cleared per-user quota override for {user_id} on {app_id}",
    )

    return AppResponse(success=had, data={
        "app_id": app_id,
        "user_id": user_id,
        "scope": "user",
        "removed": had,
    })


@router.get("/{app_id}/quota/me", response_model=AppResponse)
async def get_own_quota(request: Request, app_id: str) -> AppResponse:
    """Self-service: return the caller's own quota + usage on this app.

    **Not admin-gated** - any authenticated caller can read their own
    effective limits and consumption so the client can render a
    "X used / Y allowed" bar in Settings without admin privileges.
    Writes still require admin (see ``PUT /quota/user/{user_id}``).
    """
    _validate_id(app_id)
    limiter = _get_rate_limiter(request)
    store = _get_quota_store(request)
    user_id = _caller_user_id(request)

    envelope = store.get_user_quota(app_id, user_id) or {}
    quota_def = envelope.get("quota")

    from digitorn.core.config import get_settings
    settings = get_settings()
    global_rpm = int(getattr(settings.server, "rate_limit_rpm", 60))
    effective = store.effective_quota(
        app_id, user_id=user_id, global_default_rpm=global_rpm,
    )

    return AppResponse(success=True, data={
        "app_id": app_id,
        "user_id": user_id,
        "scope": "user",
        "quota": quota_def,           # null if no personal override
        "effective": effective,        # merged global → app → user
        "usage": store.snapshot_usage(
            app_id, user_id=user_id, global_default_rpm=global_rpm,
        ),
        "updated_at": envelope.get("updated_at"),
        "updated_by": envelope.get("updated_by"),
    })

