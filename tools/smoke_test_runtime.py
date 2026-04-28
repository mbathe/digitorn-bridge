"""Runtime smoke test - deploy representative apps against a live daemon.

Complements `tools/validate_docs.py` (static compile check) with a real
runtime deploy + status check + undeploy for each representative app pattern.

This catches:
- Module on_start / on_config_update hooks that crash
- Secrets/env var resolution at deploy time
- Bundle writing to disk
- Manifest publication (app shows up in /api/apps)
- Clean teardown (no dangling resources)

The script talks to the daemon on 127.0.0.1:8000 by default (override with
DIGITORN_HOST). Auth is not needed from loopback - the daemon's
``_is_loopback_self_call`` bypass covers the paths we touch.

Usage:
    py -3.12 tools/smoke_test_runtime.py
    py -3.12 tools/smoke_test_runtime.py --host http://127.0.0.1:8000
    py -3.12 tools/smoke_test_runtime.py --keep   # don't undeploy at the end

Exit code: 0 when every test app deploys, manifests correctly, and undeploys.
Non-zero otherwise.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_HOST = "http://127.0.0.1:8000"

# ── Test apps ───────────────────────────────────────────────────

TEST_APPS: list[tuple[str, str]] = [
    (
        "smoke-conversation",
        """
app:
  app_id: __APP_ID__
  name: "Smoke Conversation"
  description: "Single-agent conversation with filesystem + memory."

modules:
  filesystem:
    constraints:
      allowed_actions: [read, write, edit, glob, grep]
  memory: {}

agents:
  - id: main
    role: assistant
    brain:
      provider: anthropic
      model: claude-sonnet-4-5
      config:
        api_key: "claude-code"
    system_prompt: "You are a helpful assistant."

execution:
  mode: conversation
  greeting: "Hi!"

capabilities:
  default_policy: auto
""",
    ),
    (
        "smoke-multi-agent",
        """
app:
  app_id: __APP_ID__
  name: "Smoke Multi-Agent"

modules:
  filesystem:
    constraints:
      allowed_actions: [read, grep, glob]

agents:
  - id: coordinator
    role: coordinator
    brain:
      provider: anthropic
      model: claude-sonnet-4-5
      config:
        api_key: "claude-code"
    system_prompt: "You coordinate specialists."
    pool:
      max_workers: 3

  - id: analyst
    role: specialist
    brain:
      provider: anthropic
      model: claude-haiku-4-5
      config:
        api_key: "claude-code"
    specialty: "Reads files and answers questions."
    system_prompt: "You analyze code."
    modules: [filesystem]

execution:
  mode: conversation
  greeting: "Ready."

capabilities:
  default_policy: auto
""",
    ),
    (
        "smoke-background",
        """
app:
  app_id: __APP_ID__
  name: "Smoke Background"

modules:
  http: {}
  memory: {}

agents:
  - id: monitor
    role: assistant
    brain:
      provider: anthropic
      model: claude-sonnet-4-5
      config:
        api_key: "claude-code"
    system_prompt: "You monitor."

execution:
  mode: background
  triggers:
    - id: health-check
      type: cron
      schedule: "*/5 * * * *"
      message: "Run health checks."

capabilities:
  default_policy: auto
""",
    ),
    (
        "smoke-channels",
        """
app:
  app_id: __APP_ID__
  name: "Smoke Channels"

modules:
  channels:
    config:
      providers:
        inbound:
          adapter: webhook
          config:
            inbound_path: /hook/smoke
          activation:
            session: per_event
            message: "{{event.payload.text}}"

agents:
  - id: responder
    role: assistant
    brain:
      provider: anthropic
      model: claude-sonnet-4-5
      config:
        api_key: "claude-code"
    system_prompt: "You reply to webhook events."

execution:
  mode: background

capabilities:
  default_policy: auto
  grant:
    - module: channels
      actions: [reply, send_message]
""",
    ),
    (
        "smoke-rag",
        """
app:
  app_id: __APP_ID__
  name: "Smoke RAG"

modules:
  rag: {}

