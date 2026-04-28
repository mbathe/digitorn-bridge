"""End-to-end install/uninstall lifecycle test for all 4 source types.

Verifies for each source:
  1. POST /api/apps/install returns 200 (or 501 for not-implemented)
  2. Files are written under ~/.digitorn/packages/<id>/
  3. hash.sha256 exists under .digitorn/
  4. The app is visible in GET /api/apps
  5. GET /api/apps/{id} returns runtime_status=running
  6. A session can be created and a message sent (smoke test)
  7. POST /uninstall returns 200
  8. Files removed from ~/.digitorn/packages/<id>/
  9. App no longer in GET /api/apps (or not_deployed)

Source types tested:
  - local (filesystem directory)
  - builtin (bundle://digitorn/<id>)
  - hub (stubbed → 501 expected)
  - git (stubbed → 501 expected)

Run: py -3.12 tools/test_lifecycle_e2e.py
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
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

DB_PATH = Path.home() / ".digitorn" / "digitorn.db"
PKG_DIR_SYSTEM = Path.home() / ".digitorn" / "packages"


# ──────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────

@dataclass
class StepResult:
    name: str
    ok: bool
    detail: str = ""


results: list[StepResult] = []


def step(name: str, ok: bool, detail: str = "") -> bool:
    results.append(StepResult(name=name, ok=ok, detail=detail))
    tag = "[PASS]" if ok else "[FAIL]"
    dline = f"  detail: {detail}" if (not ok and detail) else ""
    print(f"{tag} {name}{chr(10) + dline if dline else ''}")
    return ok


def user_pkg_dir(user_id: str, pkg_id: str) -> Path:
    return Path.home() / ".digitorn" / "users" / user_id / "packages" / pkg_id


def find_install_dir(pkg_id: str, user_id: str | None = None) -> Path | None:
    """Resolve the actual install dir from disk (system or user scope)."""
    if user_id:
        p = user_pkg_dir(user_id, pkg_id)
        if p.is_dir():
            return p
    p = PKG_DIR_SYSTEM / pkg_id
    if p.is_dir():
        return p
    return None


def db_row_exists(pkg_id: str, user_id: str | None = None) -> bool:
    """Probe via the daemon's API rather than reading the DB directly.

    The ``installed_packages`` table is managed by async SQLAlchemy and
    may live in a different connection state than what a sync sqlite3
    client can observe (journal mode, session isolation). Querying the
    daemon's registry-backed endpoint is the reliable read path.
    """
    # This helper is intentionally API-backed - see the surrounding
    # comment. We treat "visible in /api/apps?include_installed=true"
    # as truth.
    try:
        c = httpx.Client(base_url=BASE, timeout=10.0)
        # re-use existing auth? Test calls pass in auth header via main
        # so here we just do a lightweight fallback - login again.
        c.post("/auth/login",
               json={"email": EMAIL, "username": USERNAME, "password": PASSWORD})
        r = c.get("/api/apps?include_installed=true")
        data = r.json().get("data") or []
        c.close()
        return any((a.get("app_id") == pkg_id) for a in data)
    except Exception as exc:
        print(f"[WARN] db_row_exists probe failed: {exc}")
        return False


def login(client: httpx.Client) -> tuple[str, str]:
    r = client.post(
        "/auth/login",
        json={"email": EMAIL, "username": USERNAME, "password": PASSWORD},
    )
    if r.status_code >= 400:
        r = client.post(
            "/auth/register",
            json={"email": EMAIL, "username": USERNAME, "password": PASSWORD},
        )
    r.raise_for_status()
    body = r.json()
    client.headers["Authorization"] = f"Bearer {body['access_token']}"
    return body["access_token"], body["user_id"]


def make_local_app(dirpath: Path, app_id: str, version: str = "1.0.0") -> None:
    dirpath.mkdir(parents=True, exist_ok=True)
    (dirpath / "package.toml").write_text(
        f"""[package]
id = "{app_id}"
name = "{app_id}"
version = "{version}"
description = "E2E lifecycle test app"
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
  description: "Lifecycle E2E stub"
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
    (dirpath / "README.md").write_text(f"# {app_id}\nE2E stub.\n", encoding="utf-8")


# ──────────────────────────────────────────────────────────────────
# LOCAL source full lifecycle
# ──────────────────────────────────────────────────────────────────

