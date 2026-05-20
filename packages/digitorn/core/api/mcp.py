"""MCP management REST API - daemon-level server administration."""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from digitorn.core.database import get_session

router = APIRouter(prefix="/api/mcp", tags=["mcp"])


_MCP_ADMIN_PERMS = frozenset({
    "*",
    "admin",
    "mcp.admin",
    "mcp:admin",
    "mcp:install",
})


def _require_mcp_admin(request: Request) -> None:
    """Block non-admin callers from mutating MCP server state."""
    perms = getattr(request.state, "permissions", []) or []
    if any(p in _MCP_ADMIN_PERMS for p in perms):
        return
    raise HTTPException(
        status_code=403,
        detail=(
            "MCP server management is admin-only. Regular users "
            "can only read the server list and attach credentials."
        ),
    )


class InstallRequest(BaseModel):
    server_id: str
    config: dict[str, Any] = {}


class ConfigUpdateRequest(BaseModel):
    config: dict[str, Any]


class ServerResponse(BaseModel):
    server_id: str
    display_name: str
    description: str | None = None
    source: str
    transport: str
    status: str
    tools_count: int
    health_ok: bool
    auto_start: bool
    runtime: str
    package: str | None = None

    class Config:
        from_attributes = True


_CATEGORY_ICON_FALLBACK: dict[str, tuple[str, str]] = {
    # id-substring → (category, emoji)
    "github":     ("developer-tools", "🐙"),
    "gitlab":     ("developer-tools", "🦊"),
    "linear":     ("productivity", "📐"),
    "jira":       ("productivity", "📊"),
    "atlassian":  ("productivity", "📊"),
    "clickup":    ("productivity", "✅"),
    "notion":     ("productivity", "📝"),
    "slack":      ("communication", "💬"),
    "discord":    ("communication", "🎮"),
    "telegram":   ("communication", "✈️"),
    "gmail":      ("communication", "📧"),
    "email":      ("communication", "📧"),
    "google-drive":  ("productivity", "📁"),
    "gdrive":        ("productivity", "📁"),
    "google-calendar": ("productivity", "📅"),
    "gcal":       ("productivity", "📅"),
    "google-maps": ("data", "🗺"),
    "maps":       ("data", "🗺"),
    "brave":      ("data", "🔎"),
    "search":     ("data", "🔎"),
    "stripe":     ("finance", "💳"),
    "paypal":     ("finance", "💰"),
    "shopify":    ("finance", "🛍"),
    "mailgun":    ("communication", "📮"),
    "aws":        ("developer-tools", "☁️"),
    "twilio":     ("communication", "📞"),
    "sentry":     ("developer-tools", "🚨"),
    "postgres":   ("data", "🐘"),
    "mysql":      ("data", "🗄"),
    "mongodb":    ("data", "🍃"),
    "redis":      ("data", "🔴"),
    "apify":      ("data", "🕷"),
    "filesystem": ("developer-tools", "📂"),
    "memory":     ("ai", "🧠"),
    "sqlite":     ("data", "💾"),
    "everart":    ("creative", "🎨"),
    "puppeteer":  ("developer-tools", "🎭"),
}


def _catalog_category_and_icon(
    server_id: str, explicit_category: str, explicit_icon: str,
) -> tuple[str, str]:
    """Resolve the (category, icon) pair for a catalog entry."""
    cat = (explicit_category or "").strip()
    ico = (explicit_icon or "").strip()
    if not cat or not ico:
        sid = server_id.lower()
        for key, (fallback_cat, fallback_ico) in _CATEGORY_ICON_FALLBACK.items():
            if key in sid:
                if not cat:
                    cat = fallback_cat
                if not ico:
                    ico = fallback_ico
                break
    if not cat:
        cat = "other"
    if not ico:
        ico = "🧩"
    return cat, ico


