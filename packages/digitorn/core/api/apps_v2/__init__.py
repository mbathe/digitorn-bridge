"""Drop-in replacement for `digitorn.core.api.apps`."""

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
    secrets,
    sessions,
    skills,
    snippets,
    templates,
    tools,
    triggers,
    watchers,
    widgets,
    workspace,
)

# Re-export shared helpers + models for callers that used to do
# `from digitorn.core.api.apps import AppResponse, _classify_error, ...`
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
    _get_rate_limiter,
    _get_workspace_status,
    _inc_agent_turns,
    _is_deployed,
    _merge_resources,
    _mime_matches,
    _raise_not_deployed,
    _refresh_deployed_agent_tools,
    _require_permission,
    _require_session_access,
    _require_session_create_or_owner,
    _resolve_app_bundle_dir,
    _resolve_deployed_preview,
    _serialise_widget_node,
    _serialise_widgets,
    _strip_content_from_files,
    _try_resize_image,
    _turn_event,
    _turn_semaphore,
    _usage_snapshot,
    _validate_app_id,
    _validate_id,
    _validate_payload_against_schema,
    _walk_yaml_for_secrets,
)

router = APIRouter(prefix="/api/apps", tags=["apps"])

router.add_api_route(
    "",
    lifecycle.list_apps,
    methods=["GET"],
    response_model=AppResponse,
    tags=["apps"],
)

router.include_router(lifecycle.router)
router.include_router(sessions.router)
router.include_router(messages.router)
router.include_router(workspace.router)
router.include_router(widgets.router)
router.include_router(triggers.router)
router.include_router(watchers.router)
router.include_router(background.router)
router.include_router(secrets.router)
router.include_router(snippets.router)
router.include_router(skills.router)
router.include_router(templates.router)
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