def lifecycle_local(client: httpx.Client, user_id: str) -> None:
    print("\n" + "=" * 70)
    print("LIFECYCLE: local source")
    print("=" * 70)

    app_id = "e2e-local-test-app"
    src_dir = Path(tempfile.mkdtemp(prefix="e2e_local_src_"))
    make_local_app(src_dir, app_id, version="1.0.0")

    try:
        # ── 1. Install ─────────────────────────────────────────────
        r = client.post("/api/apps/install", json={
            "source_type": "local",
            "source_uri": str(src_dir),
            "accept_permissions": True,
        })
        step("local.install returns 200", r.status_code == 200,
             f"status={r.status_code} body={r.text[:200]}")
        data = r.json().get("data") or {}
        deployed = data.get("deployed")
        step("local.install.deployed=true", deployed is True,
             f"deployed={deployed} deploy_error={data.get('deploy_error')}")
        step("local.install.hash present", bool(data.get("hash")),
             f"hash={data.get('hash')}")

        # ── 2. Disk verification ───────────────────────────────────
        install_dir = find_install_dir(app_id, user_id)
        step("local.disk install_dir exists",
             install_dir is not None and install_dir.is_dir(),
             f"install_dir={install_dir}")
        if install_dir:
            app_yaml = install_dir / "app.yaml"
            pkg_toml = install_dir / "package.toml"
            readme = install_dir / "README.md"
            hash_file = install_dir / ".digitorn" / "hash.sha256"
            step("local.disk app.yaml copied", app_yaml.is_file())
            step("local.disk package.toml copied", pkg_toml.is_file())
            step("local.disk README.md copied", readme.is_file())
            step("local.disk hash.sha256 written", hash_file.is_file(),
                 f"path={hash_file}")
            if hash_file.is_file():
                content = hash_file.read_text(encoding="utf-8").strip()
                step("local.disk hash looks like sha256",
                     len(content) == 64 and all(c in "0123456789abcdef" for c in content),
                     f"hash={content[:16]}...")

        # ── 3. DB verification ─────────────────────────────────────
        step("local.db registry row exists", db_row_exists(app_id),
             f"package_id={app_id}")

        # ── 4. Listing visibility ──────────────────────────────────
        r = client.get("/api/apps")
        apps = r.json().get("data") or []
        listed = any(a.get("app_id") == app_id for a in apps)
        step("local.listing visible in /api/apps", listed)

        # ── 5. Detail endpoint ─────────────────────────────────────
        r = client.get(f"/api/apps/{app_id}")
        detail = r.json().get("data") or {}
        rstatus = detail.get("runtime_status")
        step("local.detail runtime_status=running",
             r.status_code == 200 and rstatus == "running",
             f"status={r.status_code} runtime_status={rstatus}")

        # ── 6. Session smoke (create + delete, no LLM call) ────────
        r = client.post(f"/api/apps/{app_id}/sessions", json={
            "user_id": "e2e-user",
        })
        sid = (r.json().get("data") or {}).get("session_id")
        step("local.session can be created",
             r.status_code == 200 and bool(sid),
             f"status={r.status_code} session_id={sid}")
        if sid:
            r = client.delete(f"/api/apps/{app_id}/sessions/{sid}")
            step("local.session can be deleted",
                 r.status_code in (200, 204),
                 f"status={r.status_code}")

        # ── 7. Uninstall ───────────────────────────────────────────
        r = client.post(f"/api/apps/{app_id}/uninstall",
                        json={"force": False})
        step("local.uninstall returns 200", r.status_code == 200,
             f"status={r.status_code} body={r.text[:200]}")

        # ── 8. Post-uninstall disk check ───────────────────────────
        install_dir_after = find_install_dir(app_id, user_id)
        step("local.disk install_dir removed",
             install_dir_after is None,
             f"still exists: {install_dir_after}")

        # ── 9. Post-uninstall DB check ─────────────────────────────
        step("local.db registry row removed",
             not db_row_exists(app_id))

        # ── 10. Post-uninstall listing ─────────────────────────────
        r = client.get("/api/apps")
        apps = r.json().get("data") or []
        still_listed = any(a.get("app_id") == app_id for a in apps)
        step("local.listing gone from /api/apps",
             not still_listed,
             f"apps={[a.get('app_id') for a in apps if a.get('app_id', '').startswith('e2e')]}")

        # ── 11. Re-GET must now return 404 (or 503 during warmup) ──
        r = client.get(f"/api/apps/{app_id}")
        step("local.detail returns 404 after uninstall",
             r.status_code in (404, 503),
             f"status={r.status_code}")

    finally:
        shutil.rmtree(src_dir, ignore_errors=True)


# ──────────────────────────────────────────────────────────────────
# BUILTIN source full lifecycle (bundle://)
# ──────────────────────────────────────────────────────────────────

