"""HTTP client used by daemon-side proxies to reach a worker."""
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
    """Raised when the worker returns a non-2xx status."""

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status

class WorkerClient:
    """Async HTTP client targeting a single `WorkerEndpoint`."""

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
        # Lazy `httpx.AsyncClient`: defers the Windows cert-store
        # load out of `__init__`.
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
        if self._client is not None:
            return self._client
        async with self._client_lock:
            if self._client is not None:
                return self._client
            # Off-loop the heavy SSL-context load. Single-shot per
            # WorkerClient (cached by `get_or_create_client`).
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
        """Unary call. `ctx` carries lightweight per-call."""
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

    async def push_config(
        self, module: str, config: dict[str, Any],
        *, app_id: str | None = None,
    ) -> bool:
        """POST `/admin/config/{module}` with the per-app config."""
        path = f"/admin/config/{module}"
        body: dict[str, Any] = {"config": config}
        if app_id is not None:
            body["app_id"] = app_id
        client = await self._get_client()
        last_exc: Exception | None = None
        for attempt in range(self._retries + 1):
            try:
                resp = await client.post(path, json=body)
            except (httpx.ConnectError, httpx.ReadTimeout,
                    httpx.RemoteProtocolError) as exc:
                last_exc = exc
                if attempt >= self._retries:
                    logger.warning(
                        "worker_config_push_transport_failed module=%s "
                        "attempts=%d err=%s",
                        module, attempt + 1, exc,
                    )
                    return False
                await asyncio.sleep(0.2 * (attempt + 1))
                continue
            if resp.status_code >= 400 and resp.status_code not in (
                502, 503, 504,
            ):
                logger.warning(
                    "worker_config_push_http_error module=%s status=%d "
                    "body=%s",
                    module, resp.status_code, resp.text[:200],
                )
                return False
            if resp.status_code in (502, 503, 504):
                last_exc = WorkerError(
                    f"worker returned {resp.status_code}",
                    status=resp.status_code,
                )
                if attempt >= self._retries:
                    logger.warning(
                        "worker_config_push_5xx module=%s attempts=%d "
                        "status=%d",
                        module, attempt + 1, resp.status_code,
                    )
                    return False
                await asyncio.sleep(0.2 * (attempt + 1))
                continue
            try:
                body = resp.json()
            except json.JSONDecodeError:
                logger.warning(
                    "worker_config_push_bad_json module=%s body=%s",
                    module, resp.text[:200],
                )
                return False
            if not body.get("success"):
                logger.warning(
                    "worker_config_push_module_rejected module=%s "
                    "error=%s",
                    module, body.get("error", "(no error message)"),
                )
                return False
            return True
        # Unreachable -- the loop always exits via return.
        return False

    async def stream_action(
        self,
        module: str,
        action: str,
        args: dict[str, Any],
        *,
        ctx: dict[str, Any] | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        """NDJSON-framed bi-directional stream. Yields one decoded."""
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
            bad_chunks = 0
            async for line in resp.aiter_lines():
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError as exc:
                    bad_chunks += 1
                    logger.error(
                        "worker_stream_bad_chunk module=%s action=%s len=%d "
                        "bad_chunks=%d head=%r err=%s",
                        module, action, len(line), bad_chunks, line[:120], exc,
                    )
                    if bad_chunks >= 3:
                        yield {
                            "__error__": True,
                            "error_type": "StreamCorruption",
                            "error": (
                                f"worker NDJSON stream produced {bad_chunks} "
                                f"malformed chunks on /{module}/{action}"
                            ),
                        }
                        return
                    continue

    async def health(self) -> dict[str, Any]:
        """GET /health."""
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

_CLIENT_CACHE: dict[tuple[str, int], WorkerClient] = {}
_CLIENT_CACHE_LOCK: Any = None  # threading.Lock created lazily

def get_or_create_client(
    endpoint: WorkerEndpoint,
    *,
    timeout_s: float = 600.0,
    retries: int = 2,
) -> WorkerClient:
    """Return a shared `WorkerClient` for the endpoint, building one."""
    global _CLIENT_CACHE_LOCK
    if _CLIENT_CACHE_LOCK is None:
        import threading
        _CLIENT_CACHE_LOCK = threading.Lock()

    key = (endpoint.host if hasattr(endpoint, "host") else
           endpoint.base_url, endpoint.worker_id)
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
