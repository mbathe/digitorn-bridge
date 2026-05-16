"""MCP featured catalog routes — public read + admin CRUD.

Public:

  * ``GET  /api/v1/mcp/featured`` — list (filterable, paginated)
  * ``GET  /api/v1/mcp/featured/{server_id}`` — detail

Admin (``require_admin`` dependency):

  * ``POST   /api/v1/mcp/featured`` — create
  * ``PATCH  /api/v1/mcp/featured/{server_id}`` — partial update
  * ``DELETE /api/v1/mcp/featured/{server_id}`` — delete
  * ``POST   /api/v1/mcp/featured/seed`` — bulk upsert from a JSON body

The Hub is the **source of truth** for the catalog. Each per-user
daemon proxies its ``/api/mcp/catalog*`` calls here through a 5 min
in-memory cache. A daemon redeploy is never required to add or remove
a server — only an admin curl call against these endpoints.

The daemon's baked-in ``catalog.py`` exists only as an offline fallback
(network partition, dev box without Hub access). The contract is that
every field returned by ``GET /featured`` round-trips into a working
install on the daemon side.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth.deps import require_admin
from ..db import get_session
from ..models import McpFeaturedEntry

router = APIRouter(prefix="/mcp/featured", tags=["mcp"])


# ── Schemas ─────────────────────────────────────────────────────


class McpFeaturedRow(BaseModel):
    """Public projection used by both list and detail endpoints.

    Shape mirrors the daemon's ``CatalogEntry`` dataclass so the proxy
    side can deserialize without any field-name remapping.
    """

    model_config = ConfigDict(from_attributes=True)

    server_id: str
    display_name: str
    description: str = ""
    icon: str = ""
    category: str = ""

    transport: str = "stdio"
    command: str = ""
    args: list[str] = Field(default_factory=list)
    runtime: str = "npm"
    package: str = ""
    url: str | None = None
    default_env: dict[str, str] = Field(default_factory=dict)

    env_mapping: dict[str, str] = Field(default_factory=dict)
    key_descriptions: dict[str, str] = Field(default_factory=dict)

    oauth_provider: str | None = None
    oauth_style: str = ""
    oauth_env_token_var: str = ""
    oauth_scopes: list[str] = Field(default_factory=list)
    oauth_keyfile_env: str = ""
    oauth_credentials_env: str = ""
    oauth_credentials_filename: str = ""

    binary_name: str = ""
    smithery_slug: str = ""
    timeout: float = 30.0

    featured_priority: int = 100
    hidden: bool = False
    verified_at: datetime | None = None
    verified_by: str | None = None
    last_tested_at: datetime | None = None
    last_tested_ok: bool = False
    last_test_error: str | None = None

    registry_server_id: str | None = None

    created_at: datetime | None = None
    updated_at: datetime | None = None


class McpFeaturedListResponse(BaseModel):
    entries: list[McpFeaturedRow]
    count: int


class McpFeaturedCreate(BaseModel):
    """Admin payload for ``POST /featured`` and ``POST /featured/seed`` items.

    All fields are optional except ``server_id`` + ``display_name``; defaults
    align with the SQLAlchemy model so a minimal entry is valid.
    """

    server_id: str
    display_name: str
    description: str = ""
    icon: str = ""
    category: str = ""

    transport: str = "stdio"
    command: str = ""
    args: list[str] = Field(default_factory=list)
    runtime: str = "npm"
    package: str = ""
    url: str | None = None
    default_env: dict[str, str] = Field(default_factory=dict)

    env_mapping: dict[str, str] = Field(default_factory=dict)
    key_descriptions: dict[str, str] = Field(default_factory=dict)

    oauth_provider: str | None = None
    oauth_style: str = ""
    oauth_env_token_var: str = ""
    oauth_scopes: list[str] = Field(default_factory=list)
    oauth_keyfile_env: str = ""
    oauth_credentials_env: str = ""
    oauth_credentials_filename: str = ""

    binary_name: str = ""
    smithery_slug: str = ""
    timeout: float = 30.0

    featured_priority: int = 100
    hidden: bool = False
    registry_server_id: str | None = None


class McpFeaturedPatch(BaseModel):
    """All fields optional. ``None`` = "leave alone"."""

    display_name: str | None = None
    description: str | None = None
    icon: str | None = None
    category: str | None = None

    transport: str | None = None
    command: str | None = None
    args: list[str] | None = None
    runtime: str | None = None
    package: str | None = None
    url: str | None = None
    default_env: dict[str, str] | None = None

    env_mapping: dict[str, str] | None = None
    key_descriptions: dict[str, str] | None = None

    oauth_provider: str | None = None
    oauth_style: str | None = None
    oauth_env_token_var: str | None = None
    oauth_scopes: list[str] | None = None
    oauth_keyfile_env: str | None = None
    oauth_credentials_env: str | None = None
    oauth_credentials_filename: str | None = None

    binary_name: str | None = None
    smithery_slug: str | None = None
    timeout: float | None = None

    featured_priority: int | None = None
    hidden: bool | None = None
    registry_server_id: str | None = None


class McpFeaturedSeedRequest(BaseModel):
    """Body for ``POST /featured/seed``.

    Accepts the exact shape produced by
    ``tools/export_mcp_catalog_seed.py`` — a dict with ``version`` and
    ``entries`` keys. Each entry is validated against ``McpFeaturedCreate``.
    Pass ``replace=True`` to wipe existing rows first (rare; default is
    upsert-only).
    """

    version: int = 1
    entries: list[McpFeaturedCreate]
    replace: bool = False


class McpFeaturedSeedResponse(BaseModel):
    inserted: int
    updated: int
    deleted: int = 0


# ── Routes — public read ────────────────────────────────────────


@router.get("", response_model=McpFeaturedListResponse)
@router.get("/", response_model=McpFeaturedListResponse, include_in_schema=False)
async def list_featured(
    category: Annotated[str | None, Query(max_length=80)] = None,
    runtime: Annotated[
        str | None, Query(pattern="^(npm|pip|uv|docker|remote|custom|none)$")
    ] = None,
    include_hidden: Annotated[bool, Query()] = False,
    limit: Annotated[int, Query(ge=1, le=500)] = 200,
    offset: Annotated[int, Query(ge=0, le=10_000)] = 0,
    session: AsyncSession = Depends(get_session),
) -> McpFeaturedListResponse:
    """Return the curated catalog, sorted by ``featured_priority`` then ``server_id``.

    By default hidden entries are excluded; pass ``include_hidden=true``
    for admin tooling. The whole list is small (tens to low-hundreds of
    rows) so paging is cheap and the default cap of 200 returns the
    entire catalog in one round-trip.
    """
    stmt = select(McpFeaturedEntry)
    if not include_hidden:
        stmt = stmt.where(McpFeaturedEntry.hidden.is_(False))
    if category is not None:
        stmt = stmt.where(McpFeaturedEntry.category == category)
    if runtime is not None:
        stmt = stmt.where(McpFeaturedEntry.runtime == runtime)
    stmt = stmt.order_by(
        McpFeaturedEntry.featured_priority.asc(),
        McpFeaturedEntry.server_id.asc(),
    ).limit(limit).offset(offset)

    rows = (await session.execute(stmt)).scalars().all()
    return McpFeaturedListResponse(
        entries=[McpFeaturedRow.model_validate(r) for r in rows],
        count=len(rows),
    )


@router.get("/{server_id}", response_model=McpFeaturedRow)
async def get_featured(
    server_id: str,
    session: AsyncSession = Depends(get_session),
) -> McpFeaturedRow:
    row = (
        await session.execute(
            select(McpFeaturedEntry).where(
                McpFeaturedEntry.server_id == server_id
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            f"Featured catalog has no '{server_id}'",
        )
    return McpFeaturedRow.model_validate(row)


# ── Routes — admin CRUD ─────────────────────────────────────────


def _resolve_actor(request: Request) -> str:
    """Pull a display name for the admin from the request, falling back to ``admin``."""
    user = getattr(request.state, "user", None)
    if user is not None:
        return getattr(user, "email", None) or getattr(user, "username", None) or "admin"
    return "admin"


@router.post(
    "",
    response_model=McpFeaturedRow,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_admin)],
)
async def create_featured(
    body: McpFeaturedCreate,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> McpFeaturedRow:
    actor = _resolve_actor(request)
    now = datetime.now(timezone.utc)

    existing = await session.get(McpFeaturedEntry, body.server_id)
    if existing is not None:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"Featured entry '{body.server_id}' already exists. Use PATCH to update.",
        )

    row = McpFeaturedEntry(
        **body.model_dump(),
        verified_at=now,
        verified_by=actor,
    )
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return McpFeaturedRow.model_validate(row)


@router.patch(
    "/{server_id}",
    response_model=McpFeaturedRow,
    dependencies=[Depends(require_admin)],
)
async def update_featured(
    server_id: str,
    body: McpFeaturedPatch,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> McpFeaturedRow:
    row = await session.get(McpFeaturedEntry, server_id)
    if row is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            f"Featured entry '{server_id}' not found",
        )

    changes = body.model_dump(exclude_unset=True)
    for key, val in changes.items():
        setattr(row, key, val)

    row.verified_at = datetime.now(timezone.utc)
    row.verified_by = _resolve_actor(request)

    await session.commit()
    await session.refresh(row)
    return McpFeaturedRow.model_validate(row)


@router.delete(
    "/{server_id}",
    dependencies=[Depends(require_admin)],
)
async def delete_featured(
    server_id: str,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    row = await session.get(McpFeaturedEntry, server_id)
    if row is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            f"Featured entry '{server_id}' not found",
        )
    await session.delete(row)
    await session.commit()
    return {"deleted": server_id}


# ── Routes — admin bulk seed ────────────────────────────────────


# ``__file__`` is ``.../digitorn_hub/routers/mcp_featured.py``. Two ``.parent``
# hops get us to ``digitorn_hub/``, then the bundled ``data/`` dir that
# ships with the image (Dockerfile ``COPY src ./src``).
_DEFAULT_SEED = (
    Path(__file__).resolve().parent.parent / "data" / "mcp_catalog_seed.json"
)


@router.post(
    "/seed",
    response_model=McpFeaturedSeedResponse,
    dependencies=[Depends(require_admin)],
)
async def seed_featured(
    body: McpFeaturedSeedRequest | None = None,
    request: Request = None,  # type: ignore[assignment]
    session: AsyncSession = Depends(get_session),
) -> McpFeaturedSeedResponse:
    """Bulk upsert the featured catalog.

    Two modes:

    1. **POST with body** — upsert the provided ``entries`` list.
    2. **POST with empty body** — read the seed JSON shipped in the Hub
       repo at ``packages/hub/data/mcp_catalog_seed.json`` (exported once
       from the daemon's ``catalog.py``). Used by ``alembic upgrade head``
       follow-ups + first deploy bootstrap.

    Pass ``replace: true`` to wipe non-listed rows first (rare; default
    is non-destructive upsert).
    """
    if body is None or not body.entries:
        if not _DEFAULT_SEED.exists():
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                f"No body provided and default seed not found at {_DEFAULT_SEED}",
            )
        try:
            raw = json.loads(_DEFAULT_SEED.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(
                status.HTTP_500_INTERNAL_SERVER_ERROR,
                f"Failed to read default seed: {exc}",
            )
        body = McpFeaturedSeedRequest.model_validate(raw)

    actor = _resolve_actor(request) if request is not None else "admin-seed"
    now = datetime.now(timezone.utc)

    inserted = 0
    updated = 0

    seen_ids: set[str] = set()
    for entry in body.entries:
        seen_ids.add(entry.server_id)
        existing = await session.get(McpFeaturedEntry, entry.server_id)
        payload = entry.model_dump()
        if existing is None:
            row = McpFeaturedEntry(
                **payload,
                verified_at=now,
                verified_by=actor,
            )
            session.add(row)
            inserted += 1
        else:
            for key, val in payload.items():
                setattr(existing, key, val)
            existing.verified_at = now
            existing.verified_by = actor
            updated += 1

    deleted = 0
    if body.replace:
        stmt = select(McpFeaturedEntry.server_id).where(
            McpFeaturedEntry.server_id.notin_(seen_ids) if seen_ids else True
        )
        stale_ids = [r[0] for r in (await session.execute(stmt)).all()]
        for sid in stale_ids:
            obj = await session.get(McpFeaturedEntry, sid)
            if obj is not None:
                await session.delete(obj)
                deleted += 1

    await session.commit()
    return McpFeaturedSeedResponse(
        inserted=inserted, updated=updated, deleted=deleted,
    )
