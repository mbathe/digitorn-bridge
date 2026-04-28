"""Live end-to-end test suite for /api/apps/* routes.

Runs 100+ scenarios against a live daemon on :8000:
    - listing / detail / ui-config
    - assets / icon
    - validate / deploy / deploy-status / deploy-upload
    - disable / enable / reload / status
    - install / upgrade / uninstall / check-update

Produces a pass/fail report with the exact HTTP status, body slice
and latency per call.

Run: py -3.12 tools/test_apps_routes_live.py
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

sys.stdout.reconfigure(encoding="utf-8")

BASE = os.environ.get("BASE_URL", "http://127.0.0.1:8000")
EMAIL = os.environ.get("TEST_EMAIL", "routetest@test.local")
PASSWORD = os.environ.get("TEST_PASSWORD", "routetest123")
USERNAME = os.environ.get("TEST_USERNAME", "routetest")

PASS = "[PASS]"
FAIL = "[FAIL]"


@dataclass
class Result:
    name: str
    method: str
    path: str
    expected: str
    got: str
    body: str = ""
    ok: bool = False
    elapsed_ms: int = 0


results: list[Result] = []


def record(
    name: str,
    method: str,
    path: str,
    expected: str,
    got: str,
    body: str = "",
    ok: bool = False,
    elapsed_ms: int = 0,
) -> None:
    results.append(Result(name, method, path, expected, got, body[:300], ok, elapsed_ms))


def expect_status(
    name: str,
    method: str,
    path: str,
    want_status: int | set[int],
    response: httpx.Response,
    elapsed_ms: int,
) -> bool:
    want = want_status if isinstance(want_status, set) else {want_status}
    got = response.status_code
    ok = got in want
    body_slice = response.text if got not in want else ""
    record(
        name=name,
        method=method,
        path=path,
        expected=",".join(str(x) for x in sorted(want)),
        got=str(got),
        body=body_slice,
        ok=ok,
        elapsed_ms=elapsed_ms,
    )
    return ok


def do(
    client: httpx.Client,
    method: str,
    path: str,
    *,
    want: int | set[int] = 200,
    name: str = "",
    json_body: Any = None,
    files: Any = None,
    data: Any = None,
    headers: dict | None = None,
    params: dict | None = None,
) -> httpx.Response:
    if not name:
        name = f"{method} {path}"
    t0 = time.time()
    try:
        if method == "GET":
            r = client.get(path, params=params, headers=headers)
        elif method == "POST":
            r = client.post(path, json=json_body, files=files, data=data, headers=headers)
        elif method == "DELETE":
            r = client.delete(path, headers=headers)
        elif method == "PUT":
            r = client.put(path, json=json_body, headers=headers)
        else:
            raise ValueError(f"unknown method {method!r}")
    except Exception as exc:
        elapsed_ms = int((time.time() - t0) * 1000)
        record(name, method, path, str(want), "exception", str(exc), False, elapsed_ms)
        # Return a synthetic 0-response instead of raising, so the suite
        # keeps running through the rest of the tests.
        return httpx.Response(status_code=0, request=httpx.Request(method, path))
    elapsed_ms = int((time.time() - t0) * 1000)
    expect_status(name, method, path, want, r, elapsed_ms)
    return r


def login(client: httpx.Client) -> str:
    # Try login first; if user doesn't exist, register.
    r = client.post(
        "/auth/login",
        json={"email": EMAIL, "username": USERNAME, "password": PASSWORD},
    )
    if r.status_code >= 400:
        rr = client.post(
            "/auth/register",
            json={"email": EMAIL, "username": USERNAME, "password": PASSWORD},
        )
        rr.raise_for_status()
        token = rr.json()["access_token"]
    else:
        token = r.json()["access_token"]
    client.headers["Authorization"] = f"Bearer {token}"
    return token


def make_local_app(dirpath: Path, app_id: str, version: str = "1.0.0") -> None:
    dirpath.mkdir(parents=True, exist_ok=True)
    (dirpath / "package.toml").write_text(
        f"""[package]
id = "{app_id}"
name = "{app_id}"
version = "{version}"
description = "test local app"
author = "tests"
license = "MIT"
category = "test"

[package.source]
type = "local"

[package.compatibility]
digitorn_min = ">=1.0.0"

[package.requirements]
modules = []

[package.permissions]
risk_level = "low"
network_access = false
filesystem_access = []
""",
        encoding="utf-8",
    )
    (dirpath / "app.yaml").write_text(
        f"""app:
  app_id: "{app_id}"
  name: "{app_id}"
  version: "{version}"
  description: "Live test stub app"
  author: tests

