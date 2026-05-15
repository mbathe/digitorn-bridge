"""MCP registry ingester — keeps ``hub.mcp_registry_entries`` in sync
with ``registry.modelcontextprotocol.io``.

Run modes:

* ``ingest_once(session)`` — fetch every page from upstream, upsert
  each row, refresh ``search_vector`` + ``embedding``, mark missing
  rows as ``retired``. One-shot. Called by the periodic loop AND
  the manual ``POST /api/v1/mcp/registry/refresh`` admin endpoint.

* ``start_periodic_loop(session_factory)`` — background asyncio task
  spawned at hub startup. Runs ``ingest_once`` every 24h. Idempotent;
  uses bounded timeout + try/except so a transient network failure
  never crashes the hub.

The ingester is the **only** place that contacts the upstream
registry. Everything else (daemon proxy, dashboard search) reads
exclusively from our Postgres mirror.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any

import httpx
from sqlalchemy import bindparam, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .models import McpRegistryEntry
from .search import embed_document

logger = logging.getLogger(__name__)


REGISTRY_URL = "https://registry.modelcontextprotocol.io/v0.1/servers"
PAGE_LIMIT = 100  # registry max per page
PAGE_TIMEOUT_S = 15.0
INGEST_PERIOD_S = 24 * 3600  # 24 h


_OAUTH_PATTERNS = ("OAUTH", "CLIENT_ID", "CLIENT_SECRET", "REFRESH_TOKEN")


# ── Flattening: upstream JSON → our row shape ───────────────────


def _flatten_server(server: dict[str, Any]) -> dict[str, Any]:
    """Convert one upstream ``server`` dict to our table-row shape.

    Mirrors the daemon-side ``_summarize_registry_server`` so the
    ``server_id`` + flat fields stay byte-identical between hub-cached
    and daemon-direct paths.
    """
    name = server.get("name", "")
    short = name.rsplit("/", 1)[-1] if "/" in name else name

    packages = server.get("packages") or []
    remotes = server.get("remotes") or []

    runtime = "none"
    package_name = ""
    transport = "stdio"
    required_env_count = 0
    env_var_names: list[str] = []

    for pkg in packages:
        if not isinstance(pkg, dict):
            continue
        reg_type = pkg.get("registryType", "")
        if reg_type == "npm":
            runtime, transport = "npm", "stdio"
            package_name = pkg.get("identifier", "")
        elif reg_type in ("pip", "pypi"):
            runtime, transport = "pip", "stdio"
            package_name = pkg.get("identifier", "")
        for ev in pkg.get("environmentVariables", []) or []:
            if isinstance(ev, dict):
                env_name = ev.get("name", "")
                if env_name:
                    env_var_names.append(env_name)
                if ev.get("isRequired", False):
                    required_env_count += 1
        if runtime != "none":
            break

    if runtime == "none" and remotes:
        for remote in remotes:
            if not isinstance(remote, dict):
                continue
            rtype = remote.get("type", "")
            if rtype == "streamable-http":
                transport = "streamable_http"
            elif rtype == "sse":
                transport = "sse"
            for header in remote.get("headers", []) or []:
                if isinstance(header, dict) and header.get("value", ""):
                    required_env_count += 1
            break

    has_oauth = any(
        any(p in (ev or "").upper() for p in _OAUTH_PATTERNS)
        for ev in env_var_names
    )

    repo = server.get("repository") or {}
    repository_url = repo.get("url", "") if isinstance(repo, dict) else ""

    version_field = server.get("version")
    if isinstance(version_field, dict):
        version = str(version_field.get("number") or "")
    else:
        version = str(version_field or "")

    return {
        "server_id": short,
        "name": name,
        "description": (server.get("description") or "") or None,
        "runtime": runtime,
        "package": package_name or None,
        "transport": transport,
        "has_oauth": has_oauth,
        "required_env_count": required_env_count,
        "env_var_names": env_var_names,
        "version": version or None,
        "repository_url": repository_url or None,
        "status": (server.get("status") or "active").lower(),
        "raw_metadata": server,
    }


# ── Upstream fetch ──────────────────────────────────────────────


async def _fetch_all_pages() -> list[dict[str, Any]]:
    """Paginate the upstream registry until exhausted.

    Returns a flat list of ``server`` dicts (unflattened). Bounded by
    a per-page timeout so a stalled upstream can't hang the loop.
    """
    out: list[dict[str, Any]] = []
    cursor: str | None = None

    async with httpx.AsyncClient(timeout=PAGE_TIMEOUT_S) as client:
        while True:
            params: dict[str, Any] = {
                "limit": PAGE_LIMIT,
                "version": "latest",
            }
            if cursor:
                params["cursor"] = cursor
            try:
                resp = await client.get(REGISTRY_URL, params=params)
                resp.raise_for_status()
                data = resp.json()
            except Exception as exc:
                logger.warning(
                    "mcp_registry_fetch_failed cursor=%r err=%s",
                    cursor, exc,
                )
                break

            page_servers = data.get("servers") or []
            for entry in page_servers:
                if not isinstance(entry, dict):
                    continue
                server = entry.get("server", entry)
                if isinstance(server, dict):
                    out.append(server)

            meta = data.get("metadata") or {}
            cursor = meta.get("next_cursor") or meta.get("nextCursor")
            if not cursor:
                break

    return out


# ── Upsert ──────────────────────────────────────────────────────


_FTS_UPDATE_SQL = text(
    """
    UPDATE hub.mcp_registry_entries SET
      search_vector = setweight(to_tsvector('simple', :name), 'A') ||
                      setweight(to_tsvector('simple', :env), 'B') ||
                      setweight(to_tsvector('simple', :descr), 'C') ||
                      setweight(to_tsvector('simple', :pkg), 'D')
    WHERE server_id = :server_id
    """
).bindparams(
    bindparam("name"),
    bindparam("env"),
    bindparam("descr"),
    bindparam("pkg"),
    bindparam("server_id"),
)


async def _upsert_one(
    session: AsyncSession,
    row: dict[str, Any],
    seen_at: datetime,
    *,
    embed: bool = True,
) -> None:
    """Insert or update a single registry entry, then refresh FTS +
    embedding for that row."""
    stmt = pg_insert(McpRegistryEntry).values(
        **row,
        last_seen_at=seen_at,
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=["server_id"],
        set_={
            "name": stmt.excluded.name,
            "description": stmt.excluded.description,
            "runtime": stmt.excluded.runtime,
            "package": stmt.excluded.package,
            "transport": stmt.excluded.transport,
            "has_oauth": stmt.excluded.has_oauth,
            "required_env_count": stmt.excluded.required_env_count,
            "env_var_names": stmt.excluded.env_var_names,
            "version": stmt.excluded.version,
            "repository_url": stmt.excluded.repository_url,
            "status": stmt.excluded.status,
            "raw_metadata": stmt.excluded.raw_metadata,
            "last_seen_at": stmt.excluded.last_seen_at,
        },
    )
    await session.execute(stmt)

    await session.execute(
        _FTS_UPDATE_SQL,
        {
            "name": row["name"],
            "env": " ".join(row["env_var_names"] or []),
            "descr": row["description"] or "",
            "pkg": row["package"] or "",
            "server_id": row["server_id"],
        },
    )

    if embed:
        try:
            vec = await embed_document(
                row["name"],
                row["description"] or "",
                row["env_var_names"] or [],
            )
        except Exception as exc:
            logger.debug(
                "mcp_registry_embed_failed server=%s err=%s",
                row["server_id"], exc,
            )
            vec = []
        if vec:
            try:
                await session.execute(
                    update(McpRegistryEntry)
                    .where(McpRegistryEntry.server_id == row["server_id"])
                    .values(embedding=vec)
                )
            except Exception as exc:
                logger.debug(
                    "mcp_registry_embed_store_failed server=%s err=%s",
                    row["server_id"], exc,
                )


async def _mark_missing_retired(
    session: AsyncSession, seen_ids: set[str],
) -> int:
    """Flip ``status`` to ``retired`` for any row not seen in the last
    fetch. We don't delete — keeps the dashboard from flipping empty
    if upstream returns partial data during an outage. ``retired``
    rows fall out of the default search filter."""
    if not seen_ids:
        return 0
    stmt = (
        update(McpRegistryEntry)
        .where(McpRegistryEntry.server_id.not_in(seen_ids))
        .where(McpRegistryEntry.status != "retired")
        .values(status="retired")
    )
    result = await session.execute(stmt)
    return int(result.rowcount or 0)


# ── Public entry points ─────────────────────────────────────────


_COMMIT_BATCH = 200


async def ingest_once(
    session: AsyncSession,
    *,
    embed: bool = False,
) -> dict[str, int]:
    """Fetch the full registry once and upsert every row.

    The registry holds 8000+ servers. Two pragmatic choices keep this
    cheap on Neon + fast on first boot:

    1. **Embeddings are OFF by default** (``embed=False``). FTS is the
       primary search path and is computed inline. Semantic embeddings
       are deferred to a separate background pass (``embed_missing``)
       so first ingest finishes in ~30s instead of ~15 min.
    2. **Commits every ``_COMMIT_BATCH`` rows.** A single end-of-loop
       commit holds an 8000-row transaction open for several minutes,
       which Neon will kill on its idle-in-tx timeout. Per-batch
       commits also make partial progress visible to the dashboard
       while the ingest is still running.

    Returns ``{fetched, upserted, retired}``.
    """
    seen_at = datetime.now(timezone.utc)
    servers = await _fetch_all_pages()
    logger.info("mcp_registry_fetch_ok servers=%d", len(servers))

    seen_ids: set[str] = set()
    upserted = 0
    batch_size = 0
    for server in servers:
        try:
            row = _flatten_server(server)
        except Exception as exc:
            logger.debug("mcp_registry_flatten_failed err=%s", exc)
            continue
        if not row["server_id"]:
            continue
        seen_ids.add(row["server_id"])
        try:
            await _upsert_one(session, row, seen_at, embed=embed)
            upserted += 1
            batch_size += 1
        except Exception as exc:
            logger.warning(
                "mcp_registry_upsert_failed server=%s err=%s",
                row["server_id"], exc,
            )

        if batch_size >= _COMMIT_BATCH:
            try:
                await session.commit()
            except Exception as exc:
                logger.warning("mcp_registry_batch_commit_failed err=%s", exc)
                await session.rollback()
            batch_size = 0
            logger.info("mcp_registry_progress upserted=%d", upserted)

    retired = 0
    try:
        retired = await _mark_missing_retired(session, seen_ids)
    except Exception as exc:
        logger.warning("mcp_registry_retire_failed err=%s", exc)

    try:
        await session.commit()
    except Exception as exc:
        logger.warning("mcp_registry_final_commit_failed err=%s", exc)
        await session.rollback()

    logger.info(
        "mcp_registry_ingest_done fetched=%d upserted=%d retired=%d",
        len(servers), upserted, retired,
    )
    return {
        "fetched": len(servers),
        "upserted": upserted,
        "retired": retired,
    }


async def embed_missing(
    session: AsyncSession,
    *,
    batch_size: int = 100,
    max_rows: int | None = None,
) -> int:
    """Walk rows where ``embedding IS NULL`` and fill them in.

    Designed to run in a separate background task so the bulk-upsert
    of the registry stays fast. Embeds in batches via fastembed (one
    model load, one pass over batch_size sentences) instead of
    one-at-a-time. Commits every ``batch_size`` rows.

    Returns the count of rows embedded.
    """
    from sqlalchemy import bindparam, text as sql_text
    from .search.embeddings import _embed_sync
    import asyncio as _asyncio

    embedded = 0
    while True:
        rows = list(
            (
                await session.execute(
                    select(
                        McpRegistryEntry.server_id,
                        McpRegistryEntry.name,
                        McpRegistryEntry.description,
                        McpRegistryEntry.env_var_names,
                    )
                    .where(McpRegistryEntry.embedding.is_(None))
                    .where(McpRegistryEntry.status != "retired")
                    .limit(batch_size)
                )
            ).all()
        )
        if not rows:
            break

        # Compose corpus + offload all embeddings in one to_thread call
        # so fastembed batches them under the hood.
        corpus: list[tuple[str, str]] = []
        for sid, name, desc, env in rows:
            parts = [name or ""]
            if desc:
                parts.append(desc)
            if env:
                parts.append(" ".join(env))
            corpus.append((sid, " || ".join(p for p in parts if p)))

        def _embed_batch(items: list[tuple[str, str]]) -> list[tuple[str, list[float]]]:
            return [(sid, _embed_sync(body)) for sid, body in items]

        try:
            results = await _asyncio.to_thread(_embed_batch, corpus)
        except Exception as exc:
            logger.warning("mcp_registry_embed_batch_failed err=%s", exc)
            break

        for sid, vec in results:
            if not vec:
                continue
            try:
                await session.execute(
                    sql_text(
                        "UPDATE hub.mcp_registry_entries "
                        "SET embedding = CAST(:vec AS vector) "
                        "WHERE server_id = :sid"
                    ).bindparams(
                        bindparam("vec", value=str(vec)),
                        bindparam("sid", value=sid),
                    )
                )
                embedded += 1
            except Exception as exc:
                logger.debug(
                    "mcp_registry_embed_store_failed sid=%s err=%s", sid, exc,
                )

        try:
            await session.commit()
        except Exception as exc:
            logger.warning("mcp_registry_embed_commit_failed err=%s", exc)
            await session.rollback()
            break

        logger.info("mcp_registry_embed_progress embedded=%d", embedded)
        if max_rows is not None and embedded >= max_rows:
            break

    logger.info("mcp_registry_embed_done embedded=%d", embedded)
    return embedded


async def _periodic_loop(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    initial_delay: float = 30.0,
    period: float = INGEST_PERIOD_S,
) -> None:
    """Run ``ingest_once`` every *period* seconds. First run after
    *initial_delay* so the hub startup doesn't block on the registry."""
    await asyncio.sleep(initial_delay)
    while True:
        try:
            async with session_factory() as session:
                await ingest_once(session)
        except asyncio.CancelledError:
            return
        except Exception as exc:
            logger.warning("mcp_registry_periodic_failed err=%s", exc)
        await asyncio.sleep(period)


