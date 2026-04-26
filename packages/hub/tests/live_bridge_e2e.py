"""Live end-to-end test against a running Hub.

Run BEFORE this:
  - alembic upgrade head        # ensures 0003 applied
  - python -m digitorn_hub.admin daemon register --name central \
        --pubkey-file <pub>     # registers the test daemon
  - HUB_ENABLE_DAEMON_BRIDGE=true python -m digitorn_hub  # local hub up

Then:
  python tests/live_bridge_e2e.py <path-to-private-key>

Asserts:
  1. happy path             → 200, bearer token + user.created flag
  2. auto-provisioned user  → can call /auth/me with the token
  3. replay (same nonce)    → 409 nonce already used
  4. tampered field         → 401 signature does not verify
  5. stale ts (-3600s)      → 400 outside window
  6. unknown daemon name    → 401 unknown or revoked daemon
"""
from __future__ import annotations

import base64
import json
import secrets
import sys
import time
from pathlib import Path

import httpx
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

HUB = "http://127.0.0.1:8001"
DAEMON_NAME = "central"


def canonical(fields: dict) -> bytes:
    return json.dumps(
        fields, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def sign(sk: Ed25519PrivateKey, fields: dict) -> dict:
    sig = sk.sign(canonical(fields))
    return {**fields, "signature": base64.b64encode(sig).decode("ascii")}


def fresh_payload(
    sk: Ed25519PrivateKey,
    *,
    daemon_name: str = DAEMON_NAME,
    user_id: str = "u-test-1",
    email: str = "bridge-test@digitorn.io",
    display_name: str | None = "Bridge Test",
    ts_offset: int = 0,
) -> dict:
    return sign(
        sk,
        {
            "daemon_name": daemon_name,
            "user_id": user_id,
            "email": email,
            "display_name": display_name,
            "ts": int(time.time()) + ts_offset,
            "nonce": secrets.token_hex(16),
        },
    )


def expect(resp: httpx.Response, status: int, label: str) -> None:
    if resp.status_code != status:
        print(f"  [FAIL] {label}: expected {status}, got {resp.status_code} {resp.text[:300]}")
        sys.exit(2)
    print(f"  [OK]   {label}: {status}")


def main(sk_path: Path) -> int:
    sk_raw = base64.b64decode(sk_path.read_text().strip())
    sk = Ed25519PrivateKey.from_private_bytes(sk_raw)
    c = httpx.Client(base_url=HUB, timeout=30)

    print("[1] happy path")
    payload = fresh_payload(sk)
    r = c.post("/api/v1/auth/daemon-bridge", json=payload)
    expect(r, 200, "200 OK")
    body = r.json()
    assert "access_token" in body, body
    assert body["user"]["email"] == "bridge-test@digitorn.io", body
    print(f"  [OK]   token issued, created={body.get('created')}")
    token = body["access_token"]

    print("[2] /auth/me with bridged token")
    me = c.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"})
    expect(me, 200, "200 OK")
    assert me.json()["email"] == "bridge-test@digitorn.io", me.json()
    print(f"  [OK]   /me returned {me.json()['email']}")

    print("[3] replay same payload (nonce reuse)")
    r2 = c.post("/api/v1/auth/daemon-bridge", json=payload)
    expect(r2, 409, "409 nonce already used")

    print("[4] tampered field, fresh nonce")
    bad = fresh_payload(sk)
    bad["email"] = "mallory@example.com"  # not part of signed bytes
    r3 = c.post("/api/v1/auth/daemon-bridge", json=bad)
    expect(r3, 401, "401 signature does not verify")

    print("[5] stale timestamp")
    stale = fresh_payload(sk, ts_offset=-3600)
    r4 = c.post("/api/v1/auth/daemon-bridge", json=stale)
    expect(r4, 400, "400 outside window")

    print("[6] unknown daemon name")
    bogus = fresh_payload(sk, daemon_name="ghost")
    r5 = c.post("/api/v1/auth/daemon-bridge", json=bogus)
    expect(r5, 401, "401 unknown or revoked daemon")

    print("\nALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(__doc__)
        sys.exit(1)
    raise SystemExit(main(Path(sys.argv[1])))