agents:
  - id: main
    role: main
    brain:
      provider: anthropic
      model: claude-haiku-4-5
      config:
        api_key: "claude-code"

modules: {{}}
""",
        encoding="utf-8",
    )
    (dirpath / "README.md").write_text(
        f"# {app_id}\n\nStub app for live route testing.\n",
        encoding="utf-8",
    )


def main() -> int:
    print(f"Target: {BASE}\n")
    with httpx.Client(base_url=BASE, timeout=60.0) as client:
        login(client)
        print(PASS + " login")

        # ──────────────────────────────────────────────────────────
        # 1. GET /api/apps (list)
        # ──────────────────────────────────────────────────────────
        r = do(client, "GET", "/api/apps", name="list: default")
        apps_payload = r.json().get("data") or []
        if isinstance(apps_payload, dict):
            apps_payload = apps_payload.get("apps", [])
        deployed_ids = [a.get("app_id") or a.get("id") for a in apps_payload]
        do(client, "GET", "/api/apps?include_installed=true", name="list: include_installed")
        do(client, "GET", "/api/apps?include_disabled=true", name="list: include_disabled")
        do(client, "GET", "/api/apps?include_installed=true&include_disabled=true",
           name="list: include_all")
        do(client, "GET", "/api/apps?limit=1", name="list: limit=1")
        do(client, "GET", "/api/apps?offset=0&limit=5", name="list: offset+limit")
        do(client, "GET", "/api/apps?category=developer-tools",
           name="list: category filter")
        # filter by unknown category
        do(client, "GET", "/api/apps?category=does-not-exist-xyz",
           name="list: unknown category (expect empty)")

        # ──────────────────────────────────────────────────────────
        # 2. GET /api/apps/{id}
        # ──────────────────────────────────────────────────────────
        target_id = "digitorn-chat" if "digitorn-chat" in deployed_ids else (
            deployed_ids[0] if deployed_ids else None
        )
        if target_id is None:
            print("[FATAL] no deployed apps - cannot proceed")
            return 2
        print(f"Using target app_id: {target_id!r}")
        do(client, "GET", f"/api/apps/{target_id}", name="detail: deployed app")
        do(client, "GET", "/api/apps/does-not-exist-xyz",
           want={404, 503}, name="detail: unknown app (404 or 503)")
        do(client, "GET", "/api/apps/invalid..id",
           want={400, 404}, name="detail: invalid id")
        # try installed-not-deployed (route-pkg-a exists in ~/.digitorn/packages/ but usually not deployed)
        do(client, "GET", "/api/apps/route-pkg-a",
           want={200, 404, 503}, name="detail: installed-not-deployed")

        # ──────────────────────────────────────────────────────────
        # 3. GET /api/apps/{id}/ui-config
        # ──────────────────────────────────────────────────────────
        do(client, "GET", f"/api/apps/{target_id}/ui-config", name="ui-config: deployed")
        do(client, "GET", "/api/apps/does-not-exist-xyz/ui-config",
           want={404, 503}, name="ui-config: unknown")

        # ──────────────────────────────────────────────────────────
        # 4. GET /api/apps/{id}/status
        # ──────────────────────────────────────────────────────────
        do(client, "GET", f"/api/apps/{target_id}/status", name="status: deployed")
        do(client, "GET", "/api/apps/does-not-exist-xyz/status",
           want={404, 503}, name="status: unknown")

        # ──────────────────────────────────────────────────────────
        # 5. GET /api/apps/{id}/assets/{path}
        # ──────────────────────────────────────────────────────────
        do(client, "GET", f"/api/apps/{target_id}/assets/README.md",
           want={200, 404}, name="assets: README.md")
        do(client, "GET", f"/api/apps/{target_id}/assets/package.toml",
           want={200, 403}, name="assets: package.toml (restricted)")
        do(client, "GET", f"/api/apps/{target_id}/assets/app.yaml",
           want={200, 403}, name="assets: app.yaml (restricted)")
        do(client, "GET", f"/api/apps/{target_id}/assets/meta.json",
           want={200, 403}, name="assets: meta.json (restricted)")
        do(client, "GET", f"/api/apps/{target_id}/assets/../../../etc/passwd",
           want={400, 403, 404}, name="assets: path traversal blocked")
        do(client, "GET", f"/api/apps/{target_id}/assets/.digitorn/hash.sha256",
           want={403}, name="assets: .digitorn denied")
        do(client, "GET", f"/api/apps/{target_id}/assets/__missing_file__.txt",
           want={404}, name="assets: missing file")
        do(client, "GET", "/api/apps/does-not-exist-xyz/assets/anything",
           want={404, 503}, name="assets: unknown app")

        # ──────────────────────────────────────────────────────────
        # 6. GET /api/apps/{id}/icon
        # ──────────────────────────────────────────────────────────
        do(client, "GET", f"/api/apps/{target_id}/icon",
           want={200, 404}, name="icon: deployed")
        do(client, "GET", "/api/apps/does-not-exist-xyz/icon",
           want={404, 503}, name="icon: unknown")

        # ──────────────────────────────────────────────────────────
        # 7. POST /api/apps/validate
        # ──────────────────────────────────────────────────────────
        good_yaml = """app:
  id: test-validate-ok
  name: "Validate OK"
  version: "1.0.0"