def start_periodic_loop(
    session_factory: async_sessionmaker[AsyncSession],
) -> asyncio.Task[None]:
    """Spawn the background loop and return the task handle. Caller
    cancels it on shutdown."""
    return asyncio.create_task(
        _periodic_loop(session_factory),
        name="mcp_registry_ingester",
    )


# ── Hybrid search (FTS + semantic + RRF) ────────────────────────


async def hybrid_search(
    session: AsyncSession,
    *,
    q: str,
    limit: int = 50,
    include_retired: bool = False,
) -> list[McpRegistryEntry]:
    """Search the local mirror. Two paths combined via RRF:

    * **FTS** on ``search_vector`` — websearch_to_tsquery against the
      name(A) + env(B) + description(C) + package(D) weighted vector.
    * **Semantic** on ``embedding`` — pgvector cosine kNN.

    Empty ``q`` returns rows ordered by ``last_seen_at`` (freshness
    proxy for popularity until we collect install telemetry).
    """
    from .search import reciprocal_rank_fusion  # local to avoid import cycles
    q = (q or "").strip()
    candidate_limit = max(limit, 100)

    if not q:
        stmt = (
            select(McpRegistryEntry)
            .order_by(McpRegistryEntry.last_seen_at.desc())
            .limit(limit)
        )
        if not include_retired:
            stmt = stmt.where(McpRegistryEntry.status != "retired")
        return list((await session.execute(stmt)).scalars())

    fts_stmt = text(
        """
        SELECT server_id
        FROM hub.mcp_registry_entries
        WHERE search_vector @@ websearch_to_tsquery('simple', :q)
        """
        + ("" if include_retired else " AND status != 'retired' ") +
        """
        ORDER BY ts_rank_cd(search_vector, websearch_to_tsquery('simple', :q)) DESC
        LIMIT :lim
        """
    ).bindparams(bindparam("q", value=q), bindparam("lim", value=candidate_limit))
    fts_ids = list((await session.execute(fts_stmt)).scalars())

    sem_ids: list[str] = []
    try:
        from .search import embed_query
        qvec = await embed_query(q)
    except Exception:
        qvec = []
    if qvec:
        sem_stmt = text(
            """
            SELECT server_id
            FROM hub.mcp_registry_entries
            WHERE embedding IS NOT NULL
            """
            + ("" if include_retired else " AND status != 'retired' ") +
            """
            ORDER BY embedding <=> CAST(:vec AS vector)
            LIMIT :lim
            """
        ).bindparams(
            bindparam("vec", value=str(qvec)),
            bindparam("lim", value=candidate_limit),
        )
        sem_ids = list((await session.execute(sem_stmt)).scalars())

    # RRF expects UUID lists. Our PK is a string — adapt by wrapping
    # the IDs in a stable hashable type. ``reciprocal_rank_fusion``
    # only uses equality + dict keys, so plain strings work.
    fused: list[tuple[str, float]] = reciprocal_rank_fusion(
        [fts_ids, sem_ids],
    )  # type: ignore[arg-type]
    fused_ids = [sid for sid, _ in fused[:limit]]
    if not fused_ids:
        return []

    rows = list(
        (
            await session.execute(
                select(McpRegistryEntry).where(
                    McpRegistryEntry.server_id.in_(fused_ids)
                )
            )
        ).scalars()
    )
    # Re-sort to preserve RRF order.
    order = {sid: i for i, sid in enumerate(fused_ids)}
    rows.sort(key=lambda r: order.get(r.server_id, 1 << 30))
    return rows
