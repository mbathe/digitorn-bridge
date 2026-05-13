"""HTTP client used by daemon-side proxies to reach a worker.

Two call shapes:

  * ``call_action(module, action, args)`` -- unary POST returning
    a serialised ``ActionResult``-shaped dict. Used by ``ModuleProxy``
    for the vast majority of tool calls (Bash, WsWrite, ...).

  * ``stream_action(module, action, args)`` -- bi-directional stream
    yielding chunks. Used by ``LLMProviderProxy`` for ``chat_stream``
    so the daemon never bufferises a whole LLM response. The transport
    is HTTP chunked with ``application/x-ndjson`` framing -- one
    JSON object per line, ready to deserialise into the provider's
    chunk shape on the daemon side.

Connection re-use: a single ``httpx.AsyncClient`` lives per
``WorkerEndpoint`` -- HTTP/1.1 keepalive + connection pool of 32 per
host. This is critical because re-establishing a TCP+TLS connection
per tool call would re-introduce the very SSL stall we're sorting
out by moving the work off-loop.
"""
from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from typing import Any

import httpx

from .registry import WorkerEndpoint

logger = logging.getLogger(__name__)


_DEFAULT_LIMITS = httpx.Limits(
    max_connections=32,
    max_keepalive_connections=32,
    keepalive_expiry=60.0,
)


