"""Per-credential connection pool for the dispatch hot path.

Each credential with ``live_pool=True`` gets a long-lived
``httpx.AsyncClient`` we hand to ``litellm.acompletion(client=...)``.
LiteLLM uses our client instead of building a fresh one per call,
so the **TCP + TLS handshake is paid ONCE at first dispatch** and
then reused on every subsequent call (HTTP/2 multiplexing folded
in -- 10 parallel tool calls share a single open socket).

Lifecycle::

    [first dispatch]  pool.get(cred) ->  build httpx.AsyncClient
                                          + warm DNS + TLS handshake
                                          + cache it under cred.id
                                          + return same instance forever
    [next dispatch]   pool.get(cred) ->  same instance, 0ms overhead

    [token rotated]   pool.invalidate(cred.id) ->  drop the cached
                                                    client (its bearer
                                                    is stale anyway).
                                                    Next dispatch re-warms.

    [toggle off]      pool.on_credential_changed(cred.id, False)
                       -> close the socket, free memory.

    [toggle on]       lazily warmed at the next dispatch.

    [unused for >300s]  warmer loop closes idle clients to reclaim the
                         socket. Next dispatch re-warms.

LiteLLM keeps doing all the work it does today (usage extraction,
cost calc via its price table OR our gateway catalogue, tool-call
normalisation, error mapping). The ONLY thing this module changes
is "where do connection bytes come from" -- which is the entire
~150-300ms latency story.
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


# Sane defaults for an LLM dispatch path. Tuned for:
#  - HTTP/2 multiplexing (one socket carries N concurrent reqs)
#  - 5-minute keepalive so the connection survives idle gaps
#  - Generous timeouts (Sonnet thinking can run for 60s+)
_DEFAULT_LIMITS_KEEPALIVE = 20
_DEFAULT_KEEPALIVE_S = 300.0
_DEFAULT_TIMEOUT_S = 120.0
# Idle clients are evicted after this many seconds of zero use, so
# memory doesn't pile up if an operator leaves 1000 creds with
# live_pool=True but only uses 10 of them.
_IDLE_EVICT_S = 600.0


@dataclass
class _PooledClient:
    """One warm SDK client + its bookkeeping. The actual instance is
    provider-specific (``openai.AsyncOpenAI`` for openai-compat
    providers, ``anthropic.AsyncAnthropic`` for Anthropic-shape
    ones). LiteLLM accepts both via its ``client=`` kwarg.

    The expensive resource here is the ``httpx.AsyncClient`` ALIVE
    INSIDE the SDK wrapper: it owns the TLS context + connection pool.
    Re-using the same instance across calls = TCP+TLS handshake paid
    only once, ~150-300ms saved per dispatch."""

    cred_id: uuid.UUID
    client: Any  # openai.AsyncOpenAI | anthropic.AsyncAnthropic
    kind: str    # "openai" | "anthropic" - how to close + telemetry
    fingerprint: tuple  # (api_key_hash, base_url) - invalidate on change
    created_at: float
    last_used_at: float
    hit_count: int = 0


@dataclass
class PoolStats:
    """JSON-serialisable per-credential snapshot for the dashboard."""

    cred_id: str
    warm: bool
    warm_age_s: float = 0.0
    last_used_age_s: float = 0.0
    hit_count: int = 0


class ConnectionPool:
    """Per-credential ``httpx.AsyncClient`` registry."""

    def __init__(self) -> None:
        self._clients: dict[uuid.UUID, _PooledClient] = {}
        self._lock = asyncio.Lock()
        self._evictor_task: asyncio.Task[None] | None = None

    # ── Hot path ─────────────────────────────────────────────────────

    async def ensure(
        self,
        cred_id: uuid.UUID,
        *,
        kind: str,
        api_key: str | None,
        base_url: str | None,
        timeout: float = _DEFAULT_TIMEOUT_S,
    ) -> Any:
        """Return the warm SDK client for ``cred_id``; build one if
        absent or if the credential's identity changed (token rotated,
        base_url edited).

        ``kind`` selects the SDK shape:
          * ``"openai"``    -> ``openai.AsyncOpenAI`` (covers all
                               openai_compat: openai, deepseek,
                               together, fireworks, cerebras, copilot,
                               huggingface, codeium, ...)
          * ``"anthropic"`` -> ``anthropic.AsyncAnthropic``

        Other kinds aren't pooled: returns ``None`` and the dispatch
        path falls back to LiteLLM's default per-call client."""
        if kind not in _SUPPORTED_KINDS:
            return None

        fp = (
            (api_key or "")[:12] + (api_key or "")[-4:],  # cheap hash
            (base_url or "").lower(),
        )
        entry = self._clients.get(cred_id)
        if entry is not None and entry.fingerprint == fp:
            entry.last_used_at = time.monotonic()
            entry.hit_count += 1
            return entry.client

        async with self._lock:
            entry = self._clients.get(cred_id)
            if entry is not None and entry.fingerprint == fp:
                entry.last_used_at = time.monotonic()
                entry.hit_count += 1
                return entry.client
            # Stale entry (cred mutated but invalidate() didn't run):
            # close the old one before replacing.
            if entry is not None:
                try:
                    asyncio.get_running_loop().create_task(
                        _close(entry.client),
                    )
                except RuntimeError:
                    pass
            client = await self._build_sdk_client(
                kind=kind, api_key=api_key, base_url=base_url, timeout=timeout,
            )
            if client is None:
                return None
            now = time.monotonic()
            entry = _PooledClient(
                cred_id=cred_id, client=client, kind=kind,
                fingerprint=fp,
                created_at=now, last_used_at=now,
            )
            self._clients[cred_id] = entry
            logger.info(
                "connection_pool: warmed cred=%s kind=%s (now %d live)",
                cred_id, kind, len(self._clients),
            )
            return client

    # ── Lifecycle ────────────────────────────────────────────────────

    def invalidate(self, cred_id: uuid.UUID) -> None:
        """Drop the cached client. Called on token rotate / cred delete /
        cred edit. The actual socket close is async-fire-and-forget so
        the caller doesn't have to await."""
        entry = self._clients.pop(cred_id, None)
        if entry is None:
            return
        try:
            asyncio.get_running_loop().create_task(_close(entry.client))
        except RuntimeError:
            # No loop -> we're being called from a sync test path; skip.
            pass
        logger.info("connection_pool: invalidated cred=%s", cred_id)

    def on_credential_changed(self, cred_id: uuid.UUID, live_pool: bool) -> None:
        """Operator flipped the toggle. When OFF we evict so the socket
        is freed; when ON we lazy-warm at the next dispatch."""
        if not live_pool:
            self.invalidate(cred_id)

    async def shutdown(self) -> None:
        """Close every open client. Called from the daemon lifespan."""
        if self._evictor_task is not None:
            self._evictor_task.cancel()
            try:
                await self._evictor_task
            except (asyncio.CancelledError, Exception):
                pass
            self._evictor_task = None
        clients = list(self._clients.values())
        self._clients.clear()
        await asyncio.gather(
            *(_close(c.client) for c in clients), return_exceptions=True,
        )

    # ── Stats ────────────────────────────────────────────────────────

    def stats(self, cred_id: uuid.UUID) -> PoolStats:
        entry = self._clients.get(cred_id)
        if entry is None:
            return PoolStats(cred_id=str(cred_id), warm=False)
        now = time.monotonic()
        return PoolStats(
            cred_id=str(cred_id),
            warm=True,
            warm_age_s=round(now - entry.created_at, 2),
            last_used_age_s=round(now - entry.last_used_at, 2),
            hit_count=entry.hit_count,
        )

    def all_stats(self) -> list[PoolStats]:
        return [self.stats(cid) for cid in list(self._clients.keys())]

    # ── Background evictor ───────────────────────────────────────────

    def start_evictor(self, interval_s: float = 60.0) -> None:
        """Periodically close idle clients (>``_IDLE_EVICT_S`` since last
        use). Idempotent: calling twice replaces the previous task."""
        if self._evictor_task is not None and not self._evictor_task.done():
            return

        async def _loop() -> None:
            while True:
                try:
                    await asyncio.sleep(interval_s)
                    await self._evict_idle_once()
                except asyncio.CancelledError:
                    return
                except Exception as exc:  # pragma: no cover
                    logger.warning(
                        "connection_pool evictor error (continuing): %s", exc,
                    )

        self._evictor_task = asyncio.create_task(
            _loop(), name="gateway-pool-evictor",
        )

    async def _evict_idle_once(self) -> None:
        now = time.monotonic()
        to_drop = [
            cid for cid, e in self._clients.items()
            if now - e.last_used_at > _IDLE_EVICT_S
        ]
        for cid in to_drop:
            self.invalidate(cid)
        if to_drop:
            logger.info(
                "connection_pool: evicted %d idle clients", len(to_drop),
            )

    # ── Internals ────────────────────────────────────────────────────

    def _build_httpx(self, timeout: float) -> Any:
        """Build a warm ``httpx.AsyncClient`` tuned for LLM dispatch.

        - Keepalive 5 min so a 4-min idle gap doesn't drop the TLS
        - Generous timeouts (Sonnet thinking can take 60s+)
        - No follow_redirects (LLM endpoints don't redirect)

        Note: HTTP/2 is intentionally OFF here - the openai SDK's
        Stream protocol assumes HTTP/1.1 chunked encoding (it parses
        SSE bytes directly) and HTTP/2 would change framing in a way
        that occasionally breaks streaming. Connection-level pooling
        on HTTP/1.1 is what we actually need; multiplexing is a nice
        bonus we trade for streaming reliability."""
        import httpx
        return httpx.AsyncClient(
            timeout=timeout,
            limits=httpx.Limits(
                max_keepalive_connections=_DEFAULT_LIMITS_KEEPALIVE,
                keepalive_expiry=_DEFAULT_KEEPALIVE_S,
            ),
            follow_redirects=False,
        )

    async def _build_sdk_client(
        self,
        *,
        kind: str,
        api_key: str | None,
        base_url: str | None,
        timeout: float,
    ) -> Any:
        """Wrap a warm httpx client in the right SDK class.
        LiteLLM accepts these via ``client=``."""
        warm = self._build_httpx(timeout)
        if kind == "openai":
            try:
                import openai
            except ImportError:
                logger.warning("connection_pool: openai SDK missing")
                await _close(warm)
                return None
            return openai.AsyncOpenAI(
                api_key=api_key or "missing",
                base_url=base_url,
                http_client=warm,
                max_retries=0,  # gateway has its own failover loop
            )
        if kind == "anthropic":
            try:
                import anthropic
            except ImportError:
                logger.warning("connection_pool: anthropic SDK missing")
                await _close(warm)
                return None
            return anthropic.AsyncAnthropic(
                api_key=api_key or "missing",
                base_url=base_url,
                http_client=warm,
                max_retries=0,
            )
        await _close(warm)
        return None