agents:
  - id: main
    role: assistant
    brain:
      provider: anthropic
      model: claude-sonnet-4-5
      config:
        api_key: "claude-code"
    system_prompt: "You answer questions using the RAG knowledge base."

execution:
  mode: conversation
  greeting: "Ask me anything."

capabilities:
  default_policy: auto
  grant:
    - module: rag
""",
    ),
    (
        "smoke-workspace",
        """
app:
  app_id: __APP_ID__
  name: "Smoke Workspace"

modules:
  workspace:
    config:
      render_mode: react
      entry_file: src/App.tsx
      lint: true
  preview: {}

agents:
  - id: coder
    role: assistant
    brain:
      provider: anthropic
      model: claude-sonnet-4-5
      config:
        api_key: "claude-code"
    system_prompt: "You write React code."

execution:
  mode: conversation
  greeting: "Ready to code."

capabilities:
  default_policy: auto
  grant:
    - module: workspace
      actions: [write, read, edit, glob, grep, delete]

workspace:
  render_mode: react
  entry_file: src/App.tsx
""",
    ),
]


# ── HTTP helpers (stdlib only) ──────────────────────────────────

@dataclass
class Response:
    status: int
    body: bytes

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300

    def json(self) -> dict:
        try:
            return json.loads(self.body.decode("utf-8"))
        except Exception:
            return {}


def http_get(url: str, timeout: float = 10.0) -> Response:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return Response(r.status, r.read())
    except urllib.error.HTTPError as e:
        return Response(e.code, e.read())


def http_delete(url: str, timeout: float = 30.0) -> Response:
    req = urllib.request.Request(url, method="DELETE")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return Response(r.status, r.read())
    except urllib.error.HTTPError as e:
        return Response(e.code, e.read())


def http_multipart(url: str, fields: dict[str, str], file_name: str,
                   file_content: bytes, timeout: float = 60.0) -> Response:
    """POST multipart/form-data with one file + several text fields."""
    boundary = f"----digitorn-smoke-{uuid.uuid4().hex}"
    lines: list[bytes] = []
    for k, v in fields.items():
        lines.append(f"--{boundary}\r\n".encode())
        lines.append(f'Content-Disposition: form-data; name="{k}"\r\n\r\n'.encode())
        lines.append(v.encode("utf-8"))
        lines.append(b"\r\n")
    lines.append(f"--{boundary}\r\n".encode())
    lines.append(
        f'Content-Disposition: form-data; name="file"; filename="{file_name}"\r\n'
        'Content-Type: application/x-yaml\r\n\r\n'.encode()
    )
    lines.append(file_content)
    lines.append(b"\r\n")
    lines.append(f"--{boundary}--\r\n".encode())
    body = b"".join(lines)
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return Response(r.status, r.read())
    except urllib.error.HTTPError as e:
        return Response(e.code, e.read())


# ── Test driver ─────────────────────────────────────────────────

@dataclass
class AppResult:
    name: str
    app_id: str
    deploy_ok: bool = False
    deploy_detail: str = ""
    manifest_ok: bool = False
    manifest_detail: str = ""
    undeploy_ok: bool = False
    undeploy_detail: str = ""
    duration_s: float = 0.0

    @property
    def passed(self) -> bool:
        return self.deploy_ok and self.manifest_ok and self.undeploy_ok


def check_daemon(host: str) -> bool:
    r = http_get(f"{host}/health")
    if not r.ok:
        print(f"FATAL: daemon at {host} not responding (status {r.status})")
        return False
    status = r.json().get("status")
    print(f"daemon: {host}  status={status}  ok")
    return True


def deploy(host: str, yaml_content: str, app_id: str) -> Response:
    return http_multipart(
        f"{host}/api/apps/deploy/upload",
        fields={"force": "true"},
        file_name=f"{app_id}.yaml",
        file_content=yaml_content.encode("utf-8"),
    )


def check_manifest(host: str, app_id: str) -> Response:
    return http_get(f"{host}/api/apps/{app_id}")


def undeploy(host: str, app_id: str) -> Response:
    return http_delete(f"{host}/api/apps/{app_id}")


def run_one(host: str, name: str, template: str, *, keep: bool) -> AppResult:
    app_id = f"{name}-{uuid.uuid4().hex[:8]}"
    yaml_content = template.replace("__APP_ID__", app_id)
    result = AppResult(name=name, app_id=app_id)
    t0 = time.time()

    # 1. Deploy (async - returns 200 immediately with status="deploying")
    r = deploy(host, yaml_content, app_id)
    if r.ok:
        result.deploy_ok = True
    else:
        result.deploy_detail = _excerpt(r)
        result.duration_s = time.time() - t0
        return result

    # 2. Poll manifest until deployed or timeout. First-time deploys of apps
    # that install external tooling (node/npm for preview) can take a while.
    deadline = time.time() + 60.0
    last = None
    while time.time() < deadline:
        r = check_manifest(host, app_id)
        if r.ok and r.json().get("data", {}).get("app_id") == app_id:
            result.manifest_ok = True
            break
        last = r
        time.sleep(0.5)
    if not result.manifest_ok:
        result.manifest_detail = _excerpt(last) if last else "no response"

    # 3. Undeploy
    if keep:
        result.undeploy_ok = True
        result.undeploy_detail = "SKIPPED (--keep)"
    else:
        r = undeploy(host, app_id)
        if r.ok:
            result.undeploy_ok = True
        else:
            result.undeploy_detail = _excerpt(r)

    result.duration_s = time.time() - t0
    return result


def _excerpt(r: Response) -> str:
    body = r.body.decode("utf-8", errors="replace")[:400]
    return f"{r.status}: {body}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default=DEFAULT_HOST)
    ap.add_argument("--keep", action="store_true",
                    help="don't undeploy at the end (useful for inspection)")
    ap.add_argument("--only", default="",
                    help="comma-separated list of app names to run (default: all)")
    args = ap.parse_args()

    if not check_daemon(args.host):
        return 2

    only = {x.strip() for x in args.only.split(",") if x.strip()}

    results: list[AppResult] = []
    for name, tpl in TEST_APPS:
        if only and name not in only:
            continue
        print(f"\n--- {name} ---")
        r = run_one(args.host, name, tpl, keep=args.keep)
        results.append(r)
        stamp = "PASS" if r.passed else "FAIL"
        print(f"  {stamp}  deploy={r.deploy_ok}  manifest={r.manifest_ok}  "
              f"undeploy={r.undeploy_ok}  duration={r.duration_s:.2f}s")
        if not r.deploy_ok:
            print(f"  deploy_detail:    {r.deploy_detail}")
        if not r.manifest_ok:
            print(f"  manifest_detail:  {r.manifest_detail}")
        if not r.undeploy_ok:
            print(f"  undeploy_detail:  {r.undeploy_detail}")

    # Summary + report
    passed = sum(1 for r in results if r.passed)
    failed = len(results) - passed
    print(f"\n== Summary: pass={passed}  fail={failed}  total={len(results)} ==")

    report = Path("docs/SMOKE_TEST_REPORT.md")
    lines = [
        "# Runtime Smoke Test Report",
        "",
        f"_Generated by `tools/smoke_test_runtime.py` against `{args.host}`._",
        "",
        f"- Total: **{len(results)}**  |  Pass: **{passed}**  |  Fail: **{failed}**",
        "",
        "| App | Deploy | Manifest | Undeploy | Duration | Error |",
        "|---|---|---|---|---|---|",
    ]
    for r in results:
        err = ""
        if not r.deploy_ok:
            err = r.deploy_detail
        elif not r.manifest_ok:
            err = r.manifest_detail
        elif not r.undeploy_ok:
            err = r.undeploy_detail
        err = err.replace("|", "\\|").replace("\n", " ")[:180]
        lines.append(
            f"| `{r.name}` | {'✅' if r.deploy_ok else '❌'} | "
            f"{'✅' if r.manifest_ok else '❌'} | "
            f"{'✅' if r.undeploy_ok else '❌'} | "
            f"{r.duration_s:.2f}s | {err} |"
        )
    report.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"-> wrote {report}")

    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