@router.get("/catalog")
async def list_catalog_entries(category: str | None = None) -> dict[str, Any]:
    """List every entry in the static MCP catalog."""
    from digitorn.modules.mcp.catalog import all_catalog_entries

    entries: list[dict[str, Any]] = []
    for sid, entry in all_catalog_entries().items():
        cat, ico = _catalog_category_and_icon(
            sid,
            getattr(entry, "category", "") or "",
            getattr(entry, "icon", "") or "",
        )
        row = {
            "server_id": sid,
            "display_name": entry.display_name,
            "description": entry.description,
            "transport": entry.transport,
            "runtime": entry.runtime,
            "package": entry.package,
            "oauth_provider": entry.oauth_provider,
            "oauth_scopes": list(entry.oauth_scopes or ()),
            "required_fields": sorted((entry.env_mapping or {}).keys()),
            "has_oauth": bool(entry.oauth_provider),
            "icon": ico,
            "category": cat,
        }
        if category is not None and cat != category:
            continue
        entries.append(row)

    entries.sort(key=lambda r: r["display_name"].lower())
    return {"entries": entries, "count": len(entries)}


@router.get("/catalog/{server_id}")
async def get_catalog_entry_route(server_id: str) -> dict[str, Any]:
    """Return the full static CatalogEntry for one server_id."""
    from digitorn.modules.mcp.catalog import get_catalog_entry

    entry = get_catalog_entry(server_id)
    if entry is None:
        raise HTTPException(
            status_code=404,
            detail=f"Server '{server_id}' not in static catalog",
        )
    cat, ico = _catalog_category_and_icon(
        server_id,
        getattr(entry, "category", "") or "",
        getattr(entry, "icon", "") or "",
    )
    return {
        "server_id": entry.server_id,
        "display_name": entry.display_name,
        "description": entry.description,
        "transport": entry.transport,
        "command": entry.command,
        "args": list(entry.args or ()),
        "runtime": entry.runtime,
        "package": entry.package,
        "env_mapping": dict(entry.env_mapping or {}),
        "key_descriptions": dict(entry.key_descriptions or {}),
        "default_env": dict(entry.default_env or {}),
        "oauth_provider": entry.oauth_provider,
        "oauth_env_token_var": entry.oauth_env_token_var,
        "oauth_scopes": list(entry.oauth_scopes or ()),
        "oauth_style": entry.oauth_style,
        "oauth_keyfile_env": entry.oauth_keyfile_env,
        "oauth_credentials_env": entry.oauth_credentials_env,
        "oauth_credentials_filename": entry.oauth_credentials_filename,
        "binary_name": entry.binary_name,
        "smithery_slug": entry.smithery_slug,
        "timeout": entry.timeout,
        "has_oauth": bool(entry.oauth_provider),
        "required_fields": sorted((entry.env_mapping or {}).keys()),
        "icon": ico,
        "category": cat,
    }


@router.get("/search")
async def search_servers(q: str) -> dict[str, Any]:
    """Search for MCP servers in catalog + registry."""
    from digitorn.core.mcp_store import search_servers as do_search
    results = await do_search(q)
    return {"query": q, "results": results, "count": len(results)}


@router.get("/requirements/{server_id}")
async def get_requirements(server_id: str) -> dict[str, Any]:
    """Return install requirements for any server (catalog OR registry)."""
    from digitorn.modules.mcp.catalog import (
        get_server_requirements_async,
        get_catalog_entry,
    )

    try:
        req = await get_server_requirements_async(server_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))

    extra: dict[str, Any] = {}
    entry = get_catalog_entry(server_id)
    if entry is not None:
        cat, ico = _catalog_category_and_icon(
            server_id,
            getattr(entry, "category", "") or "",
            getattr(entry, "icon", "") or "",
        )
        extra = {
            "icon": ico,
            "category": cat,
            "oauth_scopes": list(entry.oauth_scopes or ()),
            "oauth_env_token_var": entry.oauth_env_token_var,
            "oauth_style": entry.oauth_style,
            "smithery_slug": entry.smithery_slug,
            "default_env": dict(entry.default_env or {}),
            "key_descriptions": dict(entry.key_descriptions or {}),
        }

    return {
        "server_id": req.server_id,
        "display_name": req.display_name,
        "description": req.description,
        "source": req.source,
        "transport": req.transport,
        "runtime": req.runtime,
        "package": req.package,
        "credentials": [
            {
                "key": c.key,
                "env_var": c.env_var,
                "description": c.description,
                "required": c.required,
                "is_arg": c.is_arg,
            }
            for c in req.credentials
        ],
        "oauth": req.oauth,
        "oauth_provider": req.oauth_provider,
        "install_hint": req.install_hint,
        "yaml_example": req.yaml_example,
        **extra,
    }


