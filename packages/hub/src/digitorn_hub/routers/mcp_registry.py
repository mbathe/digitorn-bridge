"""MCP registry routes — public read + admin refresh.

Public surface (consumed by daemons + the dashboard):

  * ``GET  /api/v1/mcp/registry/browse?q=&limit=&offset=&include_retired=``
  * ``GET  /api/v1/mcp/registry/{server_id}``

Admin surface:

  * ``POST /api/v1/mcp/registry/refresh``  — kick the ingester once

The browse endpoint runs the hybrid search defined in
``mcp_registry_ingester.hybrid_search`` (FTS + semantic + RRF) when
``q`` is non-empty, or a freshness-ordered popularity browse when
empty. Page sizes are bounded to keep responses small for hub
listings.
"""
from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth.deps import require_admin
from ..db import get_session
from ..mcp_registry_ingester import (
    hybrid_search,
    ingest_once,
)
from ..models import McpRegistryEntry

router = APIRouter(prefix="/mcp/registry", tags=["mcp"])


# ── Schemas ─────────────────────────────────────────────────────


class McpRegistryRow(BaseModel):
    """Public projection of one ``McpRegistryEntry`` row. Matches the
    daemon's existing ``_summarize_registry_server`` shape byte-for-byte
    so the Flutter dashboard doesn't need a separate parser."""

    server_id: str
    name: str
    description: str = ""
    runtime: str = "none"
    package: str = ""
    transport: str = "stdio"
    has_oauth: bool = False
    required_env_count: int = 0
    env_var_names: list[str] = Field(default_factory=list)
    version: str = ""
    repository_url: str = ""
    status: str = "active"
    source: str = "registry"

    @classmethod
    def from_orm_row(cls, r: McpRegistryEntry) -> "McpRegistryRow":
        return cls(
            server_id=r.server_id,
            name=r.name or "",
            description=r.description or "",
            runtime=r.runtime,
            package=r.package or "",
            transport=r.transport,
            has_oauth=bool(r.has_oauth),
            required_env_count=int(r.required_env_count or 0),
            env_var_names=list(r.env_var_names or []),
            version=r.version or "",
            repository_url=r.repository_url or "",
            status=r.status,
        )


class McpRegistryBrowseResponse(BaseModel):
    servers: list[McpRegistryRow]
    count: int
    next_cursor: str | None = None


class McpRegistryDetailResponse(McpRegistryRow):
    """Detail = summary + the full upstream metadata blob for callers
    that need the raw shape (install path needs ``packages[].environment
    Variables[].description`` for example)."""

    raw_metadata: dict[str, Any] = Field(default_factory=dict)


class McpRegistryRefreshResponse(BaseModel):
    fetched: int
    upserted: int
    retired: int


# ── Routes ──────────────────────────────────────────────────────


@router.get("/browse", response_model=McpRegistryBrowseResponse)
async def browse(
    q: Annotated[str, Query(max_length=200)] = "",
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0, le=10_000)] = 0,
    include_retired: Annotated[bool, Query()] = False,
    runtime: Annotated[str | None, Query(pattern="^(npm|pip|none|custom)$")] = None,
    has_oauth: Annotated[bool | None, Query()] = None,
    session: AsyncSession = Depends(get_session),
) -> McpRegistryBrowseResponse:
    """Hybrid search over the cached registry.

    Empty ``q`` → freshness-ordered browse (limit/offset pagination).
    Non-empty ``q`` → FTS + semantic, ranked via RRF.

    Optional ``runtime`` / ``has_oauth`` filters apply after the
    ranked candidate pool — keeps the score order stable.
    """
    rows = await hybrid_search(
        session, q=q, limit=limit + offset, include_retired=include_retired,
    )

    # Apply post-ranking filters in Python — the candidate pool is
    # capped at 100ish so this stays cheap.
    if runtime is not None:
        rows = [r for r in rows if r.runtime == runtime]
    if has_oauth is not None:
        rows = [r for r in rows if bool(r.has_oauth) == has_oauth]

    sliced = rows[offset : offset + limit]
    out_servers = [McpRegistryRow.from_orm_row(r) for r in sliced]

    next_cursor: str | None = None
    if len(rows) > offset + limit:
        next_cursor = str(offset + limit)

    return McpRegistryBrowseResponse(
        servers=out_servers,
        count=len(out_servers),
        next_cursor=next_cursor,
    )


@router.get(
    "/{server_id}", response_model=McpRegistryDetailResponse,
)
async def get_entry(
    server_id: str,
    session: AsyncSession = Depends(get_session),
) -> McpRegistryDetailResponse:
    row = (
        await session.execute(
            select(McpRegistryEntry).where(
                McpRegistryEntry.server_id == server_id
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            f"MCP registry has no '{server_id}'",
        )
    base = McpRegistryRow.from_orm_row(row)
    return McpRegistryDetailResponse(
        **base.model_dump(),
        raw_metadata=row.raw_metadata or {},
    )


@router.post(
    "/refresh",
    response_model=McpRegistryRefreshResponse,
    dependencies=[Depends(require_admin)],
)
async def refresh(
    session: AsyncSession = Depends(get_session),
) -> McpRegistryRefreshResponse:
    """Force a one-shot ingest from the upstream registry.

    Idempotent. Bounded by the per-page timeout in the ingester, so
    worst case ~60s for 800+ entries plus embedding cost. Use sparingly
    — the background loop already refreshes every 24h.
    """
    counts = await ingest_once(session)
    return McpRegistryRefreshResponse(**counts)
