"""Routes for the lifecycle group."""

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
    _is_admin,
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


async def list_apps(
    request: Request,
    include_disabled: bool = False,
    include_installed: bool = True,
    include_hidden: bool = False,
) -> AppResponse:
    """List apps visible to the caller - unified view of deployed + disabled + installed."""
    manager = _get_manager(request)
    user_id = _caller_user_id(request)

    # Pull deployed apps first - these always have the richest runtime data.
    apps = list(manager.list_apps(user_id=user_id))

    # Build a set of already-seen app_ids to de-dup when we merge in the
    # registry-only rows below.
    seen_ids: set[str] = {a.get("app_id", "") for a in apps if isinstance(a, dict)}

    registry = getattr(request.app.state, "package_registry", None)
    pkg_by_id: dict[str, dict] = {}
    if registry is not None:
        try:
            rows = await registry.list_visible_to_user(user_id=user_id)
            for row in rows or []:
                if isinstance(row, dict) and row.get("package_id"):
                    pkg_by_id[row["package_id"]] = row
        except Exception as exc:
            logger.debug("registry bulk fetch for list failed: %s", exc)

    last_used_by_id: dict[str, str] = {}
    if user_id:
        try:
            from sqlalchemy import select, func
            from digitorn.core.database import get_session_factory
            from digitorn.core.models import UserSession
            sf = get_session_factory()
            async with sf() as session:
                stmt = (
                    select(
                        UserSession.app_id,
                        func.max(UserSession.last_active_at).label("last_used"),
                    )
                    .where(UserSession.user_id == user_id)
                    .where(UserSession.deleted_at.is_(None))
                    .group_by(UserSession.app_id)
                )
                result = await session.execute(stmt)
                for row in result.all():
                    if row.app_id and row.last_used:
                        last_used_by_id[row.app_id] = row.last_used.isoformat()
        except Exception as exc:
            logger.debug("last_used_at bulk fetch failed: %s", exc)

    # Every deployed entry defaults to "running" unless later marked disabled.
    # Enrich with source attribution from the registry row when available.
    for a in apps:
        if isinstance(a, dict):
            a.setdefault("runtime_status", "running")
            a.setdefault("install_status", "installed")
            a["last_used_at"] = last_used_by_id.get(a.get("app_id") or "")
            pkg = pkg_by_id.get(a.get("app_id") or "")
            if pkg is not None:
                a.setdefault("source_type", pkg.get("source_type") or "")
                a.setdefault("source_uri", pkg.get("source_uri") or "")
                a.setdefault("installed_by", pkg.get("installed_by") or "")
                a.setdefault("install_dir", pkg.get("install_dir") or "")
                a.setdefault("hash", pkg.get("hash") or "")
                a.setdefault("installed_at", pkg.get("installed_at"))
                a.setdefault("scope", pkg.get("scope") or "system")
                a.setdefault("owner_user_id", pkg.get("owner_user_id"))

    # Admin-only wide view: include disabled apps from DB.
    if include_disabled:
        perms = list(getattr(request.state, "permissions", []) or [])
        try:
            if "*" in perms:
                disabled = await manager.list_disabled_apps()
            else:
                disabled = await manager.list_disabled_apps(
                    user_id=user_id or None,
                )
            for d in disabled:
                if isinstance(d, dict):
                    d["runtime_status"] = "disabled"
                    d.setdefault("install_status", "installed")
                    d.setdefault("last_used_at", last_used_by_id.get(d.get("app_id") or ""))
                    if d.get("app_id") not in seen_ids:
                        apps.append(d)
                        seen_ids.add(d["app_id"])
        except Exception as exc:
            logger.warning("list_disabled_apps failed: %s", exc, exc_info=True)

    # Merge installed packages that weren't deployed - broken builds,
    # never-deployed installs, in-progress uninstalls.
    if include_installed:
        registry = getattr(request.app.state, "package_registry", None)
        if registry is not None:
            try:
                rows = await registry.list_visible_to_user(user_id=user_id)
                for row in rows or []:
                    if not isinstance(row, dict):
                        continue
                    pkg_id = row.get("package_id") or ""
                    if not pkg_id or pkg_id in seen_ids:
                        continue
                    row_status = (row.get("status") or "").lower()
                    runtime_status = "broken" if row_status == "broken" else "not_deployed"
                    apps.append({
                        "app_id": pkg_id,
                        "name": row.get("name") or pkg_id,
                        "version": row.get("version", ""),
                        "description": row.get("description", ""),
                        "is_builtin": bool(row.get("is_builtin", False)),
                        "is_default": bool(row.get("is_default", False)),
                        "installed_at": row.get("installed_at"),
                        "last_used_at": last_used_by_id.get(pkg_id),
                        "install_status": row_status or "installed",
                        "runtime_status": runtime_status,
                        "deploy_error": row.get("deploy_error"),
                        "scope": row.get("scope") or "system",
                        "owner_user_id": row.get("owner_user_id"),
                        # Source attribution - lets the client tell
                        # builtin / local / hub / git apart.
                        "source_type": row.get("source_type") or "",
                        "source_uri": row.get("source_uri") or "",
                        "installed_by": row.get("installed_by") or "",
                        "install_dir": row.get("install_dir") or "",
                        "hash": row.get("hash") or "",
                    })
                    seen_ids.add(pkg_id)
            except Exception as exc:
                logger.warning("list_installed_packages failed: %s", exc, exc_info=True)

    if not include_hidden and apps:
        try:
            from sqlalchemy import select as _select
            from digitorn.core.database import get_session_factory as _gsf
            from digitorn.core.models import Application as _App
            _sf = _gsf()
            _app_ids = [str(a.get("app_id") or "") for a in apps if isinstance(a, dict)]
            _app_ids = list({a for a in _app_ids if a})
            if _app_ids:
                async with _sf() as _s:
                    _stmt = _select(
                        _App.app_id, _App.scope, _App.owner_user_id,
                    ).where(_App.app_id.in_(_app_ids)).where(_App.hidden == True)  # noqa: E712
                    _r = await _s.execute(_stmt)
                    _hidden_keys: set[tuple[str, str, str]] = set()
                    for row in _r.all():
                        _hidden_keys.add((row.app_id, row.scope, row.owner_user_id or ""))
                if _hidden_keys:
                    apps = [
                        a for a in apps
                        if not (
                            isinstance(a, dict)
                            and (
                                a.get("app_id") or "",
                                a.get("scope") or "system",
                                a.get("owner_user_id") or "",
                            ) in _hidden_keys
                        )
                    ]
        except Exception as exc:
            logger.warning("list_apps_hidden_filter_failed: %s", exc, exc_info=True)

    return AppResponse(success=True, data=apps)