# Compat values that map onto an httpx-based SDK we know how to wrap.
# Bedrock / Vertex have their own session pooling (boto3 / google-auth)
# so the pool returns None for those.
_SUPPORTED_KINDS: frozenset[str] = frozenset({"openai", "anthropic"})


def kind_for_compat(compat: str) -> str | None:
    """Map a provider compat dialect to the pool's SDK kind.

    Note on anthropic: passing a pre-built ``anthropic.AsyncAnthropic``
    via LiteLLM's ``client=`` kwarg crashes with
    ``AsyncAPIClient.post() got an unexpected keyword argument 'headers'``
    -- LiteLLM tries to forward ``extra_headers`` to the wrapped
    client in a shape the Anthropic SDK doesn't accept. The fix is in
    LiteLLM (or skipping ``extra_headers`` when ``client=`` is set,
    which would break ``claude_code``'s required Editor headers).
    Until then, anthropic compat opts out of pooling -- LiteLLM
    builds its own client per call (cold TLS each time, but at least
    it works). The openai-compat path is unaffected and pools fine.
    """
    if compat in ("openai", "openai_compat", "azure"):
        return "openai"
    return None  # anthropic, bedrock, vertex_ai, custom -> not pooled


async def _close(client: Any) -> None:
    """Best-effort close. Handles raw httpx clients AND the SDK
    wrappers (openai.AsyncOpenAI / anthropic.AsyncAnthropic both
    expose ``aclose``). Swallows errors so a misbehaving socket
    doesn't crash the pool."""
    try:
        if hasattr(client, "aclose"):
            await client.aclose()
        elif hasattr(client, "close"):
            close = client.close
            if asyncio.iscoroutinefunction(close):
                await close()
            else:
                close()
    except Exception:  # pragma: no cover
        pass


_default_pool: ConnectionPool | None = None


def get_pool() -> ConnectionPool:
    """Process-wide singleton. Always returns the same instance so the
    cache survives across requests."""
    global _default_pool
    if _default_pool is None:
        _default_pool = ConnectionPool()
    return _default_pool
