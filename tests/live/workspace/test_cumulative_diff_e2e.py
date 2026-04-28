"""End-to-end reproducer: real daemon + workspace app + 10 WsEdits.

Spawns a fresh daemon (fresh DIGITORN_HOME to avoid migration blocker),
deploys a minimal workspace+preview app, creates a session, performs
several workspace writes/edits via the HTTP API, then reads
``GET /workspace`` and asserts the `resources.files` payload contains
cumulative `insertions_pending` / `deletions_pending` / `unified_diff_pending`.
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

YAML = """
app:
  app_id: audit-workspace
  name: Audit Workspace
  version: "1.0"

modules:
  memory: {}
  preview: {}
  workspace:
    config:
      auto_approve: false
      sync_to_disk: true
      lint: false

agents:
  - id: main
    role: worker
    brain:
      provider: deepseek
      model: deepseek-chat
      backend: openai_compat
      config:
        api_key: "{{env.DEEPSEEK_API_KEY}}"
      temperature: 0.0
      max_tokens: 256

execution:
  mode: conversation
  max_turns: 10
  timeout: 60
  workspace_mode: none
"""


def _read_env(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not path.exists():
        return out
    for raw in path.read_text(encoding="utf-8").splitlines():
        if "=" in raw and not raw.strip().startswith("#"):
            k, _, v = raw.partition("=")
            out[k.strip()] = v.strip().strip('"').strip("'")
    return out


async def _main(base: str) -> int:
    async with httpx.AsyncClient(timeout=30.0) as c:
        # Register + login to get a JWT for /workspace routes.
        r = await c.post(f"{base}/auth/register", json={
            "username": "auditor",
            "password": "auditpassword123!",
            "email": "audit@test.local",
        }, timeout=15)
        if r.status_code not in (200, 409):
            print(f"FAIL register: {r.status_code} {r.text[:200]}")
            return 1
        r = await c.post(f"{base}/auth/login", json={
            "username": "auditor",
            "password": "auditpassword123!",
        }, timeout=15)
        if r.status_code != 200:
            print(f"FAIL login: {r.status_code} {r.text[:200]}")
            return 1
        token = r.json()["access_token"]
        hdrs = {"Authorization": f"Bearer {token}"}
        print(f"auth token: {token[:24]}...")

        # Deploy
        yaml_path = Path(tempfile.mkstemp(suffix=".yaml", prefix="audit-ws-")[1])
        yaml_path.write_text(YAML, encoding="utf-8")
        r = await c.post(
            f"{base}/api/apps/deploy",
            json={"yaml_path": str(yaml_path), "force": True},
            headers=hdrs,
            timeout=30,
        )
        d = r.json()
        if not d.get("success"):
            print(f"FAIL deploy: {r.status_code} {r.text[:300]}")
            return 1
        app_id = d["data"]["app_id"]
        # Wait for deployment to finish (deploy + warming up)
        deployed = False
        for _ in range(60):
            s = await c.get(f"{base}/api/apps/{app_id}/deploy-status", headers=hdrs)
            data = s.json().get("data") or {}
            if data.get("deployed"):
                deployed = True
                break
            await asyncio.sleep(1.0)
        if not deployed:
            print("FAIL: app never reached deployed=true")
            return 1
        # Wait for daemon warming_up flag to clear
        for _ in range(30):
            h = await c.get(f"{base}/health")
            if not h.json().get("warming_up"):
                break
            await asyncio.sleep(1.0)
        print(f"deployed + warm: {app_id}")

        sid = f"audit-{uuid.uuid4().hex[:8]}"

        async def ws_put(path: str, content: str) -> dict:
            r = await c.put(
                f"{base}/api/apps/{app_id}/sessions/{sid}/workspace/files/{path}",
                json={"content": content, "auto_approve": False, "source": "test"},
                headers=hdrs,
                timeout=30,
            )
            return r.json()

        async def ws_snapshot() -> dict:
            r = await c.get(
                f"{base}/api/apps/{app_id}/sessions/{sid}/workspace",
                headers=hdrs,
            )
            return r.json()

        # 1) Initial write of 3 lines
        current = "line1\nline2\nline3\n"
        r = await ws_put("foo.py", current)
        if not r.get("success"):
            print(f"FAIL write: {r}")
            return 1

        # 2) 10 successive edits - each appends one more line
        for i in range(10):
            current = current + f"extra{i}\n"
            r = await ws_put("foo.py", current)
            if not r.get("success"):
                print(f"FAIL edit {i}: {r}")
                return 1

        # 3) Snapshot and inspect the file payload
        snap = await ws_snapshot()
        files = (snap.get("data") or {}).get("snapshot", {}).get(
            "resources", {}
        ).get("files", {})
        foo = files.get("foo.py")
        if foo is None:
            print(f"FAIL: foo.py not in workspace snapshot. Files: {list(files.keys())}")
            return 1

        ins_p = foo.get("insertions_pending", 0)
        del_p = foo.get("deletions_pending", 0)
        diff = foo.get("unified_diff_pending", "")

        print(f"After 1 write + 10 edits on new file foo.py:")
        print(f"  insertions_pending = {ins_p}")
        print(f"  deletions_pending  = {del_p}")
        print(f"  diff length        = {len(diff)} chars")
        print(f"  diff preview       = {diff[:200]!r}")
        print(f"  total_insertions   = {foo.get('total_insertions')}")
        print(f"  total_deletions    = {foo.get('total_deletions')}")
        print(f"  validation         = {foo.get('validation')}")

        failures: list[str] = []
        if ins_p < 3:
            failures.append(f"insertions_pending={ins_p} should be >= 3 (initial lines)")
        if not diff:
            failures.append("unified_diff_pending is EMPTY - Fix A broken")
        if "+" not in diff:
            failures.append("unified_diff_pending has no + lines")

        if failures:
            print("\nFAIL:")
            for f in failures:
                print(f"  - {f}")
            return 1
        print("\nPASS: workspace payload cumulative & diff non-empty")
        return 0


def main() -> int:
    env_vars = _read_env(ROOT / ".env")
    port = 8291
    home = tempfile.mkdtemp(prefix="dg-audit-ws-")
    env = dict(os.environ)
    env["DIGITORN_HOME"] = home
    env["DIGITORN_SERVER__AUTH_ENABLED"] = "true"  # need JWT so /workspace routes accept us
    env["DIGITORN_SERVER__RATE_LIMIT_RPM"] = "10000"
    env["DIGITORN_LOGGING__LEVEL"] = "warning"
    env["DIGITORN_SKIP_BUILTINS"] = "1"
    if "DEEPSEEK_API_KEY" in env_vars:
        env["DEEPSEEK_API_KEY"] = env_vars["DEEPSEEK_API_KEY"]

    log = Path(home) / "daemon.log"
    lfh = open(log, "w", encoding="utf-8")
    proc = subprocess.Popen(
        [sys.executable, "-m", "digitorn.core.server", "start",
         "--port", str(port), "--no-sandbox"],
        env=env, cwd=str(ROOT),
        stdout=lfh, stderr=subprocess.STDOUT,
    )
    try:
        base = f"http://127.0.0.1:{port}"
        deadline = time.monotonic() + 180.0
        ready = False
        while time.monotonic() < deadline:
            try:
                if httpx.get(f"{base}/health", timeout=2.0).status_code == 200:
                    ready = True
                    break
            except Exception:
                pass
            time.sleep(1.0)
        if not ready:
            tail = log.read_text(encoding="utf-8", errors="replace")[-3000:]
            print(f"FAIL: daemon not ready in 180s\nLog tail:\n{tail}")
            return 1
        print(f"daemon ready at {base}")
        return asyncio.run(_main(base))
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.kill()
        lfh.close()


if __name__ == "__main__":
    sys.exit(main())