@router.post("/sync-deployed", response_model=AppResponse)
async def sync_deployed_with_db(request: Request) -> AppResponse:
    """Admin: reconcile in-memory `_deployed` with the DB."""
    if not _is_admin(request):
        raise HTTPException(
            status_code=403,
            detail="sync-deployed is admin-only.",
        )
    manager = _get_manager(request)
    try:
        result = await manager.sync_deployed_with_db()
    except Exception as exc:
        logger.error("sync_deployed_with_db failed: %s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail=str(exc))
    return AppResponse(success=True, data=result)


@router.get("/disabled", response_model=AppResponse)
async def list_disabled(request: Request) -> AppResponse:
    """List disabled apps visible to the caller."""
    manager = _get_manager(request)
    is_admin = _is_admin(request)
    caller_user_id = _caller_user_id(request) or None

    try:
        if is_admin:
            disabled = await manager.list_disabled_apps()
        else:
            disabled = await manager.list_disabled_apps(
                user_id=caller_user_id,
            )
    except Exception as exc:
        logger.warning("list_disabled failed: %s", exc, exc_info=True)
        disabled = []

    return AppResponse(success=True, data=disabled)


@router.get("/{app_id}/manifest", response_model=AppResponse)
async def get_app_manifest(request: Request, app_id: str) -> AppResponse:
    """Return the deployed app's manifest (flat shape consumed by the"""
    _validate_id(app_id)
    deployed = _get_deployed(request, app_id)
    if deployed is None:
        _raise_not_deployed(request, app_id)
    data = deployed.summary()
    return AppResponse(success=True, data=data)


@router.get("/{app_id}", response_model=AppResponse)
async def get_app(request: Request, app_id: str) -> AppResponse:
    """Get unified details of an installed app - runtime + installation."""
    _validate_id(app_id)
    deployed = _get_deployed(request, app_id)
    registry = getattr(request.app.state, "package_registry", None)

    # Pull registry metadata (may be None if no install row).
    pkg: dict | None = None
    drift: dict | None = None
    if registry is not None:
        try:
            pkg = await registry.get(app_id)
        except Exception:
            pkg = None
        if pkg is not None:
            try:
                drift = await registry.check_drift(app_id)
            except Exception as exc:
                logger.debug("drift check failed for %s: %s", app_id, exc)
                drift = {"error": str(exc)}

    if deployed is not None:
        data = deployed.summary()
        if isinstance(data, dict):
            data["runtime_status"] = "running"
            if pkg is not None:
                data.setdefault("version", pkg.get("version", ""))
                data["is_builtin"] = bool(pkg.get("is_builtin", False))
                data["is_default"] = bool(pkg.get("is_default", False))
                data["installed_at"] = pkg.get("installed_at")
                data["install_status"] = (pkg.get("status") or "installed").lower()
                data["scope"] = pkg.get("scope") or "system"
                data["owner_user_id"] = pkg.get("owner_user_id")
                data["source_type"] = pkg.get("source_type") or ""
                data["source_uri"] = pkg.get("source_uri") or ""
                data["installed_by"] = pkg.get("installed_by") or ""
                data["install_dir"] = pkg.get("install_dir") or ""
                data["hash"] = pkg.get("hash") or ""
                if drift is not None:
                    data["drift"] = drift
            else:
                data.setdefault("install_status", "installed")
        return AppResponse(success=True, data=data)

    # Not deployed - maybe just registered or broken.
    if pkg is not None:
        row_status = (pkg.get("status") or "").lower()
        runtime_status = "broken" if row_status == "broken" else "not_deployed"
        return AppResponse(
            success=True,
            data={
                "app_id": app_id,
                "name": pkg.get("name") or app_id,
                "version": pkg.get("version", ""),
                "description": pkg.get("description", ""),
                "is_builtin": bool(pkg.get("is_builtin", False)),
                "is_default": bool(pkg.get("is_default", False)),
                "installed_at": pkg.get("installed_at"),
                "install_status": row_status or "installed",
                "runtime_status": runtime_status,
                "deploy_error": pkg.get("deploy_error"),
                "scope": pkg.get("scope") or "system",
                "owner_user_id": pkg.get("owner_user_id"),
                "source_type": pkg.get("source_type") or "",
                "source_uri": pkg.get("source_uri") or "",
                "installed_by": pkg.get("installed_by") or "",
                "install_dir": pkg.get("install_dir") or "",
                "hash": pkg.get("hash") or "",
                "drift": drift,
            },
        )

    _raise_not_deployed(request, app_id)


@router.post("/deploy", response_model=AppResponse)
async def deploy_app(request: Request, body: DeployRequest) -> AppResponse:
    """Deploy an app from a YAML file path."""
    _require_permission(request, "apps:deploy")
    manager = _get_manager(request)

    if not body.yaml_path:
        raise HTTPException(status_code=400, detail="yaml_path is required")

    # scope default = user for non-admins (private install); system = admin-only (or YAML inside builtins dir).
    caller_user_id = _caller_user_id(request) or None
    # Capture caller's bearer NOW so it survives into the asyncio.create_task
    # closure below. Used to authenticate cron auto-fires that have no inbound
    # HTTP context (background.py:_run_single_activation reads this back).
    caller_token = getattr(request.state, "access_token", None)
    is_admin = _is_admin(request)
    # YAML inside the daemon's builtins/ tree may deploy at scope=system without admin (same risk model as bootstrap_builtins).
    def _yaml_is_builtin() -> bool:
        try:
            from digitorn.core.packages.bootstrap import _default_builtins_dir
            builtins_root = _default_builtins_dir().resolve()
            target = Path(body.yaml_path).resolve()
            target.relative_to(builtins_root)
            return True
        except Exception:
            return False
    if body.scope == "system":
        if not is_admin and not _yaml_is_builtin():
            raise HTTPException(
                status_code=403,
                detail=(
                    "Only administrators can deploy at scope=system. "
                    "Drop the scope field or pass scope=user for a "
                    "private install."
                ),
            )
        deploy_scope = "system"
    elif body.scope == "user":
        deploy_scope = "user"
    else:
        deploy_scope = "system" if is_admin else "user"
    # always track caller as owner; registry maps to NULL on scope=system but DELETE permission checks read it.
    deploy_owner = caller_user_id

    raw_path = Path(body.yaml_path)
    if raw_path.is_symlink():
        raise HTTPException(status_code=400, detail="Symlinks are not allowed in YAML paths.")
    yaml_path = raw_path.resolve()
    if not yaml_path.exists():
        raise HTTPException(status_code=404, detail="YAML file not found.")
    if not str(yaml_path).endswith((".yaml", ".yml")):
        raise HTTPException(
            status_code=400,
            detail="Only .yaml/.yml files are accepted.",
        )

    # pre-flight compile catches YAML errors fast; forward inline secrets so {{env.X}} references resolve here too.
    try:
        compiled = manager._compiler.compile_file(yaml_path, secrets=body.secrets)
        app_id = compiled.meta.app_id
    except Exception as exc:
        errors = getattr(exc, "errors", [str(exc)])
        return AppResponse(success=False, error=f"App compilation failed ({len(errors)} error(s)): {'; '.join(str(e) for e in errors[:5])}")

    # record per-app deploy errors so /diagnostics + /deploy-status can surface async failures.
    async def _deploy_bg():
        try:
            deployed = await manager.deploy(
                yaml_path, force=body.force, inline_secrets=body.secrets,
                scope=deploy_scope, owner_user_id=deploy_owner,
            )
            if body.secrets:
                for k, v in body.secrets.items():
                    await manager.set_secret(deployed.app_id, k, v)
            if caller_token:
                try:
                    from digitorn.core.database import get_session_factory
                    from digitorn.core.models import Application
                    from sqlalchemy import select, update
                    SF = get_session_factory()
                    async with SF() as _s:
                        await _s.execute(
                            update(Application)
                            .where(
                                Application.app_id == app_id,
                                Application.scope == deploy_scope,
                                Application.owner_user_id == (deploy_owner or ""),
                            )
                            .values(deployer_jwt={"token": caller_token})
                        )
                        await _s.commit()
                except Exception as _jwt_exc:
                    logger.warning(
                        "deployer_jwt_persist_failed app=%s: %s",
                        app_id, _jwt_exc, exc_info=True,
                    )
            logger.info(
                "deploy_complete app=%s scope=%s owner=%s",
                app_id, deploy_scope, deploy_owner or "-",
            )
            try:
                if hasattr(manager, "_deploy_errors"):
                    manager._deploy_errors.pop(app_id, None)
            except Exception as exc:
                logger.debug("lifecycle best-effort block failed: %s", exc)

            # mirror the deploy as an installed_packages row so list_apps + uninstall can find it.
            try:
                registry = getattr(request.app.state, "package_registry", None)
                if registry is not None:
                    import hashlib as _hashlib
                    _yaml_bytes = yaml_path.read_bytes()
                    _hash = _hashlib.sha256(_yaml_bytes).hexdigest()
                    # installed_packages forbids owner on scope=system rows.
                    _registry_owner = (
                        deploy_owner if deploy_scope == "user" else None
                    )
                    _manifest = {
                        "name": compiled.meta.name,
                        "version": compiled.meta.version,
                        "description": getattr(compiled.meta, "description", "") or "",
                        "author": getattr(compiled.meta, "author", "") or "",
                    }
                    await registry.create(
                        package_id=app_id,
                        source_type="local",
                        source_uri=str(yaml_path),
                        version=compiled.meta.version or "0.0.0",
                        hash=_hash,
                        manifest=_manifest,
                        installed_by=caller_user_id or "",
                        status="installed",
                        scope=deploy_scope,
                        owner_user_id=_registry_owner,
                    )
                    logger.info(
                        "deploy_registry_upserted app=%s scope=%s",
                        app_id, deploy_scope,
                    )
            except Exception as _reg_exc:
                logger.warning(
                    "deploy_registry_upsert_failed app=%s: %s",
                    app_id, _reg_exc, exc_info=True,
                )
        except Exception as exc:
            logger.error("deploy_failed app=%s: %s", app_id, exc, exc_info=True)
            try:
                import time as _time, traceback as _tb
                store = getattr(manager, "_deploy_errors", None)
                if store is None:
                    store = {}
                    manager._deploy_errors = store
                store[app_id] = {
                    "app_id": app_id,
                    "error": f"{type(exc).__name__}: {exc}"[:800],
                    "traceback": "".join(
                        _tb.format_exception(type(exc), exc, exc.__traceback__)
                    )[:4000],
                    "yaml_path": str(yaml_path),
                    "failed_at": _time.time(),
                }
            except Exception:
                logger.debug("deploy_error_store_failed", exc_info=True)

    asyncio.create_task(_deploy_bg())

    return AppResponse(success=True, data={
        "app_id": app_id,
        "name": compiled.meta.name,
        "version": compiled.meta.version,
        "scope": deploy_scope,
        "owner_user_id": deploy_owner,
        "status": "deploying",
        "message": "Deployment started. Poll GET /api/apps/{app_id} to check status.",
    })


@router.post("/deploy/upload", response_model=AppResponse)
async def deploy_app_upload(
    request: Request,
    file: UploadFile = File(...),
    force: bool = Form(False),
    secrets: str | None = Form(None),
    assets: str | None = Form(None),
    scope: str = Form("system"),
) -> AppResponse:
    """Deploy an app by uploading a YAML file + its referenced assets."""
    _require_permission(request, "apps:deploy")
    manager = _get_manager(request)

    _MAX_YAML_SIZE = 1_048_576       # 1 MB
    _MAX_ASSETS_TOTAL = 5_242_880    # 5 MB combined
    _MAX_ASSET_PATH_LEN = 512

    content = await file.read(_MAX_YAML_SIZE + 1)
    if len(content) > _MAX_YAML_SIZE:
        raise HTTPException(
            status_code=413,
            detail=f"YAML file too large (max {_MAX_YAML_SIZE // 1024} KB).",
        )

    inline_secrets: dict[str, str] | None = None
    if secrets:
        try:
            parsed = _json.loads(secrets)
        except Exception as exc:
            raise HTTPException(
                status_code=400,
                detail=f"'secrets' must be a JSON object: {exc}",
            )
        if not isinstance(parsed, dict):
            raise HTTPException(
                status_code=400,
                detail="'secrets' must be a JSON object of key/value strings.",
            )
        inline_secrets = {str(k): str(v) for k, v in parsed.items()}

    asset_map: dict[str, str] = {}
    if assets:
        try:
            parsed_assets = _json.loads(assets)
        except Exception as exc:
            raise HTTPException(
                status_code=400,
                detail=f"'assets' must be a JSON object: {exc}",
            )
        if not isinstance(parsed_assets, dict):
            raise HTTPException(
                status_code=400,
                detail="'assets' must be a JSON object of relpath/content strings.",
            )
        total_size = 0
        for rel, body in parsed_assets.items():
            if not isinstance(rel, str) or not isinstance(body, str):
                raise HTTPException(
                    status_code=400,
                    detail="'assets' keys and values must both be strings.",
                )
            if len(rel) > _MAX_ASSET_PATH_LEN:
                raise HTTPException(
                    status_code=400,
                    detail=f"asset path too long: {rel[:80]}...",
                )
            # Normalise and reject any path that tries to escape the
            # temp dir (absolute, contains .., Windows drive letters).
            norm = rel.replace("\\", "/").strip()
            while norm.startswith("./"):
                norm = norm[2:]
            if (
                not norm
                or norm.startswith("/")
                or ".." in norm.split("/")
                or ":" in norm
            ):
                raise HTTPException(
                    status_code=400,
                    detail=f"unsafe asset path rejected: {rel!r}",
                )
            total_size += len(body.encode("utf-8"))
            if total_size > _MAX_ASSETS_TOTAL:
                raise HTTPException(
                    status_code=413,
                    detail=f"assets too large (max {_MAX_ASSETS_TOTAL // 1024} KB total).",
                )
            asset_map[norm] = body

    # dedicated temp dir so the YAML + its assets share a parent (compiler resolves ./skills/X.md relative to the YAML).
    tmp_dir = Path(tempfile.mkdtemp(prefix="digitorn-deploy-"))
    yaml_filename = file.filename or "app.yaml"
    yaml_filename = Path(yaml_filename).name or "app.yaml"
    yaml_path = tmp_dir / yaml_filename

    try:
        # bulk disk write off-loop; sizeable bundles (web/dist, skills) accumulate hundreds of ms of GIL-held IO.
        def _write_bundle_to_disk() -> None:
            yaml_path.write_bytes(content)
            for rel, body in asset_map.items():
                asset_path = tmp_dir / rel
                asset_path.parent.mkdir(parents=True, exist_ok=True)
                try:
                    asset_path.resolve().relative_to(tmp_dir.resolve())
                except ValueError as _e:
                    raise HTTPException(
                        status_code=400,
                        detail=f"asset path escapes temp dir: {rel}",
                    ) from _e
                asset_path.write_text(body, encoding="utf-8")
        await asyncio.to_thread(_write_bundle_to_disk)
    except Exception:
        await asyncio.to_thread(shutil.rmtree, tmp_dir, True)
        raise

    logger.info(
        "deploy_upload_received yaml_filename=%s yaml_bytes=%d assets_count=%d "
        "secrets_count=%d tmp_dir=%s",
        yaml_filename, len(content), len(asset_map),
        len(inline_secrets or {}), tmp_dir,
    )

    try:
        compiled = await asyncio.to_thread(
            manager._compiler.compile_file, yaml_path, secrets=inline_secrets,
        )
        app_id = compiled.meta.app_id
    except Exception as exc:
        await asyncio.to_thread(shutil.rmtree, tmp_dir, True)
        errors = getattr(exc, "errors", [str(exc)])
        error_msg = f"Compilation failed: {'; '.join(str(e) for e in errors[:5])}"
        if len(asset_map) == 0 and any(
            "file not found" in str(e).lower() for e in errors
        ):
            error_msg += (
                "\n\nHint: the client uploaded 0 assets. If your YAML "
                "references skill files (skills/*.md) or agent prompt "
                "files, you must send them in the 'assets' form field as a "
                "JSON map of {relative_path: content}. See "
                "POST /api/apps/deploy/upload docs."
            )
        return AppResponse(success=False, error=error_msg)

    caller_user_id = _caller_user_id(request) or None
    deploy_scope = scope if scope in ("system", "user") else "system"
    deploy_owner = (
        caller_user_id if deploy_scope == "user" else None
    )

    async def _deploy_upload_bg():
        try:
            deployed = await manager.deploy(
                yaml_path,
                force=force,
                inline_secrets=inline_secrets,
                scope=deploy_scope,
                owner_user_id=deploy_owner,
            )
            if inline_secrets:
                for k, v in inline_secrets.items():
                    await manager.set_secret(deployed.app_id, k, v)
            logger.info(
                "deploy_upload_complete app=%s scope=%s assets=%d",
                app_id, deploy_scope, len(asset_map),
            )
        except Exception as exc:
            logger.error(
                "deploy_upload_failed app=%s scope=%s: %s",
                app_id, deploy_scope, exc,
            )
        finally:
            await asyncio.to_thread(shutil.rmtree, tmp_dir, True)

    asyncio.create_task(_deploy_upload_bg())

    return AppResponse(success=True, data={
        "app_id": app_id,
        "name": compiled.meta.name,
        "status": "deploying",
        "asset_count": len(asset_map),
        "message": "Deployment started. Poll GET /api/apps/{app_id} to check status.",
    })


@router.post("/validate", response_model=AppResponse)
async def validate_app(request: Request, body: ValidateRequest) -> AppResponse:
    """Validate an app YAML file without deploying it."""
    raw_path = Path(body.yaml_path)
    if raw_path.is_symlink():
        raise HTTPException(status_code=400, detail="Symlinks are not allowed.")
    yaml_path = raw_path.resolve()
    if not yaml_path.exists():
        raise HTTPException(status_code=404, detail="YAML file not found.")

    manager = _get_manager(request)
    try:
        compiled = manager._compiler.compile_file(yaml_path)
    except Exception as exc:
        errors = getattr(exc, "errors", [str(exc)])
        return AppResponse(success=False, error=f"Validation failed ({len(errors)} error(s))", data={
            "errors": errors,
        })

    constrained = [mid for mid, m in compiled.modules.items() if m.constraints]
    return AppResponse(success=True, data={
        "app_id": compiled.meta.app_id,
        "name": compiled.meta.name,
        "version": compiled.meta.version,
        "modules": list(compiled.module_ids),
        "agents": [a.agent_id for a in compiled.agents],
        "setup_steps": sum(len(m.setup_steps) for m in compiled.modules.values()),
        "constrained_modules": constrained,
        "security_policy": compiled.security_profile.default_policy if compiled.security_profile else None,
        "max_risk_level": compiled.security_profile.max_risk_level if compiled.security_profile else None,
        "skills": len(compiled.skills),
    })


@router.post("/{app_id}/disable", response_model=AppResponse)
async def disable_app(
    request: Request,
    app_id: str,
    body: DisableRequest | None = None,
    scope: str | None = None,
    owner_user_id: str | None = None,
) -> AppResponse:
    """Disable a scoped app install - hide it + refuse interaction."""
    _require_permission(request, "apps:undeploy")
    _validate_id(app_id)
    manager = _get_manager(request)

    caller_user_id = _caller_user_id(request) or None
    is_admin = _is_admin(request)

    if scope == "system" and not is_admin:
        raise HTTPException(
            status_code=403,
            detail="Only administrators can target the system scope.",
        )
    if owner_user_id and not is_admin and owner_user_id != caller_user_id:
        raise HTTPException(
            status_code=403,
            detail="Only administrators can disable other users' installs.",
        )

    reason = (body.reason if body is not None else None) or None

    if scope == "system":
        target_user_id: str | None = None
    else:
        target_user_id = owner_user_id if (is_admin and owner_user_id) else caller_user_id

    try:
        result = await manager.disable_app(
            app_id,
            user_id=target_user_id,
            scope=scope,
            reason=reason,
        )
    except RuntimeError as exc:
        msg = str(exc)
        if "not found" in msg.lower():
            raise HTTPException(status_code=404, detail=msg)
        raise HTTPException(status_code=400, detail=msg)
    except Exception as exc:
        logger.error("disable_app_failed app=%s: %s", app_id, exc, exc_info=True)
        raise HTTPException(status_code=500, detail=f"Disable failed: {exc}")

    return AppResponse(success=True, data={
        **result,
        "message": f"App '{app_id}' disabled. Admin must re-enable via POST /api/apps/{app_id}/enable.",
    })


@router.post("/{app_id}/enable", response_model=AppResponse)
async def enable_app(
    request: Request,
    app_id: str,
    scope: str | None = None,
    user_id: str | None = None,
) -> AppResponse:
    """Re-enable a disabled app (ADMIN ONLY) and redeploy it."""
    _validate_id(app_id)
    is_admin = _is_admin(request)
    if not is_admin:
        raise HTTPException(
            status_code=403,
            detail="Only administrators can re-enable a disabled app.",
        )

    manager = _get_manager(request)
    try:
        result = await manager.enable_app(
            app_id,
            user_id=user_id,
            scope=scope,
        )
    except RuntimeError as exc:
        msg = str(exc)
        if "not found" in msg.lower():
            raise HTTPException(status_code=404, detail=msg)
        raise HTTPException(status_code=400, detail=msg)
    except Exception as exc:
        logger.error("enable_app_failed app=%s: %s", app_id, exc, exc_info=True)
        raise HTTPException(status_code=500, detail=f"Enable failed: {exc}")

    return AppResponse(success=True, data={
        **result,
        "message": f"App '{app_id}' re-enabled.",
    })


@router.post("/{app_id}/hide", response_model=AppResponse)
async def hide_app(
    request: Request,
    app_id: str,
    scope: str | None = None,
    owner_user_id: str | None = None,
) -> AppResponse:
    """Hide an app from non-admin listings without disabling it. Reversible via POST /show."""
    _validate_id(app_id)

    caller_user_id = _caller_user_id(request) or None
    is_admin = _is_admin(request)

    if scope == "system" and not is_admin:
        raise HTTPException(
            status_code=403,
            detail="Only administrators can target the system scope.",
        )
    if owner_user_id and not is_admin and owner_user_id != caller_user_id:
        raise HTTPException(
            status_code=403,
            detail="Only administrators can hide other users' installs.",
        )

    if scope == "system":
        target_user_id: str | None = None
        target_scope = "system"
    else:
        target_user_id = owner_user_id if (is_admin and owner_user_id) else caller_user_id
        target_scope = "user" if target_user_id else "system"

    from digitorn.core.database import get_session_factory
    from datetime import datetime as _dt, timezone as _tz
    from sqlalchemy import text as _sql_text

    try:
        sf = get_session_factory()
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=f"DB not initialised: {exc}")

    async with sf() as session:
        async with session.begin():
            r = await session.execute(
                _sql_text(
                    "UPDATE applications "
                    "SET hidden = :h, hidden_at = :t "
                    "WHERE app_id = :a AND scope = :s AND owner_user_id = :o"
                ),
                {
                    "h": True, "t": _dt.now(_tz.utc),
                    "a": app_id, "s": target_scope,
                    "o": target_user_id or "",
                },
            )
            if r.rowcount == 0:
                raise HTTPException(
                    status_code=404,
                    detail=(
                        f"App '{app_id}' (scope={target_scope}, "
                        f"owner={target_user_id!r}) not found"
                    ),
                )

    logger.info(
        "app_hidden app=%s scope=%s owner=%r",
        app_id, target_scope, target_user_id,
    )
    return AppResponse(success=True, data={
        "app_id": app_id,
        "scope": target_scope,
        "owner_user_id": target_user_id,
        "hidden": True,
        "message": f"App '{app_id}' hidden (scope={target_scope}). Use POST /api/apps/{app_id}/show to reverse.",
    })