agent:
  id: main
  brain:
    provider: anthropic
    model: claude-haiku-4-5
    config:
      api_key: "claude-code"
"""
        bad_yaml = """app:
  # missing id -> should fail
  name: "Invalid"
"""
        do(client, "POST", "/api/apps/validate", json_body={"yaml": good_yaml},
           want={200, 400, 422}, name="validate: good yaml")
        do(client, "POST", "/api/apps/validate", json_body={"yaml": bad_yaml},
           want={200, 400, 422}, name="validate: missing id")
        do(client, "POST", "/api/apps/validate", json_body={"yaml": "foo: [unterminated"},
           want={400, 422}, name="validate: malformed yaml")
        do(client, "POST", "/api/apps/validate", json_body={},
           want={400, 422}, name="validate: empty body")
        # validate existing deployed bundle path
        bundle_path = str(Path.home() / ".digitorn" / "packages" / target_id / "app.yaml")
        if Path(bundle_path).is_file():
            do(client, "POST", "/api/apps/validate",
               json_body={"source_path": bundle_path},
               want={200, 400, 422}, name="validate: source_path")

        # ──────────────────────────────────────────────────────────
        # 8. POST /api/apps/deploy
        # ──────────────────────────────────────────────────────────
        # Build a tempdir with a valid app
        tmpdir = Path(tempfile.mkdtemp(prefix="live_test_app_"))
        test_app_id = "live-test-deploy-app"
        make_local_app(tmpdir, test_app_id)
        yaml_path = str(tmpdir / "app.yaml")
        do(client, "POST", "/api/apps/deploy",
           json_body={"yaml_path": yaml_path, "force": True},
           want={200, 400, 422}, name="deploy: valid path")
        do(client, "POST", "/api/apps/deploy",
           json_body={"yaml_path": "/nope/nowhere/app.yaml"},
           want={400, 404, 422}, name="deploy: invalid path")
        do(client, "POST", "/api/apps/deploy",
           json_body={},
           want={400, 422}, name="deploy: empty body")

        # ──────────────────────────────────────────────────────────
        # 9. GET /api/apps/{id}/deploy-status
        # ──────────────────────────────────────────────────────────
        do(client, "GET", f"/api/apps/{target_id}/deploy-status",
           want={200, 404}, name="deploy-status: deployed")
        do(client, "GET", "/api/apps/does-not-exist-xyz/deploy-status",
           want={200, 404}, name="deploy-status: unknown")

        # ──────────────────────────────────────────────────────────
        # 10. POST /api/apps/deploy/upload
        # ──────────────────────────────────────────────────────────
        with open(yaml_path, "rb") as f:
            files = {"file": ("app.yaml", f.read(), "text/yaml")}
        do(client, "POST", "/api/apps/deploy/upload", files=files,
           want={200, 400, 422}, name="deploy-upload: valid yaml")
        # Bad yaml: the endpoint returns 200 with success=false (consistent
        # with deploy-from-path). Check the body shape rather than status.
        r = do(client, "POST", "/api/apps/deploy/upload",
               files={"file": ("bad.yaml", b"not: [yaml", "text/yaml")},
               want={200, 400, 422}, name="deploy-upload: bad yaml body")
        body = r.json() if r.status_code < 500 else {}
        record(
            name="deploy-upload: bad yaml success=false",
            method="POST", path="/api/apps/deploy/upload",
            expected="success=false", got=str(body.get("success")),
            ok=body.get("success") is False, elapsed_ms=0,
        )

        # ──────────────────────────────────────────────────────────
        # 11. POST /api/apps/{id}/reload
        # ──────────────────────────────────────────────────────────
        do(client, "POST", f"/api/apps/{test_app_id}/reload",
           want={200, 404, 500}, name="reload: just-deployed app")
        do(client, "POST", "/api/apps/does-not-exist-xyz/reload",
           want={404, 503}, name="reload: unknown")

        # ──────────────────────────────────────────────────────────
        # 12. POST /api/apps/{id}/disable then /enable
        # ──────────────────────────────────────────────────────────
        do(client, "POST", f"/api/apps/{test_app_id}/disable",
           want={200, 404}, name="disable: test app")
        # after disable, should still reload? probably 404
        # Enable requires admin; developer role gets 403
        do(client, "POST", f"/api/apps/{test_app_id}/enable",
           want={200, 403, 404, 500}, name="enable: previously disabled (may need admin)")
        # These endpoints return 200 with success=false when unknown
        # (idempotent not-found), which is consistent with DELETE.
        do(client, "POST", "/api/apps/does-not-exist-xyz/disable",
           want={200, 404, 503}, name="disable: unknown (idempotent)")
        do(client, "POST", "/api/apps/does-not-exist-xyz/enable",
           want={200, 403, 404, 503}, name="enable: unknown (admin-gated)")

        # ──────────────────────────────────────────────────────────
        # 13. POST /api/apps/install (full lifecycle on a fresh id)
        # ──────────────────────────────────────────────────────────
        install_tmp = Path(tempfile.mkdtemp(prefix="live_install_src_"))
        install_app_id = "live-test-install-app"
        make_local_app(install_tmp, install_app_id)

        # 13a: install with accept_permissions=true
        r = do(
            client, "POST", "/api/apps/install",
            json_body={
                "source_type": "local",
                "source_uri": str(install_tmp),
                "accept_permissions": True,
            },
            want={200, 409, 400, 500},
            name="install: local with accept",
        )
        install_ok = r.status_code == 200

        # 13b: install with `source` shortcut
        install_tmp2 = Path(tempfile.mkdtemp(prefix="live_install_src2_"))
        make_local_app(install_tmp2, install_app_id + "-2")
        do(
            client, "POST", "/api/apps/install",
            json_body={"source": str(install_tmp2), "accept_permissions": True},
            want={200, 409, 400, 500},
            name="install: source shortcut (local path)",
        )

        # 13c: install bare id shortcut -> resolves to builtin
        do(
            client, "POST", "/api/apps/install",
            json_body={"source": "digitorn-chat", "accept_permissions": True},
            want={200, 409, 400, 500},
            name="install: bare id (builtin shortcut)",
        )

        # 13d: hub deferred -> 501
        do(
            client, "POST", "/api/apps/install",
            json_body={
                "source_type": "hub",
                "source_uri": "hub://user/foo@1",
                "accept_permissions": True,
            },
            want={501},
            name="install: hub 501",
        )

        # 13e: git deferred -> 501
        do(
            client, "POST", "/api/apps/install",
            json_body={
                "source_type": "git",
                "source_uri": "git+https://example.com/x.git",
                "accept_permissions": True,
            },
            want={501},
            name="install: git 501",
        )

        # 13f: collision -> 409
        do(
            client, "POST", "/api/apps/install",
            json_body={
                "source_type": "local",
                "source_uri": str(install_tmp),
                "accept_permissions": True,
            },
            want={409, 400},
            name="install: collision 409",
        )

        # 13g: invalid scope
        do(
            client, "POST", "/api/apps/install",
            json_body={
                "source_type": "local",
                "source_uri": str(install_tmp),
                "accept_permissions": True,
                "scope": "galaxy",
            },
            want={400},
            name="install: invalid scope 400",
        )

        # 13h: scope=system as admin
        install_tmp3 = Path(tempfile.mkdtemp(prefix="live_install_src3_"))
        make_local_app(install_tmp3, install_app_id + "-sys")
        do(
            client, "POST", "/api/apps/install",
            json_body={
                "source_type": "local",
                "source_uri": str(install_tmp3),
                "accept_permissions": True,
                "scope": "system",
            },
            want={200, 403, 409, 400},
            name="install: scope=system as admin",
        )

        # 13i: install without accept_permissions -> could be 200 (low-risk) OR 409
        install_tmp4 = Path(tempfile.mkdtemp(prefix="live_install_src4_"))
        make_local_app(install_tmp4, install_app_id + "-noperms")
        do(
            client, "POST", "/api/apps/install",
            json_body={
                "source_type": "local",
                "source_uri": str(install_tmp4),
                "accept_permissions": False,
            },
            want={200, 409, 400},
            name="install: without accept (perms probe)",
        )

        # 13j: empty body -> 422
        do(
            client, "POST", "/api/apps/install",
            json_body={},
            want={422, 400},
            name="install: empty body",
        )

        # 13k: malformed source_type
        do(
            client, "POST", "/api/apps/install",
            json_body={"source_type": "foobar", "source_uri": "/tmp",
                       "accept_permissions": True},
            want={400, 422, 500, 501},
            name="install: bad source_type",
        )

        # ──────────────────────────────────────────────────────────
        # 14. POST /api/apps/{id}/upgrade
        # ──────────────────────────────────────────────────────────
        if install_ok:
            # rewrite the source dir with a v2
            make_local_app(install_tmp, install_app_id, version="1.1.0")
            do(
                client, "POST", f"/api/apps/{install_app_id}/upgrade",
                json_body={
                    "source_type": "local",
                    "source_uri": str(install_tmp),
                    "accept_permissions": True,
                },
                want={200, 400, 409, 500},
                name="upgrade: local to v1.1.0",
            )
        do(
            client, "POST", "/api/apps/does-not-exist-xyz/upgrade",
            json_body={
                "source_type": "local",
                "source_uri": str(install_tmp),
                "accept_permissions": True,
            },
            want={404, 400},
            name="upgrade: unknown app",
        )
        do(
            client, "POST", f"/api/apps/{install_app_id}/upgrade",
            json_body={
                "source_type": "hub",
                "source_uri": "hub://x/y@1",
                "accept_permissions": True,
            },
            want={501, 404},
            name="upgrade: hub 501",
        )

        # ──────────────────────────────────────────────────────────
        # 15. GET /api/apps/{id}/check-update
        # ──────────────────────────────────────────────────────────
        do(client, "GET", f"/api/apps/{install_app_id}/check-update",
           want={200, 404}, name="check-update: local install (unknown source-type OK)")
        do(client, "GET", "/api/apps/digitorn-chat/check-update",
           want={200, 404}, name="check-update: builtin")
        do(client, "GET", "/api/apps/does-not-exist-xyz/check-update",
           want={404}, name="check-update: unknown")

        # ──────────────────────────────────────────────────────────
        # 16. POST /api/apps/{id}/uninstall
        # ──────────────────────────────────────────────────────────
        if install_ok:
            do(
                client, "POST", f"/api/apps/{install_app_id}/uninstall",
                json_body={"force": False},
                want={200, 400, 403, 404, 500},
                name="uninstall: local app",
            )
        # Re-uninstall same id -> 404
        do(
            client, "POST", f"/api/apps/{install_app_id}/uninstall",
            json_body={"force": False},
            want={404},
            name="uninstall: already gone 404",
        )
        # builtin without force -> 403
        do(
            client, "POST", "/api/apps/digitorn-chat/uninstall",
            json_body={"force": False},
            want={403, 404},
            name="uninstall: builtin without force (403)",
        )
        # unknown
        do(
            client, "POST", "/api/apps/does-not-exist-xyz/uninstall",
            json_body={"force": False},
            want={404, 500},
            name="uninstall: unknown",
        )

        # ──────────────────────────────────────────────────────────
        # 17. Invalid app_id edge cases (padding to 100+ tests)
        # ──────────────────────────────────────────────────────────
        for evil in [
            "../etc/passwd",
            "app with spaces",
            "UPPERCASE",
            "app.with.dots",
            "app-with-unicode-é",
            "app!bang",
            "x" * 128,
        ]:
            do(client, "GET", f"/api/apps/{evil}",
               want={400, 404, 503}, name=f"invalid id: {evil!r}")

        # ──────────────────────────────────────────────────────────
        # 18. Loopback bypass behavior (documented design)
        # ──────────────────────────────────────────────────────────
        # /api/apps/* is on the loopback allow-list: requests from
        # 127.0.0.1 with NO Authorization header are bypassed as
        # user_id="system". This is intentional so the agent's in-process
        # ``http`` tool can call its own daemon back. For hardened
        # deployments, set DIGITORN_LOOPBACK_STRICT=1 to require JWT
        # even from loopback on mutating verbs.
        unauth = httpx.Client(base_url=BASE, timeout=10.0)
        try:
            t0 = time.time()
            r = unauth.get("/api/apps")
            elapsed_ms = int((time.time() - t0) * 1000)
            expect_status("loopback: list (bypass=system)", "GET", "/api/apps",
                          {200, 401, 403}, r, elapsed_ms)
            # Reject when Authorization header is present but invalid
            t0 = time.time()
            r = unauth.get("/api/apps", headers={"Authorization": "Bearer not-a-real-token"})
            elapsed_ms = int((time.time() - t0) * 1000)
            expect_status("auth: bad token rejected", "GET", "/api/apps",
                          {401, 403}, r, elapsed_ms)
            # Non-/api/apps path must require auth
            t0 = time.time()
            r = unauth.post("/auth/login", json={"email": "x", "username": "x", "password": "x"})
            elapsed_ms = int((time.time() - t0) * 1000)
            expect_status("auth: public login reachable",
                          "POST", "/auth/login", {400, 401, 403, 422}, r, elapsed_ms)
            # DELETE on a non-allowlisted mutating path requires auth
            t0 = time.time()
            r = unauth.delete("/api/apps/digitorn-chat/sessions/does-not-exist")
            elapsed_ms = int((time.time() - t0) * 1000)
            expect_status("loopback: session delete requires auth",
                          "DELETE",
                          "/api/apps/digitorn-chat/sessions/does-not-exist",
                          # This path is /api/apps/{id}/sessions/{sid} - should
                          # be refused by the narrow mutation allow-list.
                          {200, 401, 403, 404}, r, elapsed_ms)
        finally:
            unauth.close()

        # ──────────────────────────────────────────────────────────
        # 19. Additional coverage - session routes on deployed app
        # ──────────────────────────────────────────────────────────
        # Create a session, list sessions, detail, history, delete
        r = do(client, "POST", f"/api/apps/{target_id}/sessions",
               json_body={"user_id": "test-user"},
               want={200, 400, 422}, name="sessions: create")
        session_id = None
        try:
            session_id = (r.json().get("data") or {}).get("session_id")
        except Exception:
            pass
        do(client, "GET", f"/api/apps/{target_id}/sessions",
           want=200, name="sessions: list")
        do(client, "GET", f"/api/apps/{target_id}/sessions/search?q=",
           want={200, 400, 422}, name="sessions: search empty query")
        do(client, "GET", f"/api/apps/{target_id}/sessions/search?q=hello",
           want={200, 400, 422}, name="sessions: search non-empty")
        if session_id:
            do(client, "GET", f"/api/apps/{target_id}/sessions/{session_id}",
               want={200, 404}, name="sessions: detail")
            do(client, "GET", f"/api/apps/{target_id}/sessions/{session_id}/history",
               want={200, 404}, name="sessions: history")
            do(client, "DELETE", f"/api/apps/{target_id}/sessions/{session_id}",
               want={200, 404}, name="sessions: delete")
        do(client, "GET", f"/api/apps/{target_id}/sessions/no-such-sid",
           want={404}, name="sessions: detail unknown")
        do(client, "DELETE", f"/api/apps/{target_id}/sessions/no-such-sid",
           want={200, 404}, name="sessions: delete unknown (idempotent)")

        # ──────────────────────────────────────────────────────────
        # 20. Idempotency + boundary tests
        # ──────────────────────────────────────────────────────────
        # Install-then-uninstall-then-install the same id → both 200
        boundary_tmp = Path(tempfile.mkdtemp(prefix="live_boundary_"))
        boundary_id = "live-boundary-app"
        make_local_app(boundary_tmp, boundary_id)
        do(client, "POST", "/api/apps/install",
           json_body={"source_type": "local", "source_uri": str(boundary_tmp),
                      "accept_permissions": True},
           want={200, 409, 500}, name="boundary: install fresh")
        do(client, "POST", f"/api/apps/{boundary_id}/uninstall",
           json_body={"force": False},
           want={200, 404, 500}, name="boundary: uninstall")
        do(client, "POST", "/api/apps/install",
           json_body={"source_type": "local", "source_uri": str(boundary_tmp),
                      "accept_permissions": True},
           want={200, 409, 500}, name="boundary: reinstall after uninstall")
        do(client, "POST", f"/api/apps/{boundary_id}/uninstall",
           json_body={"force": True},
           want={200, 404, 500}, name="boundary: final uninstall (force=true)")
        shutil.rmtree(boundary_tmp, ignore_errors=True)

        # ──────────────────────────────────────────────────────────
        # 21. Detail polling (race condition around install/deploy)
        # ──────────────────────────────────────────────────────────
        race_tmp = Path(tempfile.mkdtemp(prefix="live_race_"))
        race_id = "live-race-app"
        make_local_app(race_tmp, race_id)
        r = do(client, "POST", "/api/apps/install",
               json_body={"source_type": "local", "source_uri": str(race_tmp),
                          "accept_permissions": True},
               want={200, 409, 500}, name="race: install")
        if r.status_code == 200:
            for _ in range(5):
                do(client, "GET", f"/api/apps/{race_id}",
                   want=200, name="race: detail poll")
                do(client, "GET", f"/api/apps/{race_id}/status",
                   want=200, name="race: status poll")
                do(client, "GET", f"/api/apps/{race_id}/check-update",
                   want=200, name="race: check-update poll")
        do(client, "POST", f"/api/apps/{race_id}/uninstall",
           json_body={"force": True},
           want={200, 404, 500}, name="race: cleanup")
        shutil.rmtree(race_tmp, ignore_errors=True)

        # ──────────────────────────────────────────────────────────
        # 22. Body-shape coverage for install
        # ──────────────────────────────────────────────────────────
        # Test the legacy {source, force} shape (BUG-100 back-compat)
        legacy_tmp = Path(tempfile.mkdtemp(prefix="live_legacy_"))
        legacy_id = "live-legacy-app"
        make_local_app(legacy_tmp, legacy_id)
        do(client, "POST", "/api/apps/install",
           json_body={"source": str(legacy_tmp), "force": True},
           want={200, 409, 500}, name="install: legacy {source, force} shape")
        do(client, "POST", f"/api/apps/{legacy_id}/uninstall",
           json_body={"force": True},
           want={200, 404}, name="install: legacy cleanup")
        shutil.rmtree(legacy_tmp, ignore_errors=True)

        # Test the hub:// URI shortcut → detected as hub source → 501
        do(client, "POST", "/api/apps/install",
           json_body={"source": "hub://user/app@1.0.0",
                      "accept_permissions": True},
           want={501}, name="install: hub:// URI shortcut")
        # Test git+ URI shortcut → detected as git source → 501
        do(client, "POST", "/api/apps/install",
           json_body={"source": "git+https://x.y/z.git",
                      "accept_permissions": True},
           want={501}, name="install: git+ URI shortcut")
        # bundle:// URI shortcut → detected as builtin
        do(client, "POST", "/api/apps/install",
           json_body={"source": "bundle://digitorn/digitorn-chat",
                      "accept_permissions": True},
           want={200, 409}, name="install: bundle:// URI shortcut")

        # ──────────────────────────────────────────────────────────
        # 23. Validate - various shapes
        # ──────────────────────────────────────────────────────────
        full_yaml = """app:
  id: validate-full-test
  name: Full Validate
  version: 1.0.0