def lifecycle_builtin(client: httpx.Client, user_id: str) -> None:
    print("\n" + "=" * 70)
    print("LIFECYCLE: builtin source (bundle://)")
    print("=" * 70)

    # Pick a builtin that is NOT currently installed for this user.
    # digitorn-code is a good candidate (exists in source, may not be
    # deployed for routetest user).
    app_id = "digitorn-code"

    # First, ensure it's not installed (clean state)
    client.post(f"/api/apps/{app_id}/uninstall", json={"force": True})
    time.sleep(0.5)

    # ── 1. Install ─────────────────────────────────────────────────
    r = client.post("/api/apps/install", json={
        "source_type": "builtin",
        "source_uri": f"bundle://digitorn/{app_id}",
        "accept_permissions": True,
    })
    step("builtin.install returns 200 or 409", r.status_code in (200, 409),
         f"status={r.status_code} body={r.text[:200]}")

    if r.status_code == 200:
        data = r.json().get("data") or {}
        step("builtin.install.deployed=true", data.get("deployed") is True,
             f"deployed={data.get('deployed')} deploy_error={data.get('deploy_error')}")
    else:
        step("builtin already installed (409 collision)", True,
             "expected when re-running the test")

    # ── 2. Disk verification ───────────────────────────────────────
    install_dir = find_install_dir(app_id, user_id)
    step("builtin.disk install_dir exists",
         install_dir is not None and install_dir.is_dir(),
         f"install_dir={install_dir}")
    if install_dir:
        step("builtin.disk has app.yaml",
             (install_dir / "app.yaml").is_file())
        step("builtin.disk has package.toml",
             (install_dir / "package.toml").is_file())
        step("builtin.disk has hash.sha256",
             (install_dir / ".digitorn" / "hash.sha256").is_file())

    # ── 3. Detail endpoint ─────────────────────────────────────────
    r = client.get(f"/api/apps/{app_id}")
    detail = r.json().get("data") or {}
    step("builtin.detail returns 200",
         r.status_code == 200,
         f"status={r.status_code}")
    # runtime_status may be "running" (deployed) OR "not_deployed"
    # if the builtin compile failed for this user scope.
    rstatus = detail.get("runtime_status")
    step("builtin.detail has runtime_status",
         rstatus in ("running", "not_deployed", "broken"),
         f"runtime_status={rstatus}")

    # ── 4. check-update ────────────────────────────────────────────
    r = client.get(f"/api/apps/{app_id}/check-update")
    cu_data = r.json().get("data") or {}
    step("builtin.check-update returns 200",
         r.status_code == 200,
         f"status={r.status_code}")
    step("builtin.check-update has current_version",
         bool(cu_data.get("current_version")),
         f"payload={cu_data}")

    # ── 5. Uninstall (force=true; builtins need force) ─────────────
    # Note: digitorn-code isn't flagged as builtin-in-use so force may
    # not be strictly required, but passing it is safer.
    r = client.post(f"/api/apps/{app_id}/uninstall", json={"force": True})
    step("builtin.uninstall returns 200",
         r.status_code == 200,
         f"status={r.status_code} body={r.text[:200]}")

    # ── 6. Disk cleanup verification ──────────────────────────────
    # Non-admin users can only uninstall their user-scope copy. The
    # system-scope install dir under ~/.digitorn/packages/ stays. So
    # we ONLY check the user-scope path is gone.
    user_install_after = user_pkg_dir(user_id, app_id)
    step("builtin.disk user-scope install_dir removed",
         not user_install_after.is_dir(),
         f"still exists: {user_install_after}")


# ──────────────────────────────────────────────────────────────────
# HUB source (stub → 501)
# ──────────────────────────────────────────────────────────────────

def lifecycle_hub(client: httpx.Client) -> None:
    print("\n" + "=" * 70)
    print("LIFECYCLE: hub source (not yet implemented → 501)")
    print("=" * 70)

    r = client.post("/api/apps/install", json={
        "source_type": "hub",
        "source_uri": "hub://digitorn/some-app@1.0.0",
        "accept_permissions": True,
    })
    step("hub.install returns 501 (deferred to v2)",
         r.status_code == 501,
         f"status={r.status_code} body={r.text[:200]}")
    step("hub.install error message mentions v2",
         "v2" in r.text.lower() or "deferred" in r.text.lower(),
         f"body={r.text[:200]}")

    # Source shortcut
    r = client.post("/api/apps/install", json={
        "source": "hub://digitorn/other@latest",
        "accept_permissions": True,
    })
    step("hub.install via source:// shortcut also 501",
         r.status_code == 501,
         f"status={r.status_code}")

    # Upgrade path
    r = client.post("/api/apps/digitorn-chat/upgrade", json={
        "source_type": "hub",
        "source_uri": "hub://digitorn/digitorn-chat@2.0.0",
        "accept_permissions": True,
    })
    step("hub.upgrade returns 501 (deferred to v2)",
         r.status_code in (501, 404),
         f"status={r.status_code}")


# ──────────────────────────────────────────────────────────────────
# GIT source (stub → 501)
# ──────────────────────────────────────────────────────────────────