@router.post("/{app_id}/show", response_model=AppResponse)
async def show_app(
    request: Request,
    app_id: str,
    scope: str | None = None,
    owner_user_id: str | None = None,
) -> AppResponse:
    """Un-hide an app. Inverse of POST /{app_id}/hide."""
    _validate_id(app_id)

    caller_user_id = _caller_user_id(request) or None
    is_admin = _is_admin(request)

    if scope == "system" and not is_admin:
        raise HTTPException(
            status_code=403,
            detail="Only administrators can target the system scope.",
        )
    if owner_user_id and not is_admin and owner_user_id != caller_user_id:
        raise HTTPException(
            status_code=403,
            detail="Only administrators can show other users' installs.",
        )

    if scope == "system":
        target_user_id: str | None = None
        target_scope = "system"
    else:
        target_user_id = owner_user_id if (is_admin and owner_user_id) else caller_user_id
        target_scope = "user" if target_user_id else "system"

    from digitorn.core.database import get_session_factory
    from sqlalchemy import text as _sql_text

    try:
        sf = get_session_factory()
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=f"DB not initialised: {exc}")

    async with sf() as session:
        async with session.begin():
            r = await session.execute(
                _sql_text(
                    "UPDATE applications "
                    "SET hidden = :h, hidden_at = NULL "
                    "WHERE app_id = :a AND scope = :s AND owner_user_id = :o"
                ),
                {
                    "h": False,
                    "a": app_id, "s": target_scope,
                    "o": target_user_id or "",
                },
            )
            if r.rowcount == 0:
                raise HTTPException(
                    status_code=404,
                    detail=(
                        f"App '{app_id}' (scope={target_scope}, "
                        f"owner={target_user_id!r}) not found"
                    ),
                )

    logger.info(
        "app_shown app=%s scope=%s owner=%r",
        app_id, target_scope, target_user_id,
    )
    return AppResponse(success=True, data={
        "app_id": app_id,
        "scope": target_scope,
        "owner_user_id": target_user_id,
        "hidden": False,
        "message": f"App '{app_id}' is now visible.",
    })


