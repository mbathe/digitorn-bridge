"""Standalone mock LLM server that mimics the DeepSeek/OpenAI chat API.

Spins up a local aiohttp server on port 9999 returning canned JSON
responses to /v1/chat/completions. Used by the E2E credential test
suite to validate that the full agent loop runs once the api_key has
been correctly injected by the credential subsystem.

Run:
    py -3.12 tests/mock_llm_server.py        # foreground
    Ctrl-C to stop
"""
from __future__ import annotations

import json
import logging
from aiohttp import web

logger = logging.getLogger(__name__)
PORT = 9999

# Track every Authorization header we receive - the test runner reads
# this to assert that the daemon actually sent the vault api_key.
RECEIVED: list[str] = []


async def chat_completions(request: web.Request) -> web.Response:
    """Return a non-streaming completion for any input. Records the
    Authorization header so the caller can inspect what api_key the
    daemon sent."""
    auth = request.headers.get("Authorization", "")
    RECEIVED.append(auth)
    logger.info("mock_llm_chat auth=%s", auth[:20])
    body = await request.json()
    if body.get("stream"):
        # Stream a single SSE chunk.
        async def _stream() -> web.StreamResponse:
            resp = web.StreamResponse(
                status=200,
                headers={"Content-Type": "text/event-stream"},
            )
            await resp.prepare(request)
            chunk = {
                "id": "mock-1",
                "object": "chat.completion.chunk",
                "model": body.get("model", "mock"),
                "choices": [{
                    "index": 0,
                    "delta": {"role": "assistant", "content": "OK from mock LLM"},
                    "finish_reason": None,
                }],
            }
            await resp.write(b"data: " + json.dumps(chunk).encode() + b"\n\n")
            done = dict(chunk)
            done["choices"][0]["delta"] = {}
            done["choices"][0]["finish_reason"] = "stop"
            await resp.write(b"data: " + json.dumps(done).encode() + b"\n\n")
            await resp.write(b"data: [DONE]\n\n")
            await resp.write_eof()
            return resp
        return await _stream()
    return web.json_response({
        "id": "mock-1",
        "object": "chat.completion",
        "created": 1700000000,
        "model": body.get("model", "mock"),
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": "OK from mock LLM"},
            "finish_reason": "stop",
        }],
        "usage": {"prompt_tokens": 5, "completion_tokens": 4, "total_tokens": 9},
    })


async def models(request: web.Request) -> web.Response:
    return web.json_response({
        "object": "list",
        "data": [{"id": "deepseek-chat", "object": "model"}],
    })


async def received(request: web.Request) -> web.Response:
    """Diagnostic: return every Authorization header observed."""
    return web.json_response({"received": RECEIVED, "count": len(RECEIVED)})


def app() -> web.Application:
    a = web.Application()
    a.router.add_post("/v1/chat/completions", chat_completions)
    a.router.add_get("/v1/models", models)
    a.router.add_get("/__received", received)
    return a


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    web.run_app(app(), host="127.0.0.1", port=PORT)