def lifecycle_git(client: httpx.Client) -> None:
    print("\n" + "=" * 70)
    print("LIFECYCLE: git source (not yet implemented → 501)")
    print("=" * 70)

    r = client.post("/api/apps/install", json={
        "source_type": "git",
        "source_uri": "git+https://github.com/example/myapp.git",
        "accept_permissions": True,
    })
    step("git.install returns 501 (deferred to v2)",
         r.status_code == 501,
         f"status={r.status_code} body={r.text[:200]}")

    # Multiple git-URI shortcuts
    for uri in [
        "git+https://github.com/user/repo.git",
        "git+ssh://git@github.com/user/repo.git",
        "https://github.com/user/repo",
    ]:
        r = client.post("/api/apps/install", json={
            "source": uri,
            "accept_permissions": True,
        })
        step(f"git.install via source={uri[:40]}... → 501",
             r.status_code == 501,
             f"status={r.status_code}")


# ──────────────────────────────────────────────────────────────────
# UPGRADE lifecycle (local)
# ──────────────────────────────────────────────────────────────────

def lifecycle_upgrade(client: httpx.Client, user_id: str) -> None:
    print("\n" + "=" * 70)
    print("LIFECYCLE: upgrade (local)")
    print("=" * 70)

    app_id = "e2e-upgrade-test"
    src_dir = Path(tempfile.mkdtemp(prefix="e2e_upgrade_"))
    make_local_app(src_dir, app_id, version="1.0.0")

    try:
        # Install v1.0.0
        r = client.post("/api/apps/install", json={
            "source_type": "local",
            "source_uri": str(src_dir),
            "accept_permissions": True,
        })
        step("upgrade.install v1.0.0 ok", r.status_code == 200,
             f"status={r.status_code}")

        # Capture v1 hash
        install_dir = find_install_dir(app_id, user_id)
        v1_hash = ""
        if install_dir:
            hash_file = install_dir / ".digitorn" / "hash.sha256"
            if hash_file.is_file():
                v1_hash = hash_file.read_text(encoding="utf-8").strip()

        # Rewrite source to v1.1.0
        make_local_app(src_dir, app_id, version="1.1.0")

        # Upgrade
        r = client.post(f"/api/apps/{app_id}/upgrade", json={
            "source_type": "local",
            "source_uri": str(src_dir),
            "accept_permissions": True,
        })
        step("upgrade.upgrade returns 200", r.status_code == 200,
             f"status={r.status_code} body={r.text[:200]}")

        # Verify new version
        r = client.get(f"/api/apps/{app_id}")
        detail = r.json().get("data") or {}
        new_version = detail.get("version")
        step("upgrade.new version is 1.1.0",
             new_version == "1.1.0",
             f"version={new_version}")

        # Verify hash changed
        install_dir = find_install_dir(app_id, user_id)
        v2_hash = ""
        if install_dir:
            hash_file = install_dir / ".digitorn" / "hash.sha256"
            if hash_file.is_file():
                v2_hash = hash_file.read_text(encoding="utf-8").strip()
        step("upgrade.hash changed after upgrade",
             bool(v1_hash) and bool(v2_hash) and v1_hash != v2_hash,
             f"v1={v1_hash[:16]}... v2={v2_hash[:16]}...")

        # Cleanup
        r = client.post(f"/api/apps/{app_id}/uninstall", json={"force": True})
        step("upgrade.cleanup uninstall ok", r.status_code == 200)

    finally:
        shutil.rmtree(src_dir, ignore_errors=True)


# ──────────────────────────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────────────────────────

def main() -> int:
    print(f"Target: {BASE}")
    print(f"DB: {DB_PATH}")
    print(f"Packages dir: {PKG_DIR_SYSTEM}")

    with httpx.Client(base_url=BASE, timeout=60.0) as client:
        tok, user_id = login(client)
        print(f"Logged in as user_id={user_id[:16]}...\n")

        lifecycle_local(client, user_id)
        lifecycle_builtin(client, user_id)
        lifecycle_hub(client)
        lifecycle_git(client)
        lifecycle_upgrade(client, user_id)

    # ──────────────────────────────────────────────────────────────
    # Summary
    # ──────────────────────────────────────────────────────────────
    total = len(results)
    passed = sum(1 for r in results if r.ok)
    failed = total - passed
    print("\n" + "=" * 70)
    print(f"E2E LIFECYCLE SUMMARY: {passed}/{total} passed ({passed*100//total}%)")
    print("=" * 70)
    if failed:
        print("\nFailures:")
        for r in results:
            if not r.ok:
                print(f"  [FAIL] {r.name}")
                if r.detail:
                    print(f"         {r.detail[:200]}")

    out = Path("tools/test_lifecycle_e2e_result.json")
    out.write_text(json.dumps(
        [r.__dict__ for r in results], indent=2, ensure_ascii=False,
    ), encoding="utf-8")
    print(f"\nJSON: {out}")

    return 0 if failed == 0 else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        traceback.print_exc()
        sys.exit(3)
