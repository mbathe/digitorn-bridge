"""Routes for the diag group, extracted from the legacy ``apps.py``.

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



@router.get("/{app_id}/diagnostics", response_model=AppResponse)
async def app_diagnostics(request: Request, app_id: str) -> AppResponse:
    """Run diagnostics checks for a deployed app."""
    import platform
    _validate_id(app_id)
    manager = _get_manager(request)

    checks: list[dict[str, Any]] = []

    # Daemon health
    checks.append({"name": "Daemon", "ok": True, "detail": "running"})

    # App deployed - use the manager's scoped `get()` (same resolver
    # the rest of the API uses) instead of a bare dict lookup on
    # `_deployed`. The dict lookup missed user-scoped deploys whose key
    # is `user:<uid>:<app_id>`, so `/api/apps` said "deployed" and
    # `/diagnostics` said "not deployed" for the same app.
    deployed = manager.get(app_id, user_id=_caller_user_id(request))
    if deployed is None:
        checks.append({"name": "App", "ok": False, "detail": "not deployed"})
        return AppResponse(success=True, data={"checks": checks})

    checks.append({"name": "App", "ok": True, "detail": deployed.compiled.meta.name})

    # Model
    entry = deployed.entry_context
    model = getattr(entry.provider, "model", "?")
    checks.append({"name": "Model", "ok": bool(model and model != "?"), "detail": model})

    # Modules
    mod_names = list(deployed.modules.keys())
    checks.append({"name": "Modules", "ok": len(mod_names) > 0, "detail": f"{len(mod_names)} loaded"})

    # Tools
    total_tools = deployed.index.total_tools if deployed.index else 0
    checks.append({"name": "Tools", "ok": total_tools > 0, "detail": f"{total_tools} available"})

    # Platform
    checks.append({"name": "Platform", "ok": True, "detail": f"{platform.system()} {platform.release()}"})

    # Git Bash (Windows)
    if platform.system() == "Windows":
        try:
            import asyncio as _asyncio
            import subprocess
            # Off-loop: ``git --version`` is fast (50ms) but goes through
            # PATH lookup on Windows, which can stall on a slow PATH.
            def _run() -> subprocess.CompletedProcess:
                return subprocess.run(
                    ["git", "--version"],
                    capture_output=True, text=True, timeout=3,
                )
            r = await _asyncio.to_thread(_run)
            checks.append({"name": "Git", "ok": r.returncode == 0, "detail": r.stdout.strip()})
        except Exception as e:
            checks.append({"name": "Git", "ok": False, "detail": str(e)[:50]})

    # MCP servers
    mcp = deployed.modules.get("mcp")
    if mcp and hasattr(mcp, "_connections"):
        connected = sum(1 for c in mcp._connections.values()
                        if getattr(c, "status", "") == "connected")
        total = len(mcp._connections)
        checks.append({"name": "MCP", "ok": connected == total,
                        "detail": f"{connected}/{total} connected"})

    return AppResponse(success=True, data={"checks": checks})


@router.get("/{app_id}/errors", response_model=AppResponse)
async def app_errors(request: Request, app_id: str, limit: int = 10) -> AppResponse:
    """Get recent failed activations with error details."""
    _validate_id(app_id)
    store = _get_activation_store(request)
    errors = await store.recent_errors(app_id, limit=min(limit, 50))
    return AppResponse(success=True, data={"errors": errors, "count": len(errors)})


@router.get("/{app_id}/status", response_model=AppResponse)
async def app_status(request: Request, app_id: str) -> AppResponse:
    """Hero-stats endpoint for the Flutter background app dashboard.

    One round-trip returns everything the top of the dashboard needs:

    - ``live``          → current run state (``running`` / ``idle``) +
                          number of activations in status='running'
    - ``stats``         → all-time aggregated stats (same as
                          ``/activations/stats``) so the UI doesn't have
                          to chain two requests on page load
    - ``hourly``        → the 24-hour sparkline bucket list, one row per
                          hour, oldest first, including empty hours
    - ``trend_24h``     → total runs + failed runs in the last 24 h
                          (convenience aggregate on top of ``hourly``)
    - ``triggers_summary`` → light summary of trigger + channel state so
                          the dashboard header can show "2 triggers · 3
                          channels active" without a second call to
                          ``/triggers``

    This is what the dashboard should call at page load and whenever
    the user hits the ↻ refresh button. Everything else (activation
    list, trigger details, channel details) is lazy-loaded on demand.
    """
    _validate_id(app_id)
    manager = _get_manager(request)
    store = _get_activation_store(request)

    deployed = _get_deployed(request, app_id)
    if deployed is None:
        _raise_not_deployed(request, app_id)

    # ── Live state (cheap: one COUNT per status group) ─────────────
    counts = await store.count_by_status(app_id)
    running_count = counts.get("running", 0)
    live_state = "running" if running_count > 0 else "idle"

    # ── All-time stats ─────────────────────────────────────────────
    stats = await store.stats(app_id)

    # ── 24h hourly buckets for the sparkline ───────────────────────
    hourly = await store.hourly_buckets(app_id, hours=24)
    trend_total = sum(b["total"] for b in hourly)
    trend_failed = sum(b["failed"] for b in hourly)
    trend_completed = sum(b["completed"] for b in hourly)

    # ── Triggers + channels summary ────────────────────────────────
    # Apps define triggers / channels in one of two styles:
    #
    #   1. Legacy ``execution.triggers: [...]`` - shown as compiled.execution.triggers
    #   2. Module-based ``modules.channels.config.providers: {...}`` - shown as
    #      live instances on the channels module itself (deployed.modules['channels']._providers)
    #
    # The dashboard needs ONE unified count regardless of which style the
    # user picked. We aggregate both sources here so the frontend sees
    # the same number for both.
    compiled = deployed.compiled
    legacy_triggers = compiled.execution.triggers or []

    # Count providers from the live channels module if it's loaded
    channels_mod = deployed.modules.get("channels")
    channel_providers: dict[str, Any] = {}
    if channels_mod is not None:
        channel_providers = getattr(channels_mod, "_providers", {}) or {}

    # Aggregate types across both styles
    all_types: set[str] = set()
    for t in legacy_triggers:
        if hasattr(t, "type") and t.type:
            all_types.add(t.type)
    for name, prov in channel_providers.items():
        adapter_name = getattr(getattr(prov, "adapter", None), "ADAPTER_ID", None) or (
            getattr(prov, "config", None) and getattr(prov.config, "adapter", None)
        )
        if adapter_name:
            all_types.add(str(adapter_name))

    triggers_summary = {
        "count": len(legacy_triggers) + len(channel_providers),
        "types": sorted(all_types),
    }

    # Channel names include both legacy compiled.channels (top-level
    # channels section) AND active channel provider instances from the
    # channels module (the new style).
    channel_names: set[str] = set((compiled.channels or {}).keys())
    channel_names.update(channel_providers.keys())

    channels_summary = {
        "count": len(channel_names),
        "names": sorted(channel_names),
    }

    return AppResponse(
        success=True,
        data={
            "app_id": app_id,
            "mode": compiled.execution.mode,
            "is_background": compiled.execution.mode == "background",
            "live": {
                "state": live_state,
                "running_count": running_count,
                "status_counts": counts,
            },
            "stats": stats,
            "hourly": hourly,
            "trend_24h": {
                "total": trend_total,
                "completed": trend_completed,
                "failed": trend_failed,
                "success_rate": round(
                    trend_completed / max(trend_total, 1) * 100, 1,
                ),
            },
            "triggers_summary": triggers_summary,
            "channels_summary": channels_summary,
        },
    )


@router.get("/{app_id}/ui-config", response_model=AppResponse)
async def get_app_ui_config(request: Request, app_id: str) -> AppResponse:
    """Return ONLY the client-UI-relevant config flags for an app.

    Safe to call from any authenticated user - it strictly allow-lists
    fields that are safe to expose to a frontend (booleans, render
    modes, layout hints). Never leaks prompts, secrets, api_keys,
    webhook URLs, hook logic, or capability grants.

    Rationale: the Flutter / web client needs to adapt its UI based on
    per-app config (``auto_approve`` → hide approve buttons;
    ``render_mode`` → canvas vs iframe; ``preview.enabled`` → show
    web preview pane). Previous proposal was to return the full YAML
    via ``?include_yaml=true`` - that was a leak (system_prompts,
    inline api_keys, internal webhook paths). This endpoint exposes
    only the narrow subset the UI cares about.
    """
    _validate_id(app_id)
    deployed = _get_deployed(request, app_id)
    if deployed is None:
        _raise_not_deployed(request, app_id)

    compiled = getattr(deployed, "compiled", None)
    modules_cfg: dict[str, Any] = {}
    workspace_cfg: dict[str, Any] = {}
    preview_cfg: dict[str, Any] = {}

    # Allow-list fields per module. Adding a new field here is an
    # explicit decision - reject the temptation to dump everything.
    _WS_ALLOW = {"render_mode", "entry_file", "title", "sync_to_disk",
                 "lint", "auto_approve"}
    _PREVIEW_ALLOW = {"enabled", "port"}

    if compiled is not None:
        mods = getattr(compiled, "modules", {}) or {}
        ws_block = mods.get("workspace")
        if ws_block is not None:
            ws_cfg = getattr(ws_block, "config", {}) or {}
            if isinstance(ws_cfg, dict):
                workspace_cfg = {k: v for k, v in ws_cfg.items() if k in _WS_ALLOW}
        pv_block = mods.get("preview")
        if pv_block is not None:
            pv_cfg = getattr(pv_block, "config", {}) or {}
            if isinstance(pv_cfg, dict):
                preview_cfg = {k: v for k, v in pv_cfg.items() if k in _PREVIEW_ALLOW}

    # Top-level workspace: block (render_mode, entry_file, title) - same
    # shape as the summary's ``workspace`` field but filtered.
    top_ws = getattr(compiled, "workspace", None) if compiled is not None else None
    top_workspace = {}
    if top_ws is not None:
        for k in ("render_mode", "entry_file", "title"):
            v = getattr(top_ws, k, None)
            if v is not None:
                top_workspace[k] = v

    return AppResponse(success=True, data={
        "app_id": app_id,
        "workspace_config": workspace_cfg,
        "preview_config": preview_cfg,
        "workspace": top_workspace,
    })


@router.get("/{app_id}/files", response_model=AppResponse)
async def list_app_files(
    request: Request, app_id: str, subdir: str = "",
):
    """List files available in a deployed app's companion directory.

    Lets the Flutter client discover what assets / skills / prompts
    ship with an app without guessing filenames. ``subdir`` narrows
    the listing to a subdirectory (e.g. ``?subdir=skills`` or
    ``?subdir=assets``). Empty = root.

    Returns a shallow listing (one directory level) - call again
    with a subdir query to drill down.
    """
    from pathlib import Path
    _validate_id(app_id)
    manager = _get_manager(request)
    deployed = _get_deployed(request, app_id)
    if deployed is None:
        raise HTTPException(
            status_code=404, detail=f"App '{app_id}' not deployed",
        )

    bundle_dir = _resolve_app_bundle_dir(request, app_id, manager)
    if bundle_dir is None:
        pkg_registry = getattr(request.app.state, "package_registry", None)
        if pkg_registry is not None:
            try:
                pkg = await pkg_registry.get(app_id)
                if pkg and pkg.get("install_dir"):
                    bundle_dir = Path(pkg["install_dir"]).resolve()
            except Exception:
                pass
    if bundle_dir is None or not bundle_dir.is_dir():
        raise HTTPException(
            status_code=404, detail="App bundle dir not found",
        )

    if subdir.startswith(".digitorn") or "/.digitorn/" in subdir:
        raise HTTPException(
            status_code=403, detail="Access to daemon-managed files denied",
        )

    target_dir = (bundle_dir / subdir).resolve() if subdir else bundle_dir
    try:
        target_dir.relative_to(bundle_dir)
    except ValueError:
        raise HTTPException(
            status_code=400, detail="subdir escapes app dir",
        )
    if not target_dir.is_dir():
        raise HTTPException(status_code=404, detail="Directory not found")

    entries: list[dict[str, Any]] = []
    for child in sorted(target_dir.iterdir(), key=lambda p: (p.is_file(), p.name.lower())):
        name = child.name
        if name.startswith(".digitorn") or name == ".digitorn":
            continue
        rel = child.relative_to(bundle_dir).as_posix()
        entry: dict[str, Any] = {
            "name": name,
            "path": rel,
            "type": "directory" if child.is_dir() else "file",
        }
        if child.is_file():
            try:
                stat = child.stat()
                entry["size"] = stat.st_size
                # Hint the asset URL the client should use
                entry["url"] = f"/api/apps/{app_id}/assets/{rel}"
            except Exception:
                pass
        entries.append(entry)

    return AppResponse(
        success=True,
        data={
            "app_id": app_id,
            "subdir": subdir,
            "entries": entries,
            "count": len(entries),
        },
    )


@router.get("/{app_id}/icon")
async def get_app_icon(request: Request, app_id: str):
    """Serve a deployed app's icon.

    Two icon styles are accepted in the YAML's ``app.icon`` field:

    - **Emoji / short text** (e.g. ``"⚛️"``, ``"💬"``, ``"AI"``) —
      every Digitorn builtin uses this. The route renders it as a
      tiny SVG so the route always returns a real image, regardless
      of what the YAML declared.
    - **Path to a file** (e.g. ``"icons/app.png"``) — relative to
      the app bundle dir. Streamed with ``FileResponse``.

    The discriminator is path-shape: a value containing ``/`` or
    ``\\`` is treated as a path; everything else is rendered as an
    emoji SVG. This means an app that declares ``icon: "⚛️"`` no
    longer 404s on every page load.

    Prefer this over ``/api/packages/{id}/icon`` for deployed apps —
    they're the same source but this endpoint doesn't require the
    app to also be installed as a package.
    """
    from pathlib import Path
    from fastapi.responses import FileResponse, Response

    _validate_id(app_id)
    manager = _get_manager(request)
    deployed = _get_deployed(request, app_id)
    if deployed is None:
        raise HTTPException(
            status_code=404, detail=f"App '{app_id}' not deployed",
        )
    meta = deployed.compiled.meta
    icon_rel = (getattr(meta, "icon", "") or "").strip()
    if not icon_rel:
        raise HTTPException(
            status_code=404, detail="App has no icon declared",
        )

    # Path-style icon → stream the file. Anything without a path
    # separator is treated as inline text/emoji and rendered as SVG.
    if "/" not in icon_rel and "\\" not in icon_rel:
        return _render_icon_text_as_svg(icon_rel)

    bundle_dir: Path | None = None
    try:
        bs = getattr(manager, "_bundle_store", None)
        if bs is not None:
            _d = bs.app_dir(app_id)
            if _d:
                bundle_dir = Path(_d).resolve()
    except Exception:
        bundle_dir = None

    if bundle_dir is None or not bundle_dir.is_dir():
        pkg_registry = getattr(request.app.state, "package_registry", None)
        if pkg_registry is not None:
            try:
                pkg = await pkg_registry.get(app_id)
                if pkg and pkg.get("install_dir"):
                    bundle_dir = Path(pkg["install_dir"]).resolve()
            except Exception:
                pass

    if bundle_dir is None or not bundle_dir.is_dir():
        raise HTTPException(
            status_code=404, detail="App bundle dir not found",
        )

    icon_path = (bundle_dir / icon_rel).resolve()
    try:
        icon_path.relative_to(bundle_dir)
    except ValueError:
        raise HTTPException(
            status_code=400, detail="Icon path escapes app dir",
        )
    if not icon_path.is_file():
        raise HTTPException(status_code=404, detail="Icon file not found")

    return FileResponse(str(icon_path))


def _render_icon_text_as_svg(text: str):
    """Wrap a short text/emoji icon in a 64×64 SVG.

    The text is XML-escaped so a malicious YAML payload can't inject
    SVG markup. Cached for a day — the YAML field doesn't change at
    runtime.
    """
    from xml.sax.saxutils import escape
    from fastapi.responses import Response

    safe = escape(text)
    svg = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64" '
        'width="64" height="64">'
        f'<text x="50%" y="54%" dominant-baseline="middle" '
        f'text-anchor="middle" font-size="44" '
        f'font-family="-apple-system, Segoe UI Emoji, Apple Color Emoji, '
        f'Noto Color Emoji, system-ui, sans-serif">{safe}</text>'
        '</svg>'
    )
    return Response(
        content=svg.encode("utf-8"),
        media_type="image/svg+xml; charset=utf-8",
        headers={"Cache-Control": "public, max-age=86400, immutable"},
    )


@router.get("/{app_id}/index", response_model=AppResponse)
async def get_app_index(request: Request, app_id: str) -> AppResponse:
    """Get full tool index structure for the app.

    Returns all categories, tools, aliases, and metadata.
    Useful for SDK clients to build local caches or UI.
    """
    _validate_id(app_id)
    manager = _get_manager(request)
    if not _is_deployed(request, app_id):
        _raise_not_deployed(request, app_id)

    deployed = _get_deployed(request, app_id)
    if deployed is None or deployed.index is None:
        return AppResponse(success=True, data={"categories": [], "total_tools": 0})

    idx = deployed.index
    categories = []
    for cat_name in sorted(idx.categories.keys()):
        cat_info = idx.categories[cat_name]
        tools_in_cat = []
        for tool_entry in idx.tools.values():
            if tool_entry.module_id == cat_name:
                tools_in_cat.append({
                    "name": tool_entry.fqn,
                    "description": tool_entry.description,
                    "aliases": tool_entry.aliases,
                    "tags": tool_entry.tags,
                    "side_effects": tool_entry.side_effects,
                    "risk_level": tool_entry.risk_level,
                    "params_schema": tool_entry.params_schema,
                })
        categories.append({
            "name": cat_name,
            "description": cat_info.description if hasattr(cat_info, "description") else "",
            "tool_count": len(tools_in_cat),
            "tools": tools_in_cat,
        })

    return AppResponse(success=True, data={
        "total_tools": idx.total_tools,
        "total_categories": idx.total_categories,
        "tool_injection_mode": deployed.entry_context.tool_injection,
        "categories": categories,
    })


@router.get("/{app_id}/assets/{asset_path:path}")
async def get_app_asset(
    request: Request, app_id: str, asset_path: str, size: int = 0,
):
    """Serve any file from a deployed app's companion directory.

    Covers README.md, CHANGELOG.md, LICENSE, skills/*.md,
    assets/*, workspace defaults - anything the YAML references
    via a relative path. Guarded against path traversal; denies
    ``.digitorn/*`` (daemon-managed area).

    **``?size=N``** - when Pillow is installed and the asset is
    a raster image (PNG/JPG/WebP), serve a resized variant of N
    pixels on the longest side. Results are cached on disk under
    ``.digitorn/resized/`` so repeated requests don't re-encode.
    When Pillow isn't installed or the asset isn't an image,
    ``size`` is ignored and the original is served.

    Use this route over ``/api/packages/{id}/assets/...`` for
    deployed apps - it doesn't require the app to also be
    installed as a package.
    """
    from pathlib import Path
    from fastapi.responses import FileResponse

    _validate_id(app_id)
    manager = _get_manager(request)
    deployed = _get_deployed(request, app_id)
    if deployed is None:
        raise HTTPException(
            status_code=404, detail=f"App '{app_id}' not deployed",
        )

    bundle_dir = _resolve_app_bundle_dir(request, app_id, manager)
    if bundle_dir is None:
        # Package registry fallback (async get)
        pkg_registry = getattr(request.app.state, "package_registry", None)
        if pkg_registry is not None:
            try:
                pkg = await pkg_registry.get(app_id)
                if pkg and pkg.get("install_dir"):
                    bundle_dir = Path(pkg["install_dir"]).resolve()
            except Exception:
                pass
    if bundle_dir is None or not bundle_dir.is_dir():
        raise HTTPException(
            status_code=404, detail="App bundle dir not found",
        )

    if asset_path.startswith(".digitorn") or "/.digitorn/" in asset_path:
        raise HTTPException(
            status_code=403, detail="Access to daemon-managed files denied",
        )

    # BUG-079: the raw ``app.yaml`` / ``meta.json`` / ``package.toml``
    # expose system_prompts, model config, constraints, and private
    # setup_steps that include secrets at runtime. They must not be
    # readable by any authenticated user - restrict to the owner of a
    # user-scope deploy or to admins for system-scope apps. The same
    # rule applies to any other ``.yaml`` / ``.toml`` config file
    # living at the bundle root.
    _norm_asset = asset_path.replace("\\", "/").lower()
    _restricted = (
        "app.yaml", "app.yml", "meta.json", "package.toml",
        "manifest.json", "manifest.yaml",
    )
    if _norm_asset in _restricted:
        perms = getattr(request.state, "permissions", []) or []
        is_admin = "*" in perms
        caller_uid = _caller_user_id(request)
        # Walk the _deployed index to find which scope this app lives
        # under. A system-scope app's sensitive files are admin-only;
        # a user-scope app's sensitive files are owner-only.
        owner_uid: str | None = None
        scope = "system"
        for key, dep in (manager._deployed or {}).items():
            if getattr(dep, "app_id", None) != app_id:
                continue
            if key.startswith("system:"):
                scope = "system"
                owner_uid = None
                break
            if key.startswith("user:"):
                parts = key.split(":", 2)
                if len(parts) >= 2:
                    owner_uid = parts[1]
                    scope = "user"
                break
        if scope == "system" and not is_admin:
            raise HTTPException(
                status_code=403,
                detail=(
                    "Access to a system-scope app's source manifest "
                    "requires admin permission."
                ),
            )
        if scope == "user" and owner_uid and caller_uid != owner_uid and not is_admin:
            raise HTTPException(
                status_code=403,
                detail=(
                    "Access to another user's app manifest is denied."
                ),
            )

    target = (bundle_dir / asset_path).resolve()
    try:
        target.relative_to(bundle_dir)
    except ValueError:
        raise HTTPException(
            status_code=400, detail="Asset path escapes app dir",
        )
    if not target.is_file():
        raise HTTPException(
            status_code=404, detail=f"Asset not found: {asset_path}",
        )

    # Resize support (Pillow optional).
    if size and size > 0:
        resized = _try_resize_image(bundle_dir, target, size)
        if resized is not None:
            return FileResponse(str(resized))

    return FileResponse(str(target))


@router.get("/{app_id}/channels/health", response_model=AppResponse)
async def channels_health(request: Request, app_id: str) -> AppResponse:
    """Return the live health of every channel registered for an app.

    Walks the ``ChannelRegistry`` of the deployed app, calls
    ``health_check()`` on each instance and returns a structured dict
    the Flutter dashboard can render as a status badge per channel.

    Response::

        {
          "success": true,
          "data": {
            "app_id": "newsletter-digest",
            "channel_count": 3,
            "channels": {
              "email": {
                "status": "ok",
                "latency_ms": 124.5,
                "last_error": null,
                "last_success_at": "2026-04-13T10:35:45Z",
                "deliveries_total": 12,
                "deliveries_failed": 0,
                "details": {"smtp_host": "smtp.sendgrid.net"}
              },
              "slack": {"status": "ok", ...},
              "failing_webhook": {
                "status": "down",
                "last_error": "HTTP 503 Service Unavailable",
                ...
              }
            }
          }
        }

    Status values: ``ok`` (happy path), ``degraded`` (working but
    flaky - retries needed), ``down`` (last attempt failed and the
    channel is considered unreachable). The dashboard maps each to a
    dot color.
    """
    _validate_id(app_id)
    manager = _get_manager(request)
    deployed = _get_deployed(request, app_id)
    if deployed is None:
        _raise_not_deployed(request, app_id)

    # ChannelRegistry is scoped to the manager. We only want instances
    # that belong to this app - the registry indexes them by name, and
    # names are already scoped by app at creation time (see
    # AppManager._build_and_deploy channel loop).
    registry = getattr(manager, "_channel_registry", None)
    if registry is None:
        return AppResponse(
            success=True,
            data={
                "app_id": app_id,
                "channel_count": 0,
                "channels": {},
                "note": "No channel registry on this manager.",
            },
        )

    # Resolve the set of channel names the app declares. Apps can declare
    # channels either at the top-level ``channels:`` block (older form,
    # lands in ``deployed.compiled.channels``) or inside the ``channels``
    # module config as ``modules.channels.config.providers`` (newer form
    # used by most builtins). The /triggers endpoint reads the latter -
    # we fall back to it when the top-level block is empty so the two
    # endpoints stay in agreement (BUG-051).
    app_channel_names: set[str] = set((deployed.compiled.channels or {}).keys())
    if not app_channel_names:
        channels_mod = deployed.modules.get("channels") if getattr(deployed, "modules", None) else None
        if channels_mod is not None:
            app_channel_names = set(getattr(channels_mod, "_providers", {}).keys())

    try:
        all_health = await registry.health_all()
    except Exception as exc:
        logger.warning("channels_health_all failed app=%s: %s", app_id, exc)
        return AppResponse(
            success=False,
            error=f"Failed to query channel health: {exc}",
        )

    channels: dict[str, dict[str, Any]] = {}
    for name, health in all_health.items():
        if name not in app_channel_names:
            continue
        channels[name] = {
            "status": health.status,
            "latency_ms": round(health.latency_ms, 1),
            "last_error": health.last_error,
            "last_success_at": health.last_success_at,
            "deliveries_total": health.deliveries_total,
            "deliveries_failed": health.deliveries_failed,
            "details": health.details or {},
        }

    # For channels the app declared but that aren't in the registry
    # (not yet started, or failed at startup), report them as "unknown"
    # so the dashboard shows something actionable instead of hiding them.
    for name in app_channel_names:
        if name not in channels:
            channels[name] = {
                "status": "unknown",
                "latency_ms": 0.0,
                "last_error": "Channel instance not found in registry",
                "last_success_at": None,
                "deliveries_total": 0,
                "deliveries_failed": 0,
                "details": {},
            }

    return AppResponse(
        success=True,
        data={
            "app_id": app_id,
            "channel_count": len(channels),
            "channels": channels,
        },
    )


@router.get("/{app_id}/deploy-status", response_model=AppResponse)
async def get_deploy_status(request: Request, app_id: str) -> AppResponse:
    """Return the last known deploy outcome for an app.

    BUG-080: POST ``/deploy`` used to return ``status:"deploying"``
    and silently drop the error if the background deploy failed - the
    client had no way to distinguish "still running" from "failed".
    This route surfaces the stored error (if any) so the caller can
    show a meaningful message.

    Shape::

        { "deployed": true, "app_id": "...", "error": null }
        { "deployed": false, "app_id": "...", "error": "...", "traceback": "..." }
    """
    _validate_id(app_id)
    manager = _get_manager(request)
    is_deployed_now = manager.is_deployed(
        app_id, user_id=_caller_user_id(request),
    )
    errors = getattr(manager, "_deploy_errors", {}) or {}
    err = errors.get(app_id)
    data: dict[str, Any] = {
        "app_id": app_id,
        "deployed": is_deployed_now,
        "error": err.get("error") if err else None,
    }
    if err:
        data["traceback"] = err.get("traceback", "")[:2000]
        data["failed_at"] = err.get("failed_at")
        data["yaml_path"] = err.get("yaml_path")
    return AppResponse(success=True, data=data)


@router.post("/{app_id}/notifications")
async def check_notifications(request: Request, app_id: str, body: NotificationCheckRequest):
    """Check for background task notifications and stream an agent response if any.

    Returns SSE stream identical to chat/stream if notifications exist,
    or an empty 204 response if nothing is pending.
    """
    _validate_id(app_id)
    manager = _get_manager(request)

    if not _is_deployed(request, app_id):
        _raise_not_deployed(request, app_id)

    if not manager.has_active_bg_tasks(app_id):
        deployed = _get_deployed(request, app_id)
        cb = deployed.context_builder if deployed else None
        if cb is None or not hasattr(cb, "drain_bg_notifications"):
            return AppResponse(success=True, data={"notifications": 0})
        pending = cb.drain_bg_notifications(session_id=body.session_id)
        if not pending:
            return AppResponse(success=True, data={"notifications": 0})
        # Re-queue into the session's queue
        session_queue = cb._get_notification_queue(body.session_id)
        for n in pending:
            session_queue.put_nowait(n)

    queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue(maxsize=500)

    async def on_tool_call(name: str, params: dict, result: Any, call_id: str = "") -> None:
        ok, err = True, ""
        if isinstance(result, dict):
            ok = result.get("success", True)
            err = result.get("error", "")
        elif hasattr(result, "success"):
            ok = result.success
            err = getattr(result, "error", "") or ""
        await queue.put({
            "event": "tool_call",
            "data": {"id": call_id, "name": name, "params": params, "success": ok, "error": err},
        })

    async def _run():
        try:
            result = await manager.check_notifications(
                app_id, body.session_id,
                on_tool_call=on_tool_call,
            )
            if result is None:
                await queue.put({
                    "event": "result",
                    "data": {"content": "", "notifications": 0},
                })
            else:
                await queue.put({
                    "event": "result",
                    "data": {
                        "content": result.content,
                        "session_id": body.session_id,
                        "notifications": 1,
                        "tool_calls_count": result.tool_calls_count,
                        "error": result.error,
                    },
                })
        except Exception as exc:
            await queue.put({
                "event": "error",
                "data": {"error": str(exc)},
            })
        await queue.put(None)

    async def event_generator():
        task = asyncio.create_task(_run())
        try:
            while True:
                try:
                    item = await asyncio.wait_for(queue.get(), timeout=300.0)
                except asyncio.TimeoutError:
                    yield "event: timeout\ndata: {}\n\n"
                    break
                if item is None:
                    break
                event = item["event"]
                data = _json.dumps(item["data"], ensure_ascii=False, default=str)
                yield f"event: {event}\ndata: {data}\n\n"
        finally:
            if not task.done():
                task.cancel()

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/{app_id}/payload-schema", response_model=AppResponse)
async def get_app_payload_schema(request: Request, app_id: str) -> AppResponse:
    """Return the declarative payload schema for a background app.

    The Flutter dashboard calls this once per app to render a typed
    form (instead of a generic key/value editor) and to know which
    fields/files are required before a session can be activated.

    Returns ``data: null`` when the app has no schema declared - the
    dashboard should fall back to the free-form editor in that case.
    """
    _validate_id(app_id)
    manager = _get_manager(request)
    deployed = _get_deployed(request, app_id)
    if not deployed:
        _raise_not_deployed(request, app_id)
    schema = getattr(deployed.compiled.execution, "payload_schema", None)
    return AppResponse(success=True, data=schema)


@router.get("/{app_id}/hooks", response_model=AppResponse)
async def get_app_hooks(request: Request, app_id: str) -> AppResponse:
    """List every hook declared by a deployed app.

    Surfaces both app-wide hooks (``runtime.hooks[]``, legacy
    ``execution.hooks[]``) and per-agent hooks (``agents[].hooks[]``)
    in a flat list. Each entry carries its ``scope`` (``app`` or
    ``agent:<id>``) so a UI can group them. Useful for the dashboard
    introspection panel and matches the documentation reference in
    ``HookConfig.tags``.
    """
    _validate_id(app_id)
    manager = _get_manager(request)
    deployed = _get_deployed(request, app_id)
    if not deployed:
        _raise_not_deployed(request, app_id)

    def _hook_to_dict(h: Any, scope: str) -> dict[str, Any]:
        cond = getattr(h, "condition", None)
        act = getattr(h, "action", None)
        return {
            "id": getattr(h, "id", ""),
            "scope": scope,
            "on": getattr(h, "on", "turn_end"),
            "condition_type": getattr(cond, "type", "always") if cond else "always",
            "action_type": getattr(act, "type", "") if act else "",
            "cooldown": getattr(h, "cooldown", 0.0),
            "max_fires": getattr(h, "max_fires", 0),
            "priority": getattr(h, "priority", 100),
            "enabled": getattr(h, "enabled", True),
            "tags": list(getattr(h, "tags", []) or []),
        }

    hooks: list[dict[str, Any]] = []
    runtime_block = getattr(deployed.compiled, "runtime", None)
    for h in (getattr(runtime_block, "hooks", None) or []):
        hooks.append(_hook_to_dict(h, "app"))
    exec_block = getattr(deployed.compiled, "execution", None)
    for h in (getattr(exec_block, "hooks", None) or []):
        hooks.append(_hook_to_dict(h, "app"))
    for agent in (getattr(deployed.compiled, "agents", None) or []):
        agent_id = getattr(agent, "id", "?")
        for h in (getattr(agent, "hooks", None) or []):
            hooks.append(_hook_to_dict(h, f"agent:{agent_id}"))

    return AppResponse(
        success=True,
        data={
            "app_id": app_id,
            "count": len(hooks),
            "hooks": hooks,
        },
    )