@router.get("/registry/browse")
async def browse_registry(
    q: str = "",
    cursor: str | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    """Paginated browse of the official MCP registry (~800 servers)."""
    from digitorn.modules.mcp.catalog import list_registry_servers
    return await list_registry_servers(
        query=q or "", cursor=cursor, limit=limit,
    )


@router.post("/registry/refresh")
async def refresh_registry(request: Request) -> dict[str, Any]:
    """Flush the registry cache. **Admin-only.**"""
    _require_mcp_admin(request)
    from digitorn.modules.mcp.catalog import clear_registry_cache
    cleared = clear_registry_cache()
    return {"cleared": cleared, "ok": True}


@router.post("/servers", status_code=201)
async def install_server(
    body: InstallRequest,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Install an MCP server into the daemon. **Admin-only.**"""
    _require_mcp_admin(request)
    from digitorn.core.mcp_store import install_server as do_install
    try:
        credential_store = getattr(request.app.state, "credential_store", None)
        server = await do_install(
            session, body.server_id, body.config,
            credential_store=credential_store,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {
        "server_id": server.server_id,
        "display_name": server.display_name,
        "source": server.source,
        "transport": server.transport,
        "status": server.status,
    }


@router.delete("/servers/{server_id}")
async def remove_server(
    server_id: str,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> dict[str, str]:
    """Remove an installed MCP server. **Admin-only.**"""
    _require_mcp_admin(request)
    from digitorn.core.mcp_store import remove_server as do_remove

    mcp_pool = getattr(request.app.state, "mcp_pool", None)
    if mcp_pool is not None:
        await mcp_pool.disconnect_server(server_id)

    try:
        await do_remove(session, server_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return {"status": "removed", "server_id": server_id}


@router.get("/available")
async def list_available_references(
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """List MCP server IDs that an app can reference by name in YAML."""
    from digitorn.core.mcp_store import list_servers as do_list

    servers = await do_list(session, status="ready")
    return {
        "available": [
            {
                "server_id": s.server_id,
                "display_name": s.display_name,
                "description": s.description,
                "transport": s.transport,
                "tools_count": s.tools_count,
                "source": s.source,
            }
            for s in servers
        ],
        "count": len(servers),
    }


@router.get("/servers")
async def list_servers(
    status: str | None = None,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """List all installed MCP servers."""
    from digitorn.core.mcp_store import list_servers as do_list
    servers = await do_list(session, status=status)
    return {
        "servers": [
            {
                "server_id": s.server_id,
                "display_name": s.display_name,
                "description": s.description,
                "source": s.source,
                "transport": s.transport,
                "status": s.status,
                "tools_count": s.tools_count,
                "health_ok": s.health_ok,
                "auto_start": s.auto_start,
                "runtime": s.runtime,
                "package": s.package,
            }
            for s in servers
        ],
        "count": len(servers),
    }


@router.get("/servers/{server_id}")
async def get_server(
    server_id: str,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Get detailed info about an installed MCP server."""
    from digitorn.core.mcp_store import get_server as do_get
    server = await do_get(session, server_id)
    if server is None:
        raise HTTPException(status_code=404, detail=f"Server '{server_id}' not found")
    return {
        "server_id": server.server_id,
        "display_name": server.display_name,
        "description": server.description,
        "source": server.source,
        "transport": server.transport,
        "command": server.command,
        "args": server.args,
        "url": server.url,
        "runtime": server.runtime,
        "package": server.package,
        "status": server.status,
        "status_message": server.status_message,
        "tools_count": server.tools_count,
        "tools": server.tools_schema,
        "health_ok": server.health_ok,
        "last_health_check": str(server.last_health_check) if server.last_health_check else None,
        "timeout": server.timeout,
        "rate_limit_rpm": server.rate_limit_rpm,
        "auto_start": server.auto_start,
        "installed_at": str(server.installed_at),
    }


@router.post("/servers/{server_id}/test")
async def test_server(
    server_id: str,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Test an installed server. **Admin-only.**"""
    _require_mcp_admin(request)
    from digitorn.core.mcp_store import test_server as do_test
    try:
        return await do_test(session, server_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@router.put("/servers/{server_id}/config")
async def update_config(
    server_id: str,
    body: ConfigUpdateRequest,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> dict[str, str]:
    """Update a server's runtime config. **Admin-only.**"""
    _require_mcp_admin(request)
    from digitorn.core.mcp_store import update_server_config
    try:
        await update_server_config(session, server_id, body.config)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))

    mcp_pool = getattr(request.app.state, "mcp_pool", None)
    if mcp_pool is not None:
        from digitorn.core.mcp_pool import MCPServerEvent
        await mcp_pool._emit(MCPServerEvent.CONFIG_UPDATED, server_id)

    return {"status": "updated", "server_id": server_id}


@router.get("/pool")
async def pool_status(request: Request) -> dict[str, Any]:
    """Show the live daemon MCP pool status."""
    mcp_pool = getattr(request.app.state, "mcp_pool", None)
    if mcp_pool is None:
        return {"servers": [], "message": "Daemon MCP pool not initialized"}
    return {"servers": mcp_pool.list_connected()}


@router.post("/pool/{server_id}/connect")
async def pool_connect(
    server_id: str,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Manually connect a server. **Admin-only.**"""
    _require_mcp_admin(request)
    from digitorn.core.mcp_store import get_server as do_get, _build_connect_kwargs

    mcp_pool = getattr(request.app.state, "mcp_pool", None)
    if mcp_pool is None:
        raise HTTPException(status_code=503, detail="Daemon pool not initialized")

    server = await do_get(session, server_id)
    if server is None:
        raise HTTPException(status_code=404, detail=f"Server '{server_id}' not installed")

    try:
        kwargs = _build_connect_kwargs(server)
        entry = await mcp_pool.connect_server(server_id, server.transport, **kwargs)
        return {
            "server_id": server_id,
            "status": entry.status,
            "tools_count": len(entry.tools),
        }
    except Exception as exc:
        from digitorn.modules.mcp.transports import MCPTransportError

        if isinstance(exc, MCPTransportError):
            logger.warning(
                "mcp_pool_connect_transport_error server=%s code=%s msg=%s",
                server_id, exc.code, exc,
            )
            if exc.code in (401, 403):
                raise HTTPException(
                    status_code=401,
                    detail={
                        "error": "authentication_required",
                        "message": str(exc),
                        "server_id": server_id,
                        "needs_oauth": True,
                        "retryable": False,
                    },
                )
            if exc.code == 404:
                raise HTTPException(
                    status_code=502,
                    detail={
                        "error": "endpoint_not_found",
                        "message": str(exc),
                        "server_id": server_id,
                        "retryable": False,
                    },
                )
            raise HTTPException(
                status_code=502,
                detail={
                    "error": "transport_error",
                    "message": str(exc),
                    "server_id": server_id,
                    "code": exc.code,
                    "retryable": exc.retryable,
                },
            )
        logger.exception("MCP pool connect failed server=%s: %s", server_id, exc)
        raise HTTPException(status_code=500, detail="Failed to connect MCP server.")


@router.post("/pool/{server_id}/disconnect")
async def pool_disconnect(
    server_id: str,
    request: Request,
) -> dict[str, str]:
    """Manually disconnect a server. **Admin-only.**"""
    _require_mcp_admin(request)
    mcp_pool = getattr(request.app.state, "mcp_pool", None)
    if mcp_pool is None:
        raise HTTPException(status_code=503, detail="Daemon pool not initialized")

    await mcp_pool.disconnect_server(server_id)
    return {"status": "disconnected", "server_id": server_id}


@router.get("/pool/health")
async def pool_health(request: Request) -> dict[str, Any]:
    """Run health check on all connected servers in the pool."""
    mcp_pool = getattr(request.app.state, "mcp_pool", None)
    if mcp_pool is None:
        return {
            "success": True,
            "data": {"results": {}, "pool_initialized": False},
        }
    results = await mcp_pool.health_check_all()
    return {
        "success": True,
        "data": {"results": results, "pool_initialized": True},
    }