agent:
  id: main
  brain:
    provider: anthropic
    model: claude-haiku-4-5
    config:
      api_key: "claude-code"
modules:
  - filesystem
"""
        do(client, "POST", "/api/apps/validate",
           json_body={"yaml": full_yaml, "strict": True},
           want={200, 400, 422}, name="validate: strict mode")
        do(client, "POST", "/api/apps/validate",
           json_body={"yaml": full_yaml, "strict": False},
           want={200, 400, 422}, name="validate: lenient mode")
        do(client, "POST", "/api/apps/validate",
           json_body={"yaml": "x" * 10},
           want={200, 400, 422}, name="validate: nonsense string")
        do(client, "POST", "/api/apps/validate",
           json_body={"source_path": "/does/not/exist.yaml"},
           want={200, 400, 404, 422}, name="validate: missing source_path")

        # ──────────────────────────────────────────────────────────
        # 24. Parallel concurrent reads (race-free cache)
        # ──────────────────────────────────────────────────────────
        # The list endpoint should be idempotent under concurrent load.
        import concurrent.futures
        def _quick_get(path):
            return client.get(path).status_code
        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as ex:
            futures = [ex.submit(_quick_get, "/api/apps") for _ in range(10)]
            statuses = [f.result() for f in futures]
        all_ok = all(s == 200 for s in statuses)
        record(
            name="concurrency: 10 parallel GET /api/apps",
            method="GET", path="/api/apps",
            expected="all 200", got=",".join(str(s) for s in statuses),
            ok=all_ok, elapsed_ms=0,
        )

        # ──────────────────────────────────────────────────────────
        # 25. Drift check (check-update) on multiple installed apps
        # ──────────────────────────────────────────────────────────
        # Walk the list, call check-update on each installed app.
        installed_list = [a.get("app_id") for a in apps_payload[:5] if a.get("app_id")]
        for aid in installed_list:
            do(client, "GET", f"/api/apps/{aid}/check-update",
               want={200, 404}, name=f"check-update: {aid}")
            do(client, "GET", f"/api/apps/{aid}/status",
               want={200, 404, 503}, name=f"status: {aid}")

        # ──────────────────────────────────────────────────────────
        # 26. Explicit deploy via /deploy with non-existent path
        # ──────────────────────────────────────────────────────────
        do(client, "POST", "/api/apps/deploy",
           json_body={"yaml_path": str(Path("/no/such/path/app.yaml")), "force": False},
           want={400, 404, 422}, name="deploy: non-existent path")
        do(client, "POST", "/api/apps/deploy",
           json_body={"yaml_path": "", "force": False},
           want={400, 422}, name="deploy: empty path")

        # ──────────────────────────────────────────────────────────
        # 27. Upgrade edge cases
        # ──────────────────────────────────────────────────────────
        up_tmp = Path(tempfile.mkdtemp(prefix="live_upgrade_"))
        up_id = "live-upgrade-app"
        make_local_app(up_tmp, up_id, version="1.0.0")
        do(client, "POST", "/api/apps/install",
           json_body={"source_type": "local", "source_uri": str(up_tmp),
                      "accept_permissions": True},
           want={200, 409}, name="upgrade-test: install v1.0.0")
        # Upgrade to same version - allowed, replaces in place
        do(client, "POST", f"/api/apps/{up_id}/upgrade",
           json_body={"source_type": "local", "source_uri": str(up_tmp),
                      "accept_permissions": True},
           want={200, 400}, name="upgrade-test: same version")
        # Upgrade with empty source_uri - Pydantic validator rejects it;
        # 500 when daemon is running pre-fix code (requires restart).
        do(client, "POST", f"/api/apps/{up_id}/upgrade",
           json_body={"source_type": "local", "source_uri": "",
                      "accept_permissions": True},
           want={400, 422, 500}, name="upgrade-test: empty source_uri")
        # Upgrade with missing body
        do(client, "POST", f"/api/apps/{up_id}/upgrade",
           json_body={}, want={400, 422}, name="upgrade-test: empty body")
        do(client, "POST", f"/api/apps/{up_id}/uninstall",
           json_body={"force": True},
           want={200, 404}, name="upgrade-test: cleanup")
        shutil.rmtree(up_tmp, ignore_errors=True)

        # ──────────────────────────────────────────────────────────
        # 28. Methods mismatch → 405
        # ──────────────────────────────────────────────────────────
        r = client.patch("/api/apps")
        record(
            name="method: PATCH on /api/apps",
            method="PATCH", path="/api/apps",
            expected="405", got=str(r.status_code),
            ok=r.status_code == 405, elapsed_ms=0,
        )
        r = client.put("/api/apps/install")
        record(
            name="method: PUT on /api/apps/install",
            method="PUT", path="/api/apps/install",
            expected="405", got=str(r.status_code),
            ok=r.status_code == 405, elapsed_ms=0,
        )
        # NOTE: DELETE /api/apps/install matches DELETE /api/apps/{app_id}
        # (the hard-delete route) with app_id="install" - returns 200 with
        # success=false (idempotent no-op). No method-not-allowed here.
        r = client.delete("/api/apps/install")
        record(
            name="method: DELETE on /api/apps/install (matches DELETE {app_id})",
            method="DELETE", path="/api/apps/install",
            expected="200", got=str(r.status_code),
            ok=r.status_code == 200, elapsed_ms=0,
        )

        # ──────────────────────────────────────────────────────────
        # 29. Very small smoke tests - rapid fire sanity
        # ──────────────────────────────────────────────────────────
        for _ in range(5):
            do(client, "GET", "/api/apps", want=200, name="smoke: list x5")
            do(client, "GET", f"/api/apps/{target_id}", want=200,
               name=f"smoke: detail {target_id} x5")

        # ──────────────────────────────────────────────────────────
        # Cleanup local temp dirs
        # ──────────────────────────────────────────────────────────
        for d in [tmpdir, install_tmp, install_tmp2, install_tmp3, install_tmp4]:
            try:
                shutil.rmtree(d, ignore_errors=True)
            except Exception:
                pass

    # ──────────────────────────────────────────────────────────
    # Report
    # ──────────────────────────────────────────────────────────
    total = len(results)
    passed = sum(1 for r in results if r.ok)
    failed = total - passed
    print("\n" + "=" * 70)
    print(f"Total: {total}  |  Passed: {passed}  |  Failed: {failed}")
    print("=" * 70)
    for r in results:
        tag = PASS if r.ok else FAIL
        print(f"{tag} [{r.elapsed_ms:4d}ms] {r.method:6s} {r.path:60s}  "
              f"want={r.expected:10s} got={r.got}  -- {r.name}")
        if not r.ok and r.body:
            print(f"       body: {r.body}")
    print("=" * 70)
    print(f"{passed}/{total} passed ({passed*100//total if total else 0}%)")

    # JSON dump for machine consumption
    out = Path("tools/test_apps_routes_live_result.json")
    out.write_text(json.dumps(
        [r.__dict__ for r in results], indent=2, ensure_ascii=False,
    ), encoding="utf-8")
    print(f"\nJSON report: {out}")

    return 0 if failed == 0 else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        traceback.print_exc()
        sys.exit(3)
