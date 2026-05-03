"""Routes for the oauth_mcp group, extracted from the legacy ``apps.py``.

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



@router.get("/{app_id}/oauth/authorize", response_model=AppResponse)
async def oauth_authorize(
    request: Request,
    app_id: str,
    server_id: str,
    session_id: str,
) -> AppResponse:
    """Start an OAuth2 authorization flow for an MCP server.

    Returns the authorization URL the user should open in their browser.
    """
    _validate_id(app_id)
    _validate_id(session_id, "session_id")
    manager = _get_manager(request)
    deployed = _get_deployed(request, app_id)
    if deployed is None:
        raise HTTPException(404, f"App not deployed: {app_id}")

    mcp_module = deployed.modules.get("mcp")
    if mcp_module is None:
        raise HTTPException(400, "App has no MCP module")

    entry = mcp_module._pool.get_server(server_id)
    if entry is None:
        raise HTTPException(404, f"MCP server not connected: {server_id}")
    if entry.auth_config is None:
        raise HTTPException(400, f"MCP server '{server_id}' has no OAuth config")

    # Identity comes from the JWT (the daemon doesn't own a users
    # table). Fall back to the session's bound user_id only if the
    # request didn't come through the auth middleware.
    user_id = getattr(request.state, "user_id", None)
    if not user_id:
        user_store = getattr(mcp_module, "_user_store", None)
        if user_store is not None:
            user_id = await user_store.get_user_id_for_session(session_id)
    if not user_id:
        raise HTTPException(404, f"No user bound to session: {session_id}")

    auth_url, state_key = mcp_module._oauth.build_authorize_url(
        entry.auth_config, server_id, user_id,
    )

    return AppResponse(
        success=True,
        data={
            "auth_url": auth_url,
            "state": state_key,
            "provider": entry.auth_config.provider,
            "server_id": server_id,
        },
    )


@router.get("/{app_id}/oauth/callback")
async def oauth_callback(
    request: Request,
    app_id: str,
    code: str,
    state: str,
) -> AppResponse:
    """OAuth2 callback endpoint - exchanges authorization code for tokens.

    This is the redirect_uri that the OAuth provider redirects to after
    the user authorizes. Exchanges the code for tokens and stores them.
    """
    _validate_id(app_id)
    manager = _get_manager(request)
    deployed = _get_deployed(request, app_id)
    if deployed is None:
        raise HTTPException(404, f"App not deployed: {app_id}")

    mcp_module = deployed.modules.get("mcp")
    if mcp_module is None:
        raise HTTPException(400, "App has no MCP module")

    oauth_state = mcp_module._oauth.get_pending_state(state)
    if oauth_state is None:
        raise HTTPException(400, "Invalid or expired OAuth state")

    entry = mcp_module._pool.get_server(oauth_state.server_id)
    if entry is None or entry.auth_config is None:
        raise HTTPException(400, "MCP server or auth config not found")

    try:
        token_data = await mcp_module._oauth.exchange_code(
            entry.auth_config, oauth_state, code,
        )
    except Exception as exc:
        logger.error("oauth_exchange_failed: %s", exc)
        raise HTTPException(400, f"Token exchange failed: {exc}")

    from digitorn.modules.mcp.oauth import parse_token_response

    access_token, refresh_token, expires_at, scope = parse_token_response(token_data)

    user_store = getattr(mcp_module, "_user_store", None)
    if user_store is None:
        raise HTTPException(500, "UserStore not available")

    await user_store.store_token(
        oauth_state.user_id,
        entry.auth_config.provider,
        access_token,
        refresh_token,
        scope=scope,
        expires_at=expires_at,
    )

    await mcp_module._inject_oauth_token(
        oauth_state.server_id, entry, entry.auth_config, access_token,
        token_type=token_data.get("token_type"),
    )

    if deployed.context_builder is not None:
        security_profile = getattr(deployed.compiled, "security_profile", None)
        new_index = deployed.context_builder.build_and_set_index(
            deployed.modules, security_profile,
        )
        _refresh_deployed_agent_tools(deployed, new_index)

    accept = request.headers.get("accept", "")
    if "text/html" in accept:
        from starlette.responses import HTMLResponse

        return HTMLResponse(
            "<html><body style='font-family:system-ui;text-align:center;padding:60px'>"
            f"<h1>&#10004; {entry.auth_config.provider.title()} autorisé !</h1>"
            "<p>Tu peux fermer cet onglet.</p></body></html>"
        )

    return AppResponse(
        success=True,
        data={
            "message": f"Authorization successful for {entry.auth_config.provider}",
            "provider": entry.auth_config.provider,
            "server_id": oauth_state.server_id,
            "user_id": oauth_state.user_id,
        },
    )


@router.post("/{app_id}/mcp/{server_id}/oauth-token", response_model=AppResponse)
async def inject_oauth_token(
    request: Request, app_id: str, server_id: str, body: InjectOAuthTokenRequest,
) -> AppResponse:
    """Inject an OAuth token into an MCP server and persist it in DB.

    Used by the CLI after completing a local OAuth flow.
    """
    _validate_id(app_id)
    manager = _get_manager(request)
    deployed = _get_deployed(request, app_id)
    if deployed is None:
        raise HTTPException(404, f"App not deployed: {app_id}")

    mcp_module = deployed.modules.get("mcp")
    if mcp_module is None:
        raise HTTPException(400, "App has no MCP module")

    entry = mcp_module._pool.get_server(server_id)
    if entry is None:
        raise HTTPException(404, f"MCP server not found: {server_id}")
    if entry.auth_config is None:
        raise HTTPException(400, f"MCP server '{server_id}' has no OAuth config")

    await mcp_module._inject_oauth_token(
        server_id, entry, entry.auth_config, body.access_token,
        token_type=body.token_type,
    )

    cb = deployed.context_builder
    if cb is not None:
        security_profile = getattr(deployed.compiled, "security_profile", None)
        new_index = cb.build_and_set_index(deployed.modules, security_profile)
        _refresh_deployed_agent_tools(deployed, new_index)
        logger.info(
            "tool_index_rebuilt after oauth_inject app=%s tools=%d",
            app_id, new_index.total_tools,
        )

    user_store = getattr(mcp_module, "_user_store", None)
    if user_store is not None:
        from datetime import datetime, timedelta, timezone

        # Token is stored against the calling user (from the JWT). If
        # the call comes outside an authenticated context (CLI tooling
        # against a dev daemon), fall back to "cli-user" so the token
        # row is still keyed to *something* the next request can find.
        token_user_id = getattr(request.state, "user_id", None) or "cli-user"
        expires_at = None
        if body.expires_in:
            expires_at = datetime.now(timezone.utc) + timedelta(seconds=body.expires_in)
        await user_store.store_token(
            token_user_id, entry.auth_config.provider, body.access_token,
            refresh_token=body.refresh_token,
            scope=body.scope or "",
            expires_at=expires_at,
        )

    return AppResponse(
        success=True,
        data={
            "server_id": server_id,
            "provider": entry.auth_config.provider,
            "status": "injected",
        },
    )


@router.delete("/{app_id}/mcp/{server_id}/oauth-token", response_model=AppResponse)
async def revoke_mcp_oauth(request: Request, app_id: str, server_id: str) -> AppResponse:
    """Revoke an MCP server's OAuth token - disconnect and delete from DB.

    The server entry is kept in the pool (with status reset) so /connect
    can re-authorize later.
    """
    _validate_id(app_id)
    manager = _get_manager(request)
    deployed = _get_deployed(request, app_id)
    if deployed is None:
        raise HTTPException(404, f"App not deployed: {app_id}")

    mcp_module = deployed.modules.get("mcp")
    if mcp_module is None:
        raise HTTPException(404, "No MCP module in this app")

    pool = getattr(mcp_module, "_pool", None)
    if pool is None:
        raise HTTPException(404, "No MCP connection pool")

    entry = pool.get_server(server_id)
    if entry is None:
        raise HTTPException(404, f"MCP server not found: {server_id}")

    if entry.auth_config is None:
        raise HTTPException(400, f"Server '{server_id}' has no OAuth config")

    provider = entry.auth_config.provider

    user_store = getattr(mcp_module, "_user_store", None)
    if user_store is not None:
        try:
            from digitorn.core.database import get_session
            from digitorn.core.models import UserOAuthToken
            from sqlalchemy import select, delete as sa_delete

            async for session in get_session():
                await session.execute(
                    sa_delete(UserOAuthToken).where(
                        UserOAuthToken.provider == provider,
                    )
                )
                await session.commit()
                break
        except Exception as exc:
            logger.warning("revoke_token_db_error provider=%s: %s", provider, exc)

    try:
        await pool.disconnect(server_id)
    except Exception as exc:
        logger.warning("mcp_disconnect_error server=%s: %s", server_id, exc)

    from digitorn.modules.mcp.connections import MCPServerEntry

    stub = MCPServerEntry(
        server_id=server_id,
        transport_type=entry.transport_type,
        transport=entry.transport,
        status="disconnected",
        auth_config=entry.auth_config,
        _connect_kwargs={
            k: v for k, v in entry._connect_kwargs.items()
            if k != "env" or not entry.auth_config.env_token_var
        },
    )
    if entry.auth_config.env_token_var and "env" in entry._connect_kwargs:
        clean_env = dict(entry._connect_kwargs.get("env", {}))
        clean_env.pop(entry.auth_config.env_token_var, None)
        stub._connect_kwargs["env"] = clean_env

    pool._servers[server_id] = stub

    cb = deployed.context_builder
    if cb is not None:
        security_profile = getattr(deployed.compiled, "security_profile", None)
        new_index = cb.build_and_set_index(deployed.modules, security_profile)
        _refresh_deployed_agent_tools(deployed, new_index)
        logger.info(
            "tool_index_rebuilt after oauth_revoke app=%s server=%s tools=%d",
            app_id, server_id, new_index.total_tools,
        )

    return AppResponse(
        success=True,
        data={
            "server_id": server_id,
            "provider": provider,
            "status": "disconnected",
        },
    )


@router.get("/{app_id}/mcp/pending-oauth", response_model=AppResponse)
async def list_pending_oauth(request: Request, app_id: str) -> AppResponse:
    """List MCP servers that need OAuth authorization (no valid token yet)."""
    _validate_id(app_id)
    manager = _get_manager(request)
    deployed = _get_deployed(request, app_id)
    if deployed is None:
        raise HTTPException(404, f"App not deployed: {app_id}")

    mcp_module = deployed.modules.get("mcp")
    if mcp_module is None:
        return AppResponse(success=True, data={"pending": []})

    pool = getattr(mcp_module, "_pool", None)
    if pool is None:
        return AppResponse(success=True, data={"pending": []})

    pending = []
    for server_id, entry in pool._servers.items():
        if entry.auth_config is None:
            continue
        has_token = False
        if entry.transport_type == "stdio" and entry.auth_config.env_token_var:
            current_env = getattr(entry, "_connect_kwargs", {}).get("env", {})
            has_token = bool(current_env.get(entry.auth_config.env_token_var))
        elif entry.status == "connected" and entry.tools:
            has_token = True

        if not has_token:
            pending.append({
                "server_id": server_id,
                "provider": entry.auth_config.provider,
                "client_id": entry.auth_config.client_id,
                "client_secret": entry.auth_config.client_secret,
                "scopes": entry.auth_config.scopes,
                "redirect_uri": entry.auth_config.redirect_uri,
                "env_token_var": entry.auth_config.env_token_var,
            })

    return AppResponse(success=True, data={"pending": pending})

