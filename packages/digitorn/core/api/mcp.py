"""MCP management REST API - daemon-level server administration.

Endpoints for searching, installing, testing, configuring, and monitoring
MCP servers through the daemon. Powers both the CLI and future dashboard.

All endpoints under ``/api/mcp/``.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from digitorn.core.database import get_session

router = APIRouter(prefix="/api/mcp", tags=["mcp"])


def _require_mcp_admin(request: Request) -> None:
    """Block non-admin callers from mutating MCP server state.

    MCP servers are daemon-level resources (processes running
    inside the daemon), so installation and config management
    are reserved to administrators. Regular users can read the
    server list and catalog, and they can attach their own
    credentials via the unified credential store - but they
    can't install, remove, reconfigure, or connect/disconnect
    servers.
    """
    perms = getattr(request.state, "permissions", []) or []
    if "*" in perms or "admin" in perms or "mcp.admin" in perms:
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


# Category + icon fallback map for catalog entries whose fields
# are blank. Keyed by server_id substring. The Flutter client uses
# ``category`` to filter the catalog grid and ``icon`` to render
# each card when the entry has no explicit values.
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
    """Resolve the (category, icon) pair for a catalog entry.

    Explicit values win; when absent, the fallback substring map
    fills in sensible defaults based on the server_id. Final
    fallback when nothing matches: category="other", icon="🧩".
    """
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
    """List every entry in the static MCP catalog.

    This is what the Flutter Hub → MCP Servers tab calls to
    populate the "Browse" view without having to do a search.
    Returns a compact card-ready shape for each entry - enough
    to render a grid of installable servers.

    Use ``GET /api/mcp/catalog/{server_id}`` for the full
    CatalogEntry with ``env_mapping`` + ``key_descriptions`` so
    the client can render the install form fields.
    """
    from digitorn.modules.mcp.catalog import CATALOG

    entries: list[dict[str, Any]] = []
    for sid, entry in CATALOG.items():
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
    """Return the full static CatalogEntry for one server_id.

    This is the endpoint the install form uses. It returns every
    field the client needs to render the correct form:

    - ``env_mapping``: which logical field name maps to which
      env var the subprocess expects (e.g. ``token`` → ``GITHUB_PERSONAL_ACCESS_TOKEN``)
    - ``key_descriptions``: human-readable help text per field,
      shown as TextField helperText
    - ``oauth_provider`` + ``oauth_scopes``: when set, the
      server uses OAuth - the client should show a "Connect"
      button instead of a text field
    - ``default_env``: pre-filled env vars (e.g. Google Drive
      uses ``GOOGLE_APPLICATION_CREDENTIALS`` pointing at a
      credential file managed by the daemon)

    Returns 404 if ``server_id`` is not in the static catalog.
    For remote-registry entries the client should use the
    search result fields directly (they don't have env_mapping
    metadata - remote servers declare their config via their
    MCP manifest when connected).
    """
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
        server = await do_install(session, body.server_id, body.config)
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
    """Run health check on all connected servers in the pool.

    BUG-067: wrapped in the standard ``{success, data, error}``
    envelope so clients don't need special-case parsing for this one
    endpoint.
    """
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
