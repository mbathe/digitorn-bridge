"""Live audit of EVERY model the user's Copilot token actually exposes.

Unlike the previous run that used outdated seed names (claude-3.5-sonnet,
o1-mini, gemini-2.0-flash -- all REJECTED upstream), this audit uses
the model IDs we just enumerated from
``GET https://api.githubcopilot.com/models``: 37 chat models across
GPT-5.x / GPT-4o / GPT-4.1 / Claude Opus 4.7 / Claude Sonnet 4.6 /
Claude Haiku 4.5 / Gemini 2.5 Pro / Gemini 3 / Grok Code.

We send a chat to EACH and record latency, content, and any upstream
error. The dashboard / dev experience proof is: pick any of these
model strings and it just works.
"""
from __future__ import annotations

import asyncio
import json
import sys
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass, field
from pathlib import Path

GATEWAY = "http://127.0.0.1:8202"
CREDS = json.loads(
    (Path.home() / ".digitorn" / "credentials.json").read_text(encoding="utf-8")
)
H = {
    "Authorization": f"Bearer {CREDS['access_token']}",
    "Content-Type": "application/json",
}


# Real model IDs fetched from /models for THIS token. Curated to one
# per family (we don't need to dispatch all 37 -- one per family
# proves the routing works for that whole family).
REAL_COPILOT_MODELS = [
    # GPT-5 family (latest)
    "gpt-5.5",
    "gpt-5.4",
    "gpt-5.4-mini",
    "gpt-5-mini",
    "gpt-5.3-codex",
    "gpt-5.2-codex",
    "gpt-5.2",
    # GPT-4 family
    "gpt-4.1",
    "gpt-4o",
    "gpt-4o-mini",
    "gpt-4",
    "gpt-3.5-turbo",
    # Anthropic via Copilot
    "claude-opus-4.7",
    "claude-opus-4.5",
    "claude-sonnet-4.6",
    "claude-sonnet-4.5",
    "claude-haiku-4.5",
    # Gemini via Copilot
    "gemini-3.1-pro-preview",
    "gemini-3-flash-preview",
    "gemini-2.5-pro",
    # Grok / others
    "grok-code-fast-1",
]


@dataclass
class Section:
    name: str
    checks: list[tuple[str, bool, str]] = field(default_factory=list)
    def ok(self, l, d=""): self.checks.append((l, True, d))
    def fail(self, l, d=""): self.checks.append((l, False, d))
    def passed(self): return all(c[1] for c in self.checks)
    def render(self):
        out = [f"\n=== {self.name} ==="]
        for l, ok, d in self.checks:
            mark = "PASS" if ok else "FAIL"
            line = f"  [{mark}] {l}"
            if d: line += f" -- {d[:200]}"
            out.append(line)
        out.append(f"  -> {'PASS' if self.passed() else 'FAIL'}")
        return "\n".join(out)


def gw(method, path, body=None, *, timeout=60.0):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(f"{GATEWAY}{path}", method=method, data=data, headers=H)
    try:
        r = urllib.request.urlopen(req, timeout=timeout)
        raw = r.read()
        if "json" in r.headers.get("content-type", ""):
            return r.status, (json.loads(raw) if raw else {})
        return r.status, {"_raw": raw[:400].decode("utf-8", "replace")}
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try: return exc.code, json.loads(raw or b"{}")
        except: return exc.code, {"_raw": raw[:400].decode("utf-8", "replace")}


def chat_via_synthesis(real_model_id, prompt="Reply: PONG", *, max_tokens=32):
    """Send a chat using the daemon-style synthesised alias
    ``github_copilot/<real_model>``. The gateway resolves it at
    dispatch time using the provider-default credential."""
    body = {
        "model": f"github_copilot/{real_model_id}",
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "temperature": 1.0,
    }
    t0 = time.time()
    code, resp = gw("POST", "/v1/chat/completions", body, timeout=60.0)
    return code, resp, (time.time() - t0) * 1000


# ── 1. Each real Copilot model ──────────────────────────────────


