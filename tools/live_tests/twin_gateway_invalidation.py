"""Prove cross-worker ConfigCache invalidation works via Redis Pub/Sub.

Setup: TWO single-worker gateways on :8205 and :8206 sharing the same
Redis (DB 2). Each holds its own in-memory ConfigCache. When gateway A
writes a route, the Redis publish fires and gateway B's subscriber
schedules a ``reload_from_db()``. Within ~1s gateway B sees the same
truth.

Without the fix this would hang for up to 30s (the periodic refresh
interval) or never converge (if the read keeps landing on the worker
that never received the write).

Test scenarios:
  1. Sanity     : both gateways respond to /healthz
  2. Bootstrap  : both gateways list the same providers / routes count
  3. Write A    : POST a model alias on A
  4. Read B     : within 3s, GET the same alias on B returns it
  5. Write A    : POST a route on A pointing at the alias
  6. Read B     : within 3s, GET the alias's routes on B returns it
  7. Dispatch B : send a chat completion through B (route lives on
                  alias just written on A); must succeed first try
  8. Cleanup    : delete the alias; B sees the deletion within 3s
"""
from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

GATEWAY_A = "http://127.0.0.1:8205"
GATEWAY_B = "http://127.0.0.1:8206"

CREDS = json.loads(
    (Path.home() / ".digitorn" / "credentials.json").read_text(encoding="utf-8")
)
H = {
    "Authorization": f"Bearer {CREDS['access_token']}",
    "Content-Type": "application/json",
}


def call(base: str, method: str, path: str, body: dict | None = None,
         *, timeout: float = 60.0) -> tuple[int, dict]:
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(
        f"{base}{path}", method=method, data=data, headers=H,
    )
    try:
        r = urllib.request.urlopen(req, timeout=timeout)
        raw = r.read()
        if "json" in r.headers.get("content-type", ""):
            return r.status, (json.loads(raw) if raw else {})
        return r.status, {"_raw": raw[:500].decode("utf-8", "replace")}
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            return exc.code, json.loads(raw or b"{}")
        except Exception:
            return exc.code, {"_raw": raw[:500].decode("utf-8", "replace")}


def wait_for(base: str, path: str, predicate, *,
             timeout_s: float = 5.0, label: str = "") -> tuple[bool, float]:
    """Poll GET ``path`` on ``base`` until ``predicate(code, body)``
    returns True. Returns ``(found, seconds_waited)``. Used to measure
    how long cross-worker convergence takes."""
    deadline = time.monotonic() + timeout_s
    t0 = time.monotonic()
    while time.monotonic() < deadline:
        code, body = call(base, "GET", path)
        if predicate(code, body):
            return True, time.monotonic() - t0
        time.sleep(0.2)
    return False, timeout_s


def find_copilot_credential() -> str:
    code, body = call(GATEWAY_A, "GET", "/admin/credentials")
    if code != 200:
        return ""
    for c in body.get("rows", []):
        if c["provider_slug"] == "github_copilot" and c["status"] == "active":
            return c["id"]
    return ""


