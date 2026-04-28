"""Dedicated test for upgrade rollback when V2 deploy fails.

Scenario:
  1. Install a valid V1 - deploys OK, app runs.
  2. Replace the source with a V2 that has a BROKEN app.yaml
     (bad schema, so patch succeeds but compiler rejects at deploy).
  3. Upgrade - InstallFlow patches install_dir with V2 then tries to
     deploy → fails → must roll back to V1.
  4. Verify: install_dir contains V1 content again (via hash),
     app is still running with V1 version.

Prior to the fix in install.py::upgrade, the backup step was missing,
so the rollback tried to rename a nonexistent ``-old`` dir back. The
app was left with V2 files on disk and the registry stuck on UPGRADING
or BROKEN.
"""
from __future__ import annotations

import hashlib
import shutil
import sys
import tempfile
import time
from pathlib import Path

import httpx

sys.stdout.reconfigure(encoding="utf-8")

BASE = "http://127.0.0.1:8000"
EMAIL = "routetest@test.local"
USERNAME = "routetest"
PASSWORD = "routetest123"


def _write_valid_app(dirpath: Path, app_id: str, version: str) -> None:
    dirpath.mkdir(parents=True, exist_ok=True)
    (dirpath / "package.toml").write_text(
        f"""[package]
id = "{app_id}"
name = "{app_id}"
version = "{version}"
description = "rollback test"
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
""", encoding="utf-8")
    (dirpath / "app.yaml").write_text(
        f"""app:
  app_id: "{app_id}"
  name: "{app_id}"
  version: "{version}"
  description: "V{version}"
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
""", encoding="utf-8")


def _write_broken_app(dirpath: Path, app_id: str, version: str) -> None:
    """Same package.toml as valid (manifest parses), but app.yaml is
    schema-invalid - compiler will refuse it."""
    dirpath.mkdir(parents=True, exist_ok=True)
    (dirpath / "package.toml").write_text(
        f"""[package]
id = "{app_id}"
name = "{app_id}"
version = "{version}"
description = "rollback test V2 broken"
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
""", encoding="utf-8")
    # Schema-invalid - `modules` is a list not a dict, and `app.id` instead
    # of `app.app_id`. Both guarantee the compiler rejects.
    (dirpath / "app.yaml").write_text(
        f"""app:
  id: "{app_id}"
  name: "{app_id}"
  version: "{version}"

agent:
  id: main

modules: []
""", encoding="utf-8")


def main() -> int:
    app_id = "rollback-test-app"
    src_dir = Path(tempfile.mkdtemp(prefix="rollback_src_"))
    results: list[tuple[str, bool, str]] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        results.append((name, ok, detail))
        tag = "[PASS]" if ok else "[FAIL]"
        print(f"{tag} {name}")
        if not ok and detail:
            print(f"       {detail}")

    try:
        with httpx.Client(base_url=BASE, timeout=60.0) as c:
            # Auth
            r = c.post("/auth/login", json={
                "email": EMAIL, "username": USERNAME, "password": PASSWORD,
            })
            if r.status_code >= 400:
                r = c.post("/auth/register", json={
                    "email": EMAIL, "username": USERNAME, "password": PASSWORD,
                })
            tok = r.json()["access_token"]
            c.headers["Authorization"] = f"Bearer {tok}"

            # Cleanup from any previous run
            c.post(f"/api/apps/{app_id}/uninstall", json={"force": True})

            # ── V1 install ─────────────────────────────────────────
            _write_valid_app(src_dir, app_id, "1.0.0")
            r = c.post("/api/apps/install", json={
                "source_type": "local",
                "source_uri": str(src_dir),
                "accept_permissions": True,
            })
            check("V1 install returns 200", r.status_code == 200,
                  f"status={r.status_code}")
            data = r.json().get("data") or {}
            check("V1 deployed", data.get("deployed") is True,
                  f"deploy_error={data.get('deploy_error')}")
            v1_hash = data.get("hash") or ""
            check("V1 hash captured", bool(v1_hash), f"hash={v1_hash[:16]}")

            # Confirm V1 is reachable
            r = c.get(f"/api/apps/{app_id}")
            check("V1 detail runtime_status=running",
                  r.status_code == 200 and
                  (r.json().get("data") or {}).get("runtime_status") == "running")

            # ── Replace src with BROKEN V2 ─────────────────────────
            shutil.rmtree(src_dir, ignore_errors=True)
            _write_broken_app(src_dir, app_id, "2.0.0")

            # ── Upgrade attempt - should fail to deploy ────────────
            r = c.post(f"/api/apps/{app_id}/upgrade", json={
                "source_type": "local",
                "source_uri": str(src_dir),
                "accept_permissions": True,
            })
            # Expected: 200 with deploy_error, or possibly 400/500
            data = r.json().get("data") or {}
            deploy_error = data.get("deploy_error") or ""
            check("V2 upgrade returns (with deploy_error)",
                  r.status_code in (200, 400, 500) and
                  (bool(deploy_error) or r.status_code >= 400),
                  f"status={r.status_code} deploy_error={deploy_error[:100]}")

            # ── The critical part: rollback behavior ───────────────
            # Give the daemon a moment to commit rollback state
            time.sleep(0.5)

            r = c.get(f"/api/apps/{app_id}")
            det = r.json().get("data") or {}
            current_version = det.get("version", "")
            current_hash = det.get("hash", "")
            current_status = det.get("runtime_status", "")
            install_status = det.get("install_status", "")

            # After rollback, we expect:
            #   - version back to 1.0.0 (rollback restored V1 content)
            #   - hash matches v1_hash
            #   - runtime_status=running (V1 still deployable; OR broken
            #     if the rollback restore succeeded but redeploy didn't)
            #   - install_status in (installed, broken, upgrading) -
            #     installed if rollback full, broken if rollback partial
            check("After rollback: version restored to 1.0.0",
                  current_version == "1.0.0",
                  f"version={current_version}")

            check("After rollback: hash matches V1",
                  current_hash == v1_hash,
                  f"v1={v1_hash[:16]} current={current_hash[:16]}")

            check("After rollback: install_status not UPGRADING (not stuck)",
                  install_status != "upgrading",
                  f"status={install_status}")

            # App must not be stuck in a broken state where it's neither
            # running nor cleanly broken.
            check("After rollback: runtime_status is coherent",
                  current_status in ("running", "broken", "not_deployed"),
                  f"runtime_status={current_status}")

            # Verify the install_dir has V1 files
            install_dir = Path(det.get("install_dir") or "")
            if install_dir.is_dir():
                app_yaml = (install_dir / "app.yaml").read_text(encoding="utf-8")
                check("After rollback: app.yaml has V1 schema (app_id not id)",
                      "app_id:" in app_yaml and "modules: {}" in app_yaml,
                      f"app_yaml head={app_yaml[:200]}")

            # ── Cleanup ────────────────────────────────────────────
            r = c.post(f"/api/apps/{app_id}/uninstall", json={"force": True})
            check("Cleanup uninstall",
                  r.status_code == 200,
                  f"status={r.status_code}")

            # And verify undeploy (Bug A+B)
            r = c.get("/api/apps")
            apps = [a.get("app_id") for a in (r.json().get("data") or [])]
            check("Cleanup: app not in listing",
                  app_id not in apps,
                  f"still there: {app_id in apps}")

    finally:
        shutil.rmtree(src_dir, ignore_errors=True)

    total = len(results)
    passed = sum(1 for _, ok, _ in results if ok)
    print("\n" + "=" * 60)
    print(f"UPGRADE ROLLBACK: {passed}/{total} passed")
    print("=" * 60)
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