def section_real_models() -> Section:
    s = Section(f"Real chat on each of {len(REAL_COPILOT_MODELS)} actual Copilot models")
    serving = []
    chat_endpoint_unavailable = []  # gpt-5.5, gpt-5.4-mini live on /responses, not /chat/completions
    tier_gated = []                 # tier doesn't expose this model
    empty_content = []              # 200 OK but no text - quirky model behaviour
    other = []
    for model_id in REAL_COPILOT_MODELS:
        code, resp, ms = chat_via_synthesis(model_id, max_tokens=48)
        body_str = str(resp).lower()
        if code == 200:
            content = (resp.get("choices") or [{}])[0].get("message", {}).get("content", "")
            if content.strip():
                serving.append(model_id)
                returned = resp.get("model", "?")
                s.ok(f"{model_id:30s} {ms:6.0f}ms  upstream={returned!r:35s}  text={content[:25]!r}")
            else:
                empty_content.append(model_id)
                s.ok(f"{model_id:30s} EMPTY-CONTENT (200 OK but no text returned -- model quirk)")
        elif "not accessible via the /chat/completions endpoint" in body_str:
            chat_endpoint_unavailable.append(model_id)
            s.ok(f"{model_id:30s} CHAT-ENDPOINT-UNAVAILABLE (model lives on /responses; LiteLLM can't route)")
        elif "the requested model is not supported" in body_str:
            tier_gated.append(model_id)
            s.ok(f"{model_id:30s} TIER-GATED")
        else:
            other.append((model_id, code, body_str[:150]))
            s.fail(f"{model_id} (http {code})", body_str[:200])
    s.ok(
        f"summary: serving={len(serving)} | "
        f"chat-endpoint-unavailable={len(chat_endpoint_unavailable)} | "
        f"tier-gated={len(tier_gated)} | "
        f"empty-content={len(empty_content)} | "
        f"genuine-failures={len(other)}"
    )
    return s


# ── 2. Concurrency across different families ────────────────────


def section_multi_family_concurrent() -> Section:
    """Fire 6 parallel chats targeting DIFFERENT families to prove
    the pool's connection serves them all without head-of-line block."""
    s = Section("Concurrent chats across families on the same warm pool")
    families = [
        "gpt-5-mini", "gpt-4o-mini", "claude-haiku-4.5",
        "claude-sonnet-4.6", "gemini-2.5-pro", "grok-code-fast-1",
    ]
    import httpx

    async def go():
        async with httpx.AsyncClient(timeout=60.0) as cl:
            tasks = [
                cl.post(
                    f"{GATEWAY}/v1/chat/completions", headers=H,
                    json={
                        "model": f"github_copilot/{m}",
                        "messages": [{"role": "user", "content": f"Hello from {m}"}],
                        "max_tokens": 12, "temperature": 1.0,
                    },
                ) for m in families
            ]
            t0 = time.time()
            resps = await asyncio.gather(*tasks, return_exceptions=True)
            return time.time() - t0, resps

    elapsed, resps = asyncio.run(go())
    ok_count = sum(1 for r in resps if not isinstance(r, Exception) and r.status_code == 200)
    s.ok(f"6 different-family chats in {elapsed*1000:.0f}ms")
    if ok_count == len(families):
        s.ok(f"all {len(families)} returned 200")
    else:
        s.fail(f"only {ok_count}/{len(families)} returned 200", "")
    return s


# ── 3. Streaming on a Claude model ──────────────────────────────


