"""Routes for the secrets group, extracted from the legacy ``apps.py``.

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



@router.get("/{app_id}/required-secrets", response_model=AppResponse)
async def required_secrets(request: Request, app_id: str) -> AppResponse:
    """List the secrets the app's YAML REQUIRES and their current status.

    Returns everything the UI needs to render a "manage credentials"
    screen for an app with multiple providers:

    - ``key``       : the secret name (e.g. ``ANTHROPIC_API_KEY``)
    - ``used_by``   : list of dotted YAML paths where the secret is
                      referenced - so the UI can group by agent/module
                      or at least show "this key is used in 2 places"
    - ``is_set``    : ``true`` if the secret is already defined in
                      ``SecretStore`` or matches an env var on the daemon
    - ``reference_type``: ``"env"`` or ``"secret"`` depending on the
                      template style (``{{env.X}}`` vs ``{{secret.X}}``)

    The response also includes:

    - ``missing_count`` : how many required secrets have no value yet
    - ``unused_keys``   : secrets stored in SecretStore that are NOT
                         referenced by the current YAML (orphans from
                         an old version of the app - the UI can offer
                         to clean them up)

    This route reads from the app's **current bundle** on disk - so the
    list reflects the deployed version, not the live memory state (they
    should match, but if someone fiddled with secrets manually the
    ``is_set`` column will reveal the drift).
    """
    _validate_id(app_id)
    _require_permission(request, "apps:read")
    manager = _get_manager(request)

    # Resolve the raw YAML the app was deployed with. Prefer the bundle
    # (authoritative) and fall back to Application.yaml_content for
    # legacy rows that predate the bundle refactor.
    raw_yaml: str | None = None
    try:
        from sqlalchemy import select as _select
        from sqlalchemy.orm import selectinload as _selectinload

        from digitorn.core.database import get_session_factory
        from digitorn.core.models import Application

        _sf = get_session_factory()
        async with _sf() as session:
            result = await session.execute(
                _select(Application)
                .options(_selectinload(Application.current_bundle))
                .where(Application.app_id == app_id)
            )
            app_row = result.scalar_one_or_none()
    except Exception as exc:
        logger.error("required_secrets db error app=%s: %s", app_id, exc)
        raise HTTPException(
            status_code=500, detail=f"Database error: {exc}",
        )

    if app_row is None:
        raise HTTPException(status_code=404, detail=f"App '{app_id}' not found")

    if app_row.current_bundle is not None:
        descriptor = manager._bundle_store.get_by_path(
            app_id, app_row.current_bundle.bundle_path,
        )
        if descriptor is not None:
            try:
                raw_yaml = manager._bundle_store.load_yaml(descriptor)
            except Exception as exc:
                logger.warning(
                    "required_secrets: bundle YAML unreadable for %s: %s",
                    app_id, exc,
                )

    if raw_yaml is None:
        raw_yaml = app_row.yaml_content

    if not raw_yaml:
        raise HTTPException(
            status_code=404,
            detail=(
                f"App '{app_id}' has no readable YAML (no bundle and no "
                f"yaml_content). Re-deploy it to enable secret introspection."
            ),
        )

    # Parse YAML and walk it for secret references. Even if the YAML is
    # malformed we still want to run the regex over the raw text as a
    # fallback - the user NEEDS to know what keys the app expects.
    try:
        import yaml as _yaml
        parsed = _yaml.safe_load(raw_yaml)
    except Exception:
        parsed = None

    hits: dict[str, dict[str, Any]] = {}
    ref_type: dict[str, str] = {}

    if isinstance(parsed, dict):
        _walk_yaml_for_secrets(parsed, [], hits)
        # Annotate each hit with the reference type (env vs secret) by
        # re-scanning the raw text.
        for key in hits:
            # Default to "env" since it's the common case; upgrade to
            # "secret" if we find a {{secret.KEY}} reference in the text.
            ref_type[key] = "env"
        for m in _SECRET_REF_RE.finditer(raw_yaml):
            ref_type[m.group(2)] = m.group(1)
    else:
        # Fallback: regex over raw text only (no used_by paths / no
        # provider or agent inference).
        for m in _SECRET_REF_RE.finditer(raw_yaml):
            key = m.group(2)
            hits.setdefault(
                key, {"locations": [], "providers": set(), "agents": set()},
            )
            ref_type[key] = m.group(1)

    # Cross-reference with what's actually in the store.
    stored_keys = set(await manager.list_secrets(app_id))

    # Also consult the (new) credentials DB for each inferred provider:
    # a secret is considered "set" if the calling user has a granted
    # credential for the same provider this app references. Without
    # this check the pre-session gate would keep reporting a secret
    # as missing even after the user created and granted it, because
    # ``list_secrets`` only sees the legacy per-app ``SecretStore``.
    _uid = getattr(request.state, "user_id", None) or "local"
    _cred_store = getattr(request.app.state, "credential_store", None)

    async def _credential_is_set(provider_name: str | None) -> bool:
        if not provider_name or _cred_store is None:
            return False
        try:
            row = await _cred_store.resolve_for_app(
                provider_name=provider_name,
                user_id=_uid,
                app_id=app_id,
                decrypt=False,
            )
        except Exception as exc:
            logger.warning(
                "required_secrets: resolve_for_app failed provider=%s: %s",
                provider_name, exc,
            )
            return False
        return row is not None

    # Pre-compute per-key metadata, then fan out the credential vault
    # checks in parallel - cold-open speed: the chat panel waits on
    # this fetch before mounting. Sequential awaits used to cost
    # N * Postgres latency (~50-100 ms each) for an app with several
    # secrets.
    sorted_keys = sorted(hits.keys())
    key_meta: list[tuple[str, dict[str, Any], list[str], list[str], str | None, str | None]] = []
    for key in sorted_keys:
        entry = hits[key]
        providers_list = sorted(entry.get("providers", set()))
        agents_list = sorted(entry.get("agents", set()))
        primary_provider = providers_list[0] if len(providers_list) == 1 else None
        primary_agent = agents_list[0] if len(agents_list) == 1 else None
        key_meta.append((key, entry, providers_list, agents_list, primary_provider, primary_agent))

    creds_set_results = await asyncio.gather(
        *(_credential_is_set(meta[4]) for meta in key_meta)
    )

    required: list[dict[str, Any]] = []
    missing_count = 0
    for (key, entry, providers_list, agents_list, primary_provider, primary_agent), is_set_in_creds in zip(
        key_meta, creds_set_results
    ):
        is_set_in_store = key in stored_keys
        is_set_in_env = key in os.environ
        is_set = is_set_in_store or is_set_in_env or is_set_in_creds
        if not is_set:
            missing_count += 1
        required.append({
            "key": key,
            "reference_type": ref_type.get(key, "env"),
            "used_by": sorted(set(entry.get("locations", []))),
            "is_set": is_set,
            "source": (
                "credentials" if is_set_in_creds
                else ("secret_store" if is_set_in_store
                      else ("daemon_env" if is_set_in_env else None))
            ),
            # Canonical provider name the credential should be stored
            # under - matches what ``session_resolver`` will look up
            # at turn time. Never the internal ``{agent}_brain`` id.
            "provider": primary_provider,
            "providers": providers_list,
            # Owning agent id(s), for UX grouping - the same secret
            # can legitimately be used by several agents sharing one
            # provider.
            "agent_id": primary_agent,
            "agent_ids": agents_list,
        })

    # Secrets stored but NOT referenced - orphans from an older version.
    referenced = set(hits.keys())
    unused = sorted(k for k in stored_keys if k not in referenced)

    return AppResponse(
        success=True,
        data={
            "app_id": app_id,
            "required": required,
            "missing_count": missing_count,
            "total_required": len(required),
            "unused_keys": unused,
        },
    )


@router.get("/{app_id}/secrets", response_model=AppResponse)
async def list_secrets(request: Request, app_id: str) -> AppResponse:
    """List secret key names for an app (values are never returned)."""
    _validate_id(app_id)
    manager = _get_manager(request)
    keys = await manager.list_secrets(app_id)
    return AppResponse(success=True, data={"app_id": app_id, "keys": keys})


@router.get("/{app_id}/secrets/{key}", response_model=AppResponse)
async def check_secret(request: Request, app_id: str, key: str) -> AppResponse:
    """Check if a secret exists (value is never returned)."""
    _validate_id(app_id)
    manager = _get_manager(request)
    value = await manager.get_secret(app_id, key)
    return AppResponse(
        success=True,
        data={"app_id": app_id, "key": key, "exists": value is not None},
    )


@router.put("/{app_id}/secrets", response_model=AppResponse)
async def set_secrets_bulk(
    request: Request,
    app_id: str,
    body: SecretsBulkSetRequest,
    reload: bool = True,
) -> AppResponse:
    """Set many secrets in one request, then optionally hot-reload.

    This is the convenience endpoint to use when rotating several keys
    at the same time. It writes each secret through ``SecretStore``
    (same code path as the per-key PUT) and then, if ``reload=true``
    (the default), triggers a single ``reload_app`` so every new value
    takes effect together. Without bulk, rotating N keys would cost N
    reloads which is wasteful and can momentarily break the app between
    two updates.
    """
    _require_permission(request, "apps:write")
    _validate_id(app_id)
    manager = _get_manager(request)

    if not body.secrets:
        return AppResponse(
            success=False,
            error="'secrets' map is empty - nothing to set.",
        )

    failed: dict[str, str] = {}
    set_count = 0
    for key, value in body.secrets.items():
        if not isinstance(key, str) or not key:
            failed[str(key)] = "invalid key"
            continue
        if not isinstance(value, str):
            failed[key] = "value must be a string"
            continue
        try:
            await manager.set_secret(app_id, key, value)
            set_count += 1
        except Exception as exc:
            failed[key] = str(exc)

    reloaded = False
    reload_error: str | None = None
    if reload and set_count > 0 and _is_deployed(request, app_id):
        try:
            await manager.reload_app(app_id)
            reloaded = True
        except Exception as exc:
            logger.warning(
                "bulk_secret_reload_failed app=%s: %s", app_id, exc,
            )
            reload_error = str(exc)

    return AppResponse(
        success=len(failed) == 0,
        data={
            "app_id": app_id,
            "set_count": set_count,
            "failed": failed,
            "reloaded": reloaded,
            "reload_error": reload_error,
        },
        error=(
            f"{len(failed)} secret(s) failed to set: {list(failed.keys())}"
            if failed else None
        ),
    )


@router.put("/{app_id}/secrets/{key}", response_model=AppResponse)
async def set_secret(
    request: Request,
    app_id: str,
    key: str,
    body: SecretSetRequest,
    reload: bool = True,
) -> AppResponse:
    """Set (or update) an encrypted secret for an app.

    By default the running app is **hot-reloaded** immediately so the
    new value takes effect without a daemon restart - the typical use
    case is rotating an API key and wanting the next request to use it.
    Pass ``?reload=false`` when you want to stage multiple secret
    updates and trigger a single reload at the end via
    ``POST /api/apps/{app_id}/reload`` or the bulk ``PUT /secrets``.
    """
    _require_permission(request, "apps:write")
    _validate_id(app_id)
    manager = _get_manager(request)
    try:
        await manager.set_secret(app_id, key, body.value)
    except Exception as exc:
        return AppResponse(success=False, error=f"Failed to set secret: {exc}")

    reloaded = False
    reload_error: str | None = None
    if reload and _is_deployed(request, app_id):
        try:
            await manager.reload_app(app_id)
            reloaded = True
        except Exception as exc:
            logger.warning(
                "secret_set_reload_failed app=%s key=%s: %s",
                app_id, key, exc,
            )
            reload_error = str(exc)

    return AppResponse(
        success=True,
        data={
            "app_id": app_id,
            "key": key,
            "status": "set",
            "reloaded": reloaded,
            "reload_error": reload_error,
        },
    )


@router.delete("/{app_id}/secrets/{key}", response_model=AppResponse)
async def delete_secret(request: Request, app_id: str, key: str) -> AppResponse:
    """Delete a secret."""
    _require_permission(request, "apps:write")
    _validate_id(app_id)
    manager = _get_manager(request)
    deleted = await manager.delete_secret(app_id, key)
    if not deleted:
        raise HTTPException(status_code=404, detail=f"Secret '{key}' not found")
    return AppResponse(
        success=True, data={"app_id": app_id, "key": key, "status": "deleted"}
    )