class WorkerError(Exception):
    """Raised when the worker returns a non-2xx status or the
    payload cannot be parsed. Distinct from transport errors so the
    proxy can decide whether to retry or surface the failure.
    """

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class WorkerClient:
    """Async HTTP client targeting a single ``WorkerEndpoint``.

    Reusable across many calls; the proxy holds one of these per
    endpoint it routes to. Closing it cancels the in-flight pool.

    Performance notes
    -----------------

    * **Lazy ``httpx.AsyncClient`` init** -- creating an
      ``httpx.AsyncClient`` eagerly calls
      ``ssl.create_default_context()`` which loads the Windows
      certificate store via ``CertOpenSystemStoreW``. On a cold
      machine this is a 200ms-3s SYNCHRONOUS syscall that BLOCKS
      THE MAIN LOOP. Constructing many ``WorkerClient`` instances
      back-to-back at daemon boot (one per workered module) used to
      cascade into 2-5s stalls. We now defer the
      ``httpx.AsyncClient`` until the first actual call -- the
      construction itself is microseconds.

    * **Per-endpoint cache** (see ``get_or_create_client`` below) --
      one ``WorkerClient`` per ``(host, port)`` shared across all
      modules a worker hosts. Cuts client count from
      ``len(hosted_modules) * len(apps)`` down to
      ``len(workers)``.
    """

    def __init__(
        self,
        endpoint: WorkerEndpoint,
        *,
        timeout_s: float = 600.0,
        retries: int = 2,
    ) -> None:
        self._endpoint = endpoint
        self._timeout_s = timeout_s
        self._retries = retries
        # Lazy: the actual httpx.AsyncClient is built on first use
        # inside ``_get_client()``. Avoids loading the SSL context
        # at __init__ time, which on Windows blocks the main loop
        # for hundreds of ms during the cert-store read.
        self._client: httpx.AsyncClient | None = None
        self._client_lock = asyncio.Lock()

    def _build_client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=self._endpoint.base_url,
            timeout=httpx.Timeout(
                connect=10.0,
                read=self._timeout_s,
                write=60.0,
                pool=10.0,
            ),
            limits=_DEFAULT_LIMITS,
            headers={
                "Authorization": f"Bearer {self._endpoint.secret}",
                "Content-Type": "application/json",
                "X-Digitorn-Worker-Client": "1",
            },
        )

    async def _get_client(self) -> httpx.AsyncClient:
        """Return the underlying ``httpx.AsyncClient``, building it on
        first use. The SSL-context creation that happens inside
        ``httpx.AsyncClient.__init__`` is moved to a worker thread via
        ``asyncio.to_thread`` so the main loop doesn't block.
        """
        if self._client is not None:
            return self._client
        async with self._client_lock:
            if self._client is not None:
                return self._client
            # Off-loop the heavy SSL-context load. Single-shot per
            # WorkerClient (cached by ``get_or_create_client``).
            self._client = await asyncio.to_thread(self._build_client)
            return self._client

    @property
    def endpoint(self) -> WorkerEndpoint:
        return self._endpoint

    async def call_action(
        self,
        module: str,
        action: str,
        args: dict[str, Any],
        *,
        ctx: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Unary call. ``ctx`` carries lightweight per-call context
        (session_id, user_id, agent_id, workspace, etc.) the worker
        needs to reconstruct an ``AgentContext``-compatible scope.

        Retries on connection-reset / 502 / 503 with linear backoff.
        Surfaces a ``WorkerError`` on 4xx/5xx that aren't transient.
        """
        payload = {"args": args, "ctx": ctx or {}}
        path = f"/tool/{module}/{action}"
        client = await self._get_client()
        last_exc: Exception | None = None
        for attempt in range(self._retries + 1):
            try:
                resp = await client.post(path, json=payload)
            except (httpx.ConnectError, httpx.ReadTimeout,
                    httpx.RemoteProtocolError) as exc:
                last_exc = exc
                if attempt >= self._retries:
                    raise WorkerError(
                        f"worker transport error after "
                        f"{attempt + 1} attempts: {exc}",
                    ) from exc
                await asyncio.sleep(0.2 * (attempt + 1))
                continue
            if resp.status_code in (502, 503, 504):
                last_exc = WorkerError(
                    f"worker returned {resp.status_code}",
                    status=resp.status_code,
                )
                if attempt >= self._retries:
                    raise last_exc
                await asyncio.sleep(0.2 * (attempt + 1))
                continue
            if resp.status_code >= 400:
                raise WorkerError(
                    f"worker {resp.status_code}: {resp.text[:500]}",
                    status=resp.status_code,
                )
            try:
                return resp.json()
            except json.JSONDecodeError as exc:
                raise WorkerError(
                    f"worker returned non-JSON: {resp.text[:200]}",
                ) from exc
        # Unreachable -- the loop always exits via return/raise.
        raise WorkerError(f"worker call failed: {last_exc}")

    async def stream_action(
        self,
        module: str,
        action: str,
        args: dict[str, Any],
        *,
        ctx: dict[str, Any] | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        """NDJSON-framed bi-directional stream. Yields one decoded
        dict per chunk. Caller iterates with ``async for``; closing
        the iterator early cancels the request cleanly.

        Used for LLM streaming -- the worker forwards anthropic /
        openai SSE chunks line-by-line. Never bufferises end-to-end.
        """
        payload = {"args": args, "ctx": ctx or {}}
        path = f"/stream/{module}/{action}"
        client = await self._get_client()
        async with client.stream(
            "POST", path, json=payload,
        ) as resp:
            if resp.status_code >= 400:
                body = (await resp.aread()).decode("utf-8", "replace")
                raise WorkerError(
                    f"worker stream {resp.status_code}: {body[:500]}",
                    status=resp.status_code,
                )
            async for line in resp.aiter_lines():
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    logger.warning(
                        "worker_stream_bad_chunk module=%s action=%s "
                        "len=%d",
                        module, action, len(line),
                    )
                    continue

    async def health(self) -> dict[str, Any]:
        """``GET /health`` -- returns worker uptime, hosted modules,
        and basic stats. Used by the supervisor poller.
        """
        client = await self._get_client()
        resp = await client.get("/health")
        resp.raise_for_status()
        return resp.json()

    async def aclose(self) -> None:
        # The lazy client may never have been built (e.g. wrap was
        # installed but no call ever fired). Nothing to close in that
        # case -- avoid surprising AttributeError on the user side.
        if self._client is None:
            return
        await self._client.aclose()
        self._client = None


# ── Per-endpoint singleton cache ─────────────────────────────────


_CLIENT_CACHE: dict[tuple[str, int], WorkerClient] = {}
_CLIENT_CACHE_LOCK: Any = None  # threading.Lock created lazily


def get_or_create_client(
    endpoint: WorkerEndpoint,
    *,
    timeout_s: float = 600.0,
    retries: int = 2,
) -> WorkerClient:
    """Return a shared ``WorkerClient`` for the endpoint, building one
    on first request. Subsequent calls with the same ``(host, port)``
    return the SAME client.

    Why a cache: ``WorkerClient.__init__`` is now cheap (lazy
    ``httpx.AsyncClient``) but the connection pool inside the
    underlying httpx client is the resource we really want to share.
    Without caching, every wrapped module + every ``LLMProviderProxy``
    instantiates its OWN pool of up to 32 keepalive connections to
    the worker -- multiplied by N apps deployed at boot, we'd build
    hundreds of TCP connections for nothing. Shared client = single
    pool, one keepalive per worker.

    Thread-safe: uses a module-level ``threading.Lock`` (we may be
    called from sync code paths like ``registry.create``).
    """
    global _CLIENT_CACHE_LOCK
    if _CLIENT_CACHE_LOCK is None:
        import threading
        _CLIENT_CACHE_LOCK = threading.Lock()

    key = (endpoint.host if hasattr(endpoint, "host") else
           endpoint.base_url, endpoint.worker_id)
    # ``WorkerEndpoint`` only carries ``base_url`` + ``worker_id`` --
    # use (base_url, worker_id) as the cache key. Two endpoints with
    # the same base_url and same worker_id are the same physical
    # destination; the secret is part of the same daemon's shared
    # secret so it's identical too.
    cache_key = (endpoint.base_url, endpoint.worker_id)
    with _CLIENT_CACHE_LOCK:
        cached = _CLIENT_CACHE.get(cache_key)
        if cached is not None:
            return cached
        client = WorkerClient(
            endpoint, timeout_s=timeout_s, retries=retries,
        )
        _CLIENT_CACHE[cache_key] = client
        return client


async def shutdown_all_clients() -> None:
    """Close every cached client. Called by the lifespan shutdown."""
    with _CLIENT_CACHE_LOCK if _CLIENT_CACHE_LOCK is not None else _NullCtx():
        clients = list(_CLIENT_CACHE.values())
        _CLIENT_CACHE.clear()
    for c in clients:
        try:
            await c.aclose()
        except Exception as exc:
            logger.debug("worker_client_close_failed: %s", exc)


class _NullCtx:
    def __enter__(self): return self
    def __exit__(self, *a): return False