@router.delete("/{app_id}", response_model=AppResponse)
async def delete_app(
    request: Request,
    app_id: str,
    undeploy_only: bool = False,
    delete_history: bool = True,
    scope: str | None = None,
    owner_user_id: str | None = None,
) -> AppResponse:
    """Delete a scoped app install - scope-aware removal. Built-in apps cannot be deleted."""
    _require_permission(request, "apps:undeploy")
    _validate_id(app_id)
    manager = _get_manager(request)

    # full delete accepts both in-memory and DB-only states; undeploy_only requires the in-memory presence.
    is_in_memory = _is_deployed(request, app_id)
    if undeploy_only and not is_in_memory:
        raise HTTPException(
            status_code=404, detail=f"App '{app_id}' not deployed",
        )

    deployed = _get_deployed(request, app_id)
    if deployed is not None and getattr(deployed, "builtin", False):
        return AppResponse(
            success=False,
            error=f"Cannot remove built-in app '{app_id}'.",
        )

    caller_user_id = _caller_user_id(request) or None
    is_admin = _is_admin(request)

    if scope == "system" and not is_admin:
        raise HTTPException(
            status_code=403,
            detail="Only administrators can target the system scope.",
        )
    if owner_user_id and not is_admin and owner_user_id != caller_user_id:
        raise HTTPException(
            status_code=403,
            detail="Only administrators can delete other users' installs.",
        )

    if undeploy_only:
        async def _undeploy_bg():
            try:
                await manager.undeploy(app_id, user_id=caller_user_id)
                logger.info("undeploy_complete app=%s", app_id)
            except Exception as exc:
                logger.error("undeploy_failed app=%s: %s", app_id, exc)

        asyncio.create_task(_undeploy_bg())
        return AppResponse(
            success=True,
            data={
                "app_id": app_id,
                "undeployed": True,
                "deleted": False,
                "message": "App stopped. Data preserved - will reload at next daemon restart.",
            },
        )

    if scope == "system":
        target_user_id: str | None = None
    else:
        target_user_id = owner_user_id if (is_admin and owner_user_id) else caller_user_id

    # Full delete: synchronous so the caller knows the outcome.
    try:
        result = await manager.delete_app(
            app_id,
            user_id=target_user_id,
            scope=scope,
            delete_history=delete_history,
        )
    except RuntimeError as exc:
        msg = str(exc)
        low = msg.lower()
        if "built-in" in low or "builtin" in low or "cannot delete" in low:
            raise HTTPException(status_code=403, detail=msg)
        if "not found" in low:
            raise HTTPException(status_code=404, detail=msg)
        raise HTTPException(status_code=400, detail=msg)
    except Exception as exc:
        logger.error("delete_app_failed app=%s: %s", app_id, exc, exc_info=True)
        raise HTTPException(status_code=500, detail=f"Delete failed: {exc}")

    # drop the matching installed_packages row so list_apps stops showing it as installed.
    try:
        from digitorn.core.api.packages import _get_registry as _get_pkg_registry

        registry = _get_pkg_registry(request)
        if registry is not None:
            r_scope = result.get("scope") or scope or "system"
            r_owner = result.get("owner_user_id") or (
                caller_user_id if r_scope == "user" else None
            )
            await registry.delete(
                app_id, scope=r_scope, owner_user_id=r_owner,
            )
    except Exception as exc:  # noqa: BLE001 - non-fatal: deploy cleanup already won
        logger.warning(
            "registry cleanup failed during DELETE app=%s: %s",
            app_id, exc,
        )

    msg_tail = " (history preserved)" if not delete_history else ""
    actually = bool(result.get("actually_deleted", True))
    if not actually:
        # surface no-op deletes as a real 404 so HTTP-aware clients don't false-positive on response.ok.
        target_scope = result.get("scope") or scope or "user"
        raise HTTPException(
            status_code=404,
            detail={
                "error": "nothing_to_delete",
                "app_id": app_id,
                "scope": target_scope,
                "deleted": False,
                "deployed": False,
                "disk_removed": False,
                "secrets_deleted": 0,
                "db_removed": False,
                "message": (
                    f"Nothing to delete for '{app_id}' at scope "
                    f"'{target_scope}'. The app may be a built-in, "
                    f"installed under a different scope, or already "
                    f"removed."
                ),
            },
        )

    return AppResponse(
        success=True,
        data={
            "app_id": app_id,
            "scope": result.get("scope", "system"),
            "owner_user_id": result.get("owner_user_id", ""),
            "deleted": True,
            "deployed": result.get("deployed", False),
            "disk_removed": result.get("disk_removed", False),
            "secrets_deleted": result.get("secrets_deleted", 0),
            "db_removed": result.get("db_removed", False),
            "history_preserved": result.get("history_preserved", False),
            "message": (
                f"App '{app_id}' permanently deleted "
                f"({result.get('secrets_deleted', 0)} secret(s))" + msg_tail + "."
            ),
        },
    )