def main() -> int:
    print("=" * 70)
    print("  CROSS-WORKER INVALIDATION via Redis Pub/Sub (twin gateways)")
    print("=" * 70)
    print(f"  Gateway A: {GATEWAY_A}")
    print(f"  Gateway B: {GATEWAY_B}")
    print()

    fails = 0
    suffix = uuid.uuid4().hex[:8]
    alias = f"github_copilot/twin-test-{suffix}"
    rid: str | None = None

    # 1. Sanity
    code, _ = call(GATEWAY_A, "GET", "/healthz")
    if code != 200:
        print("FAIL: gateway A unhealthy"); fails += 1
    else:
        print("[PASS] gateway A healthy")
    code, _ = call(GATEWAY_B, "GET", "/healthz")
    if code != 200:
        print("FAIL: gateway B unhealthy"); fails += 1
    else:
        print("[PASS] gateway B healthy")

    # 2. Bootstrap parity
    code_a, body_a = call(GATEWAY_A, "GET", "/admin/providers")
    code_b, body_b = call(GATEWAY_B, "GET", "/admin/providers")
    if code_a == 200 and code_b == 200 and body_a["count"] == body_b["count"]:
        print(f"[PASS] both gateways see {body_a['count']} providers")
    else:
        print(f"[FAIL] bootstrap parity: A={body_a.get('count')} B={body_b.get('count')}")
        fails += 1

    cred_id = find_copilot_credential()
    if not cred_id:
        print("FAIL: no github_copilot credential available")
        return 2

    try:
        # 3. Write A: POST a model alias
        code, body = call(GATEWAY_A, "POST", "/admin/models", {
            "alias": alias, "provider_slug": "github_copilot",
            "real_model_id": "gpt-4o-mini",
            "cost_per_1k_input_tokens": 0,
            "cost_per_1k_output_tokens": 0,
            "max_context_tokens": 8192,
            "is_custom": False, "metadata": {},
        })
        if code != 201:
            print(f"FAIL: POST model on A returned {code}")
            return 1
        print(f"[PASS] POST model {alias} on A -> 201")

        # 4. Read B: within 3s the alias must appear
        ok, secs = wait_for(
            GATEWAY_B, f"/admin/models/{alias}",
            lambda code, body: code == 200 and body.get("alias") == alias,
            timeout_s=3.0,
        )
        if ok:
            print(f"[PASS] gateway B sees the new alias after {secs * 1000:.0f}ms "
                  f"(Redis pub/sub propagation)")
        else:
            print(f"[FAIL] gateway B never saw the alias within 3s")
            fails += 1

        # 5. Write A: POST a route override
        code, body = call(GATEWAY_A, "POST", "/admin/routes", {
            "model_alias": alias, "credential_id": cred_id,
            "priority": 0,
            "provider_slug": "github_copilot",
            "real_model_id": "gpt-4o-mini",
            "compat": "openai_compat",
        })
        if code != 201:
            print(f"FAIL: POST route on A returned {code} {body}")
            return 1
        rid = body["id"]
        print(f"[PASS] POST route {rid[:8]}... on A -> 201")

        # 6. Read B: routes for this alias must include the new one
        ok, secs = wait_for(
            GATEWAY_B, f"/admin/routes?model_alias={alias}",
            lambda code, body: code == 200 and any(
                r["id"] == rid for r in body.get("rows", [])
            ),
            timeout_s=3.0,
        )
        if ok:
            print(f"[PASS] gateway B sees the new route after {secs * 1000:.0f}ms")
        else:
            print(f"[FAIL] gateway B never saw the route within 3s")
            fails += 1

        # 7. Dispatch B: send a chat through gateway B for the alias
        # written through gateway A. Must succeed first try -- this is
        # the exact failure mode that broke section J on multi-worker
        # before the Redis fix.
        time.sleep(0.5)  # extra settle
        code, body = call(
            GATEWAY_B, "POST", "/v1/chat/completions",
            {
                "model": alias,
                "messages": [{"role": "user", "content": "Reply with: OK"}],
                "max_tokens": 6, "temperature": 1.0,
            },
            timeout=60.0,
        )
        if code == 200 and body.get("choices"):
            text = body["choices"][0]["message"].get("content", "")
            print(f"[PASS] gateway B dispatched the alias (200, content={text!r})")
        else:
            print(f"[FAIL] gateway B dispatch -> {code} {str(body)[:160]}")
            fails += 1

        # 8. Delete on A; verify B sees the deletion
        code, _ = call(GATEWAY_A, "DELETE", f"/admin/models/{alias}")
        if code == 200:
            print(f"[PASS] DELETE model on A -> 200")
        else:
            print(f"[FAIL] DELETE on A returned {code}")
            fails += 1
        ok, secs = wait_for(
            GATEWAY_B, f"/admin/models/{alias}",
            lambda code, body: code == 404,
            timeout_s=3.0,
        )
        if ok:
            print(f"[PASS] gateway B sees the deletion after {secs * 1000:.0f}ms")
        else:
            print(f"[FAIL] gateway B never saw the deletion within 3s")
            fails += 1

    finally:
        # Best-effort cleanup
        call(GATEWAY_A, "DELETE", f"/admin/models/{alias}")

    print()
    print("=" * 70)
    print(f"  {'PASS' if fails == 0 else f'FAIL ({fails} issues)'}")
    print("=" * 70)
    return 0 if fails == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