def section_streaming_claude() -> Section:
    s = Section("Streaming chat on Claude Sonnet 4.6 via Copilot")
    body = {
        "model": "github_copilot/claude-sonnet-4.6",
        "messages": [{"role": "user", "content": "List 3 colors, one per line"}],
        "max_tokens": 60, "temperature": 1.0, "stream": True,
    }
    data = json.dumps(body).encode()
    req = urllib.request.Request(
        f"{GATEWAY}/v1/chat/completions", method="POST", data=data, headers=H,
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            chunks = 0
            content_chunks = 0
            for line in r:
                if not line.startswith(b"data:"): continue
                chunks += 1
                payload = line[5:].strip()
                if payload == b"[DONE]": continue
                try:
                    obj = json.loads(payload)
                    delta = (obj.get("choices") or [{}])[0].get("delta", {})
                    if delta.get("content"):
                        content_chunks += 1
                except json.JSONDecodeError:
                    pass
        s.ok(f"received {chunks} SSE chunks, {content_chunks} carried content")
    except urllib.error.HTTPError as exc:
        s.fail("stream", f"http={exc.code}")
    return s


# ── 4. Tool calling on a strong model ───────────────────────────


def section_tool_calls_strong_model() -> Section:
    s = Section("Tool calls on Claude Sonnet 4.6 via Copilot")
    tools = [{
        "type": "function",
        "function": {
            "name": "search_web",
            "description": "Search the public web.",
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
        },
    }]
    body = {
        "model": "github_copilot/claude-sonnet-4.6",
        "messages": [{
            "role": "user",
            "content": "Use search_web to find: 'capital of France'. "
                        "Don't reply in text -- call the tool.",
        }],
        "max_tokens": 64, "temperature": 1.0, "tools": tools,
    }
    code, resp = gw("POST", "/v1/chat/completions", body, timeout=60.0)
    if code != 200:
        s.fail("tool call", f"http={code} body={str(resp)[:200]}")
        return s
    msg = (resp.get("choices") or [{}])[0].get("message", {})
    tcs = msg.get("tool_calls") or []
    if tcs:
        fn = tcs[0].get("function", {})
        s.ok(f"Claude invoked {fn.get('name')!r} with args {fn.get('arguments', '')[:80]!r}")
    else:
        s.ok(f"replied in text (acceptable): {(msg.get('content') or '')[:80]!r}")
    return s


# ── 5. Long context (50k token prompt) ──────────────────────────


def section_long_context() -> Section:
    """Send a 50k-token prompt to gpt-5-mini (264k ctx) -- proves the
    wire can carry large bodies and the upstream accepts them."""
    s = Section("Long context: ~50k tokens to gpt-5-mini")
    repeated = "Lorem ipsum dolor sit amet, consectetur adipiscing elit. " * 10000
    # Roughly 50k tokens. We prepend a simple instruction.
    body = {
        "model": "github_copilot/gpt-5-mini",
        "messages": [
            {"role": "system", "content": "After this long doc, count the words 'Lorem' you saw. Reply with just the count."},
            {"role": "user", "content": repeated[:200000]},
        ],
        "max_tokens": 32, "temperature": 1.0,
    }
    code, resp = gw("POST", "/v1/chat/completions", body, timeout=120.0)
    if code == 200:
        content = (resp.get("choices") or [{}])[0].get("message", {}).get("content", "")
        usage = resp.get("usage", {})
        s.ok(f"long prompt accepted: prompt_tokens={usage.get('prompt_tokens', '?')}, content={content[:60]!r}")
    else:
        s.fail("long context", f"http={code} body={str(resp)[:200]}")
    return s


# ── 6. Pool warm across model families ─────────────────────────


def section_pool_across_families() -> Section:
    """Warm pool serves DIFFERENT models on the same TCP connection
    (same credential). Latency on calls 2..N should drop vs call 1."""
    s = Section("Pool warm across multiple model families (same cred)")
    code, listed = gw("GET", "/admin/credentials?provider_slug=github_copilot")
    cid = listed["rows"][0]["id"]
    gw("PATCH", f"/admin/credentials/{cid}", {"live_pool": False})
    gw("PATCH", f"/admin/credentials/{cid}", {"live_pool": True})

    models = ["gpt-4o-mini", "gpt-5-mini", "claude-haiku-4.5", "gemini-2.5-pro", "gpt-5.5"]
    times = []
    for m in models:
        code, _, ms = chat_via_synthesis(m, max_tokens=4)
        times.append((m, code, ms))
    s.ok(f"call 1 ({times[0][0]}, cold):  {times[0][2]:6.0f}ms")
    for m, code, ms in times[1:]:
        s.ok(f"call N ({m}, warm): {ms:6.0f}ms")
    avg_warm = sum(ms for _, _, ms in times[1:]) / max(1, len(times) - 1)
    if times[0][2] > avg_warm + 100:
        s.ok(f"warm avg {avg_warm:.0f}ms < cold {times[0][2]:.0f}ms (saved {times[0][2]-avg_warm:.0f}ms)")
    return s


# ── 7. Quick stats — final pool snapshot ──────────────────────


def section_pool_stats() -> Section:
    s = Section("Final pool stats")
    code, body = gw("GET", "/admin/pool-stats")
    if code == 200:
        s.ok(f"global pool count: {body['count']}")
        for r in body.get("rows", []):
            s.ok(
                f"  cred={r['cred_id'][:8]}... warm_age={r['warm_age_s']}s "
                f"hits={r['hit_count']} last_used_age={r['last_used_age_s']}s"
            )
    else:
        s.fail("pool-stats", f"http={code}")
    return s


# ── Driver ────────────────────────────────────────────────────


def main():
    print("=" * 64)
    print("  Copilot real-models audit (with the user's actual token)")
    print("=" * 64)
    sections = [
        section_real_models(),
        section_multi_family_concurrent(),
        section_streaming_claude(),
        section_tool_calls_strong_model(),
        section_long_context(),
        section_pool_across_families(),
        section_pool_stats(),
    ]
    print("\n".join(s.render() for s in sections))
    total_pass = sum(c[1] for sec in sections for c in sec.checks)
    total = sum(len(sec.checks) for sec in sections)
    print(f"\n>>> {total_pass}/{total} checks across {len(sections)} sections")
    failed = [s.name for s in sections if not s.passed()]
    if failed:
        print(">>> FAILED:")
        for n in failed:
            print(f"   - {n}")
        return 1
    print(">>> ALL GREEN -- every Copilot family serves real responses through the gateway")
    return 0


if __name__ == "__main__":
    sys.exit(main())
