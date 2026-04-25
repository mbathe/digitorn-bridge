"""Drop-in replacement for ``digitorn.core.api.apps``.

Composes a single ``router`` from every sub-module's APIRouter so the
daemon can swap ``from digitorn.core.api.apps import router`` for
``from digitorn.core.api.apps_v2 import router`` without further changes.

The legacy ``apps.py`` remains untouched — readers can keep using it as
a fallback while we validate the split.
"""

from __future__ import annotations

from fastapi import APIRouter

from . import (
    approvals,
    background,
    diag,
    lifecycle,
    lsp,
    messages,
    oauth_mcp,
    preview,
    quota,
    secrets,
    sessions,
    tools,
    triggers,
    watchers,
    widgets,
    workspace,
)

# Re-export shared helpers + models for callers that used to do
# ``from digitorn.core.api.apps import AppResponse, _classify_error, ...``
from ._shared import (  # noqa: F401
    AppResponse,
    AppSummary,
    ApprovalResolveRequest,
    BackgroundSessionCreateRequest,
    BackgroundTaskActionRequest,
    BackgroundTaskRequest,
    ChatRequest,
    CommitRequest,
    CreateSessionRequest,
    DeployRequest,
    DisableRequest,
    FileActionRequest,
    HunksActionRequest,
    InjectOAuthTokenRequest,
    InteractRequest,
    LspCancelRequest,
    LspRpcRequest,
    NotificationCheckRequest,
    OAuthCallbackParams,
    PayloadSetRequest,
    PipelineRequest,
    RunRequest,
    SecretSetRequest,
    SecretsBulkSetRequest,
    SessionMessageRequest,
    ToolExecuteRequest,
    ValidateRequest,
    WatcherCreateRequest,
    WidgetActionRequest,
    WorkspaceForkRequest,
    WorkspaceImportRequest,
    WritebackRequest,
    _activate_preview_session,
    _active_turn_tasks,
    _assert_session_visible,
    _build_history_turns,
    _caller_user_id,
    _classify_error,
    _context_advice,
    _drain_queue_next,
    _execute_widget_tool,
    _get_activation_store,
    _get_bg_session_store,
    _get_deployed,
    _get_manager,
    _get_quota_store,
    _get_rate_limiter,
    _get_workspace_status,
    _has_static_dist,
    _inc_agent_turns,
    _is_deployed,
    _merge_resources,
    _mime_matches,
    _proxy_preview_http,
    _raise_not_deployed,
    _refresh_deployed_agent_tools,
    _require_admin_for_quota,
    _require_permission,
    _require_session_access,
    _require_session_create_or_owner,
    _resolve_app_bundle_dir,
    _resolve_deployed_preview,
    _serialise_widget_node,
    _serialise_widgets,
    _strip_content_from_files,
    _try_resize_image,
    _try_serve_static_dist,
    _turn_event,
    _turn_semaphore,
    _usage_snapshot,
    _validate_app_id,
    _validate_id,
    _validate_payload_against_schema,
    _walk_yaml_for_secrets,
)

router = APIRouter(prefix="/api/apps", tags=["apps"])

# ``list_apps`` registers at the master prefix with no extra path
# segment. Sub-routers can't carry an empty path (FastAPI rejects
# ``prefix='' + path=''``), so we mount it directly on the master.
router.add_api_route(
    "",
    lifecycle.list_apps,
    methods=["GET"],
    response_model=AppResponse,
    tags=["apps"],
)

# Order is intentional only insofar as a few modules expose more
# specific paths than others (e.g. ``/sessions/search`` before
# ``/sessions/{session_id}``); FastAPI matches in registration order.
router.include_router(lifecycle.router)
router.include_router(sessions.router)
router.include_router(messages.router)
router.include_router(workspace.router)
router.include_router(widgets.router)
router.include_router(triggers.router)
router.include_router(watchers.router)
router.include_router(background.router)
router.include_router(secrets.router)
router.include_router(quota.router)
router.include_router(oauth_mcp.router)
router.include_router(diag.router)
router.include_router(tools.router)
router.include_router(preview.router)
router.include_router(approvals.router)
router.include_router(lsp.router)

__all__ = [
    "router",
    # Pydantic models
    "AppResponse",
    "AppSummary",
    "ApprovalResolveRequest",
    "BackgroundSessionCreateRequest",
    "BackgroundTaskActionRequest",
    "BackgroundTaskRequest",
    "ChatRequest",
    "CommitRequest",
    "CreateSessionRequest",
    "DeployRequest",
    "DisableRequest",
    "FileActionRequest",
    "HunksActionRequest",
    "InjectOAuthTokenRequest",
    "InteractRequest",
    "LspCancelRequest",
    "LspRpcRequest",
    "NotificationCheckRequest",
    "OAuthCallbackParams",
    "PayloadSetRequest",
    "PipelineRequest",
    "RunRequest",
    "SecretSetRequest",
    "SecretsBulkSetRequest",
    "SessionMessageRequest",
    "ToolExecuteRequest",
    "ValidateRequest",
    "WatcherCreateRequest",
    "WidgetActionRequest",
    "WorkspaceForkRequest",
    "WorkspaceImportRequest",
    "WritebackRequest",
    # Helpers
    "_classify_error",
    "_drain_queue_next",
    "_active_turn_tasks",
    "_turn_semaphore",
    "_get_manager",
    "_get_deployed",
    "_is_deployed",
    "_validate_id",
]
