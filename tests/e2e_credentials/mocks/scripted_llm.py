"""Scripted OpenAI-compatible LLM mock for e2e testing.

Scenarios queue up responses BEFORE the test runs. Each user turn
the agent makes triggers a POP from the queue. Tool calls are
returned to the agent loop, the agent loop executes the tools (real
ones, hitting real mocks), then comes back for the next response.

This mock supports:
  * Plain text replies.
  * Tool calls (OpenAI/DeepSeek-format `function` calls).
  * Multi-turn (one queued response per HTTP /chat/completions hit).
  * Streaming + non-streaming.
  * Per-session response queues (different sessions can coexist).

Scripting API (over HTTP, on /__script):

    POST /__script
        {"session": "<session_id>", "responses": [
            {"text": "I'll look that up.", "tool_calls": [
                {"name": "StripeListCustomers", "arguments": {}}
            ]},
            {"text": "You have 3 customers: Alice, Bob, Charlie."}
        ]}

The tests build the queue then send messages via the daemon. Each
LLM call pops the next queued response.

When the queue is exhausted the mock returns a generic "OK" reply
so the test fails on assertion rather than on a 5xx.

Auth headers received are recorded - see /__received.
"""

from __future__ import annotations

import json
import logging
import secrets
from collections import defaultdict, deque

from aiohttp import web

logger = logging.getLogger(__name__)

PORT = 9999

# Per-(model, session) queue of scripted responses. Falls back to a
# global queue indexed by None when no session id is provided.
QUEUES: dict[str, deque[dict]] = defaultdict(deque)

# Auth headers received - test scenarios assert against this list.
RECEIVED_AUTH: list[str] = []
RECEIVED_BODIES: list[dict] = []


def _q_key(session_id: str | None) -> str:
    return session_id or "_global"


def _make_id() -> str:
    return "mock-" + secrets.token_hex(6)


def _build_chat_completion(
    *, model: str, scripted: dict | None, request_id: str,
) -> dict:
    """Translate a scripted-response dict into an OpenAI-style payload."""
    if scripted is None:
        # Default fallback: empty assistant reply.
        return {
            "id": request_id,
            "object": "chat.completion",
            "created": 1700000000,
            "model": model,
            "choices": [{
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": "OK",
                },
                "finish_reason": "stop",
            }],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }

    text = scripted.get("text", "")
    tool_calls_raw = scripted.get("tool_calls") or []
    tc_payload = []
    for i, tc in enumerate(tool_calls_raw):
        tc_payload.append({
            "id": f"tc_{i}_{secrets.token_hex(3)}",
            "type": "function",
            "function": {
                "name": tc.get("name", ""),
                "arguments": json.dumps(tc.get("arguments", {})),
            },
        })

    msg: dict = {"role": "assistant", "content": text or None}
    if tc_payload:
        msg["tool_calls"] = tc_payload

    finish = "tool_calls" if tc_payload else "stop"
    return {
        "id": request_id,
        "object": "chat.completion",
        "created": 1700000000,
        "model": model,
        "choices": [{
            "index": 0,
            "message": msg,
            "finish_reason": finish,
        }],
        "usage": {"prompt_tokens": 5, "completion_tokens": 5, "total_tokens": 10},
    }


def _stream_chunks(payload: dict) -> bytes:
    """Encode a non-streaming completion as SSE chunks (so the agent
    loop's streaming code path runs)."""
    out: list[bytes] = []
    msg = payload["choices"][0]["message"]
    delta_text = msg.get("content") or ""
    delta_tcs = msg.get("tool_calls") or []
    base = {
        "id": payload["id"], "object": "chat.completion.chunk",
        "model": payload["model"],
    }

    # Emit the role first.
    role_chunk = dict(base)
    role_chunk["choices"] = [{
        "index": 0,
        "delta": {"role": "assistant"},
        "finish_reason": None,
    }]
    out.append(b"data: " + json.dumps(role_chunk).encode() + b"\n\n")

    # Content (chunked into 1 piece for simplicity).
    if delta_text:
        c = dict(base)
        c["choices"] = [{
            "index": 0,
            "delta": {"content": delta_text},
            "finish_reason": None,
        }]
        out.append(b"data: " + json.dumps(c).encode() + b"\n\n")

    # Tool calls.
    for i, tc in enumerate(delta_tcs):
        c = dict(base)
        c["choices"] = [{
            "index": 0,
            "delta": {
                "tool_calls": [{
                    "index": i,
                    "id": tc["id"],
                    "type": "function",
                    "function": {
                        "name": tc["function"]["name"],
                        "arguments": tc["function"]["arguments"],
                    },
                }],
            },
            "finish_reason": None,
        }]
        out.append(b"data: " + json.dumps(c).encode() + b"\n\n")

    # Finish.
    fin = dict(base)
    fin["choices"] = [{
        "index": 0,
        "delta": {},
        "finish_reason": payload["choices"][0]["finish_reason"],
    }]
    out.append(b"data: " + json.dumps(fin).encode() + b"\n\n")
    out.append(b"data: [DONE]\n\n")
    return b"".join(out)


# ─── HTTP routes ────────────────────────────────────────────────────


async def chat_completions(request: web.Request) -> web.Response:
    """Pop the next scripted response and return it (stream or not)."""
    auth = request.headers.get("Authorization", "")
    RECEIVED_AUTH.append(auth)
    body = await request.json()
    RECEIVED_BODIES.append(body)
    session = (
        request.headers.get("X-Session-Id")
        or body.get("metadata", {}).get("session_id")
        or "_global"
    )
    model = body.get("model", "scripted-mock")

    # Pop in priority: per-session queue, fallback to global.
    queue = QUEUES.get(session) or QUEUES.get("_global")
    scripted = queue.popleft() if queue else None
    payload = _build_chat_completion(
        model=model, scripted=scripted, request_id=_make_id(),
    )

    if body.get("stream"):
        resp = web.StreamResponse(
            status=200,
            headers={"Content-Type": "text/event-stream"},
        )
        await resp.prepare(request)
        await resp.write(_stream_chunks(payload))
        await resp.write_eof()
        return resp
    return web.json_response(payload)


async def models(request: web.Request) -> web.Response:
    return web.json_response({
        "object": "list",
        "data": [
            {"id": "deepseek-chat", "object": "model"},
            {"id": "deepseek-v4-pro", "object": "model"},
            {"id": "scripted-mock", "object": "model"},
        ],
    })


async def script(request: web.Request) -> web.Response:
    """Configure the queue for a session_id."""
    body = await request.json()
    session = body.get("session", "_global")
    responses = body.get("responses", [])
    QUEUES[session] = deque(responses)
    return web.json_response({"queued": len(responses), "session": session})


async def reset(request: web.Request) -> web.Response:
    QUEUES.clear()
    RECEIVED_AUTH.clear()
    RECEIVED_BODIES.clear()
    return web.json_response({"ok": True})


async def received(request: web.Request) -> web.Response:
    return web.json_response({
        "auth_headers": RECEIVED_AUTH,
        "bodies_count": len(RECEIVED_BODIES),
        "remaining_queues": {k: len(v) for k, v in QUEUES.items()},
    })


def app() -> web.Application:
    a = web.Application()
    a.router.add_post("/v1/chat/completions", chat_completions)
    a.router.add_get("/v1/models", models)
    a.router.add_post("/__script", script)
    a.router.add_post("/__reset", reset)
    a.router.add_get("/__received", received)
    return a


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    web.run_app(app(), host="127.0.0.1", port=PORT)
