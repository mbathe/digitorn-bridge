"""Reproducer for ``tests/functional/test_03_chat.py`` failures.

Mirrors ``send_and_wait`` step by step against a fresh daemon with a
verbose log of every response. The goal is to pinpoint which step
goes wrong.
"""
from __future__ import annotations
import asyncio
import json
import os
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[3]


def _load_env() -> dict[str, str]:
    out: dict[str, str] = {}
    p = ROOT / ".env"
    if p.exists():
        for raw in p.read_text(encoding="utf-8").splitlines():
            if "=" in raw and not raw.strip().startswith("#"):
                k, _, v = raw.partition("=")
                out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def _wait_ready(base: str, timeout: float = 180.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if httpx.get(f"{base}/health", timeout=2.0).status_code == 200:
                return True
        except Exception:
            pass
        time.sleep(1.0)
    return False


async def _send_and_trace(client: httpx.AsyncClient, base: str, tok: str,
                          app_id: str, sid: str, msg: str) -> None:
    hdrs = {"Authorization": f"Bearer {tok}"} if tok else {}
    print(f"\n── sending {msg!r} to {app_id}/{sid} (auth={'on' if tok else 'off'}) ──")
    r = await client.post(
        f"{base}/api/apps/{app_id}/sessions/{sid}/messages",
        headers=hdrs,
        json={"message": msg, "workspace": ""},
        timeout=10,
    )
    print(f"[POST /messages] {r.status_code}")
    print(f"  body: {r.text[:300]}")
    if r.status_code >= 400:
        return

    deadline = time.monotonic() + 60.0
    tick = 0
    while time.monotonic() < deadline:
        tick += 1
        sr = await client.get(
            f"{base}/api/apps/{app_id}/sessions/{sid}",
            headers=hdrs,
            timeout=10,
        )
        sd = sr.json().get("data", {})
        is_active = sd.get("is_active")
        print(f"[tick {tick:>2}] GET /sessions/{{sid}} status={sr.status_code} "
              f"is_active={is_active!r} msgs={len(sd.get('messages', []) or [])}")
        if is_active is False:
            hr = await client.get(
                f"{base}/api/apps/{app_id}/sessions/{sid}/history",
                headers=hdrs,
                timeout=10,
            )
            hd = hr.json().get("data", {})
            print(f"[history] status={hr.status_code} "
                  f"messages={len(hd.get('messages', []) or [])} "
                  f"events={len(hd.get('events', []) or [])}")
            last_assistant = ""
            for m in reversed(hd.get("messages", []) or []):
                if m.get("role") == "assistant":
                    last_assistant = (m.get("content", "") or "")[:120]
                    break
            print(f"[last assistant] {last_assistant!r}")
            return
        await asyncio.sleep(1.0)
    print("[TIMEOUT] 60s reached without is_active=False")


async def _async_main(port: int, tok: str) -> None:
    base = f"http://127.0.0.1:{port}"
    async with httpx.AsyncClient(timeout=30.0) as c:
        # Deploy minimal.yaml (uses anthropic claude-code) and also
        # our audit-conversation.yaml (deepseek). We test both to
        # see whether the failure depends on the provider.
        for yaml_rel, label in [
            ("tests/live/route_audit/apps/audit-conversation.yaml", "deepseek"),
            ("tests/functional/apps/minimal.yaml", "claude-code"),
        ]:
            print(f"\n═══ provider={label} yaml={yaml_rel} ═══")
            yaml_abs = (ROOT / yaml_rel).resolve()
            hdrs = {"Authorization": f"Bearer {tok}"} if tok else {}
            r = await c.post(
                f"{base}/api/apps/deploy",
                headers=hdrs,
                json={"yaml_path": str(yaml_abs), "force": True},
                timeout=30.0,
            )
            d = r.json()
            if not d.get("success"):
                print(f"  deploy failed: {r.status_code} {r.text[:300]}")
                continue
            app_id = d["data"]["app_id"]
            # wait for deploy-status
            for _ in range(30):
                s = await c.get(
                    f"{base}/api/apps/{app_id}/deploy-status",
                    headers=hdrs,
                )
                if (s.json().get("data") or {}).get("deployed"):
                    break
                await asyncio.sleep(1.0)
            print(f"  deployed: {app_id}")
            sid = f"debug-{uuid.uuid4().hex[:8]}"
            await _send_and_trace(c, base, tok, app_id, sid, "PING")


def main() -> int:
    env_vars = _load_env()
    if not env_vars.get("DEEPSEEK_API_KEY"):
        print("FAIL: DEEPSEEK_API_KEY missing from .env")
        return 1
    port = 8290
    data_dir = tempfile.mkdtemp(prefix="dg-dbg-chat-")
    env = dict(os.environ)
    env["DIGITORN_HOME"] = data_dir
    env["DIGITORN_DISCOVERY__SKIP_EMBEDDINGS"] = "1"
    env["DEEPSEEK_API_KEY"] = env_vars["DEEPSEEK_API_KEY"]
    # Reproduce the functional test env exactly — auth DISABLED,
    # no user token, same high rate limit.
    env["DIGITORN_SERVER__AUTH_ENABLED"] = "false"
    env["DIGITORN_SERVER__RATE_LIMIT_RPM"] = "10000"
    print(f"[boot] port={port} data_dir={data_dir}")
    log_path = Path(data_dir) / "daemon.log"
    log_fh = open(log_path, "w", encoding="utf-8")
    proc = subprocess.Popen(
        [sys.executable, "-m", "digitorn.core.server", "start",
         "--port", str(port), "--no-sandbox"],
        env=env, cwd=str(ROOT),
        stdout=log_fh, stderr=subprocess.STDOUT,
    )
    print(f"[boot] log={log_path}")
    try:
        base = f"http://127.0.0.1:{port}"
        if not _wait_ready(base):
            print("FAIL: daemon not ready in 180s")
            return 1
        print(f"[boot] ready at {base}")
        # Probe: is auth really disabled?
        r = httpx.get(f"{base}/api/apps", timeout=5.0)
        print(f"[probe] GET /api/apps (no auth) → {r.status_code}")
        print(f"  body: {r.text[:200]}")

        # Auth disabled → no token, exactly like the functional
        # conftest's ``client`` / ``headers`` fixtures.
        asyncio.run(_async_main(port, ""))
        return 0
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5.0)
        except Exception:
            proc.kill()


if __name__ == "__main__":
    sys.exit(main())
