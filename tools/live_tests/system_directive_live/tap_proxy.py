"""Passthrough HTTP proxy that sits between the daemon and Ollama.

Purpose
-------
The daemon's ``openai_compat`` provider calls ``/v1/chat/completions``
on whatever ``base_url`` the YAML config sets. By pointing that
``base_url`` at this proxy and the proxy at the real Ollama port, we
get a complete capture of every request the daemon sends to the LLM -
the EXACT ``messages: [...]`` list that the model sees - without
touching either side.

Why not a wrapper provider?
---------------------------
A wrapper would require adding code to ``packages/digitorn``, which
muddies the prod path with test-only logic. A side-car HTTP proxy
lives entirely in test infra, plugs in via one YAML edit, and exits
with the test. The daemon doesn't know it exists.

Capture format
--------------
Every captured request is appended to ``captures.jsonl`` in the run
directory. One JSON object per line:

    {
      "ts": "2026-05-17T12:34:56Z",
      "path": "/v1/chat/completions",
      "method": "POST",
      "request": { "model": "...", "messages": [...] },
      "response_status": 200
    }

The scenarios filter on ``request.messages`` to verify directive
visibility.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from aiohttp import ClientSession, ClientTimeout, web

logger = logging.getLogger(__name__)


class OllamaTapProxy:
    """Tiny aiohttp app: capture + forward."""

    def __init__(
        self,
        *,
        listen_host: str = "127.0.0.1",
        listen_port: int = 11500,
        upstream: str = "http://127.0.0.1:11434",
        capture_path: Path,
    ) -> None:
        self.listen_host = listen_host
        self.listen_port = listen_port
        self.upstream = upstream.rstrip("/")
        self.capture_path = Path(capture_path)
        self.capture_path.parent.mkdir(parents=True, exist_ok=True)
        # Re-open per request so the file never grows stale across long
        # test runs. The OS handles batched fsync; we don't need O_SYNC.
        self.capture_path.write_text("", encoding="utf-8")
        self._app: web.Application | None = None
        self._runner: web.AppRunner | None = None
        self._site: web.TCPSite | None = None
        self._client: ClientSession | None = None
        self._lock = asyncio.Lock()

    @property
    def base_url(self) -> str:
        return f"http://{self.listen_host}:{self.listen_port}"

    async def start(self) -> None:
        self._client = ClientSession(timeout=ClientTimeout(total=300))
        self._app = web.Application()
        # Catch-all - we don't enumerate Ollama's paths so any future
        # endpoint the daemon hits is transparently forwarded.
        self._app.router.add_route("*", "/{tail:.*}", self._handle)
        self._runner = web.AppRunner(self._app, access_log=None)
        await self._runner.setup()
        self._site = web.TCPSite(
            self._runner, host=self.listen_host, port=self.listen_port,
        )
        await self._site.start()
        logger.info(
            "tap proxy listening on %s -> %s (captures -> %s)",
            self.base_url, self.upstream, self.capture_path,
        )

    async def stop(self) -> None:
        if self._site is not None:
            await self._site.stop()
            self._site = None
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None
        if self._client is not None:
            await self._client.close()
            self._client = None

    async def __aenter__(self) -> "OllamaTapProxy":
        await self.start()
        return self

    async def __aexit__(self, *args: Any) -> None:
        await self.stop()

    # ── HTTP handler ──────────────────────────────────────────────

    async def _handle(self, request: web.Request) -> web.StreamResponse:
        assert self._client is not None
        path = request.path
        url = f"{self.upstream}{path}"
        if request.query_string:
            url = f"{url}?{request.query_string}"

        body_raw = await request.read()
        try:
            body_json = json.loads(body_raw.decode("utf-8")) if body_raw else None
        except Exception:
            body_json = None

        # Strip hop-by-hop + host so the upstream sees a clean request.
        fwd_headers = {
            k: v for k, v in request.headers.items()
            if k.lower() not in {"host", "content-length", "connection"}
        }

        # NOTE: streaming responses (SSE) - just forward chunked. The
        # daemon uses non-stream OpenAI-compat for Ollama by default,
        # so the common path is a single JSON response.
        async with self._client.request(
            request.method, url,
            data=body_raw, headers=fwd_headers,
        ) as upstream_resp:
            resp_status = upstream_resp.status
            # Read the whole body so we can both forward AND capture it
            # (capture isn't strictly needed for response, but useful
            # for diagnostics on a failing turn). aiohttp streams the
            # whole body to client either way.
            resp_body = await upstream_resp.read()
            resp_headers = {
                k: v for k, v in upstream_resp.headers.items()
                if k.lower() not in {"transfer-encoding", "content-encoding", "content-length"}
            }

        # Append capture - serialised so concurrent turns don't interleave
        # half-written lines (jsonl readers parse line by line).
        async with self._lock:
            with self.capture_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps({
                    "ts": datetime.now(timezone.utc).isoformat(),
                    "path": path,
                    "method": request.method,
                    "request": body_json,
                    "response_status": resp_status,
                }, ensure_ascii=False) + "\n")

        return web.Response(
            status=resp_status, body=resp_body, headers=resp_headers,
        )

    # ── Reader helpers ────────────────────────────────────────────

    def load_captures(self) -> list[dict[str, Any]]:
        """Re-read captures.jsonl. Cheap, no streaming - the run
        produces a few hundred lines max."""
        if not self.capture_path.exists():
            return []
        out: list[dict[str, Any]] = []
        for line in self.capture_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return out

    def chat_captures(self) -> list[dict[str, Any]]:
        """Filter to ``/v1/chat/completions`` POSTs. Returns the
        request payload of each capture (``model``, ``messages``,
        ``tools``, ...). The order matches the wall-clock order in
        which the daemon issued the LLM call."""
        out: list[dict[str, Any]] = []
        for cap in self.load_captures():
            if (
                cap.get("path") == "/v1/chat/completions"
                and cap.get("method") == "POST"
                and isinstance(cap.get("request"), dict)
            ):
                out.append(cap["request"])
        return out

    def messages_seen_by_llm(self) -> list[list[dict[str, Any]]]:
        """Return the ``messages`` array of each chat() request, in
        chronological order. Each entry is what the LLM "saw" at one
        LLM round-trip."""
        return [req.get("messages") or [] for req in self.chat_captures()]


async def _selftest() -> int:
    """Quick smoke: forward one /api/tags request and assert capture
    landed."""
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        cap = Path(td) / "captures.jsonl"
        async with OllamaTapProxy(
            listen_port=11500, capture_path=cap,
        ) as proxy:
            async with ClientSession() as session:
                async with session.get(f"{proxy.base_url}/api/tags") as r:
                    assert r.status == 200, f"proxy did not forward: {r.status}"
                    body = await r.json()
                    assert "models" in body
            caps = proxy.load_captures()
            assert len(caps) == 1, f"expected 1 capture, got {len(caps)}"
            assert caps[0]["path"] == "/api/tags"
            print(f"tap proxy selftest OK (captured {len(caps)} requests)")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(asyncio.run(_selftest()))
