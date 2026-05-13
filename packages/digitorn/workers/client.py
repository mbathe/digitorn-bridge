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
        self._client = httpx.AsyncClient(
            base_url=endpoint.base_url,
            timeout=httpx.Timeout(
                connect=10.0, read=timeout_s, write=60.0, pool=10.0,
            ),
            limits=_DEFAULT_LIMITS,
            headers={
                "Authorization": f"Bearer {endpoint.secret}",
                "Content-Type": "application/json",
                "X-Digitorn-Worker-Client": "1",
            },
        )

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
        last_exc: Exception | None = None
        for attempt in range(self._retries + 1):
            try:
                resp = await self._client.post(path, json=payload)
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
        async with self._client.stream(
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
        resp = await self._client.get("/health")
        resp.raise_for_status()
        return resp.json()

    async def aclose(self) -> None:
        await self._client.aclose()