@router.post("/{app_id}/reload", response_model=AppResponse)
async def reload_app(request: Request, app_id: str) -> AppResponse:
    """Hot-reload a deployed app from its current bundle."""
    _require_permission(request, "apps:deploy")
    _validate_id(app_id)
    manager = _get_manager(request)

    # Guard: built-in apps are rebuilt by _deploy_builtin_apps at boot.
    deployed = _get_deployed(request, app_id)
    if deployed is not None and getattr(deployed, "builtin", False):
        raise HTTPException(
            status_code=403,
            detail=(
                f"Cannot hot-reload built-in app '{app_id}'. "
                f"Restart the daemon to pick up changes."
            ),
        )

    try:
        result = await manager.reload_app(app_id)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"App '{app_id}' not found")
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        logger.error("reload_app_failed app=%s: %s", app_id, exc, exc_info=True)
        raise HTTPException(
            status_code=500,
            detail=f"Reload failed: {exc}",
        )

    return AppResponse(
        success=True,
        data={
            **result,
            "message": (
                f"App '{app_id}' reloaded with "
                f"{result.get('secrets_applied', 0)} secret(s) applied."
            ),
        },
    )


@router.post("/{app_id}/run", response_model=AppResponse)
async def run_app(request: Request, app_id: str, body: RunRequest) -> AppResponse:
    """Run a deployed one-shot app."""
    _validate_id(app_id)
    manager = _get_manager(request)

    await _inc_agent_turns(request)
    try:
        result = await manager.run_one_shot(app_id, body.input)
        return AppResponse(
            success=result.error is None,
            data={
                "content": result.content,
                "tool_calls_count": result.tool_calls_count,
                "turns_used": result.turns_used,
                "truncated": result.truncated,
            },
            error=result.error,
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    finally:
        await _inc_agent_turns(request, -1)


@router.post("/{app_id}/pipeline", response_model=AppResponse)
async def run_pipeline(request: Request, app_id: str, body: PipelineRequest) -> AppResponse:
    """Execute a pipeline of app calls."""
    _validate_id(app_id)
    manager = _get_manager(request)

    if not _is_deployed(request, app_id):
        _raise_not_deployed(request, app_id)

    deployed = _get_deployed(request, app_id)
    raw_steps = body.steps
    if not raw_steps and deployed and hasattr(deployed, "compiled"):
        raw_steps = getattr(deployed.compiled, "pipeline", [])

    if not raw_steps:
        raise HTTPException(status_code=400, detail="No pipeline steps defined")

    from digitorn.core.pipeline import compile_pipeline, execute_pipeline

    try:
        steps = compile_pipeline(raw_steps)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    result = await execute_pipeline(steps, body.input)

    return AppResponse(
        success=result.success,
        data={
            "final_output": result.final_output,
            "steps": [
                {
                    "app_id": s.app_id,
                    "success": s.success,
                    "output": s.output[:500],
                    "duration": round(s.duration, 2),
                    "error": s.error,
                }
                for s in result.steps
            ],
            "total_duration": round(result.total_duration, 2),
        },
        error=result.error or None,
    )

