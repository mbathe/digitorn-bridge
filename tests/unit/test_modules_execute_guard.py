"""BUG-061: POST /api/modules/{id}/execute must require admin.

The route bypasses every per-app sandbox (security profile, workspace
root, path traversal guards) because those only apply when modules are
invoked through the agent loop. A developer-level token was previously
enough to get arbitrary shell + filesystem RCE. The guard added in
this change must reject non-admin callers with 403 BEFORE any module
lookup happens, so the error does not depend on `module_id` being
valid.
"""
from __future__ import annotations
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "packages"))

from types import SimpleNamespace

from fastapi import HTTPException  # noqa: E402

from digitorn.core.api.modules import _require_admin_for_execute  # noqa: E402


def _req(perms: list[str]) -> SimpleNamespace:
    state = SimpleNamespace(permissions=perms)
    return SimpleNamespace(state=state)


def run() -> int:
    failures: list[str] = []

    # 1. No perms → 403
    try:
        _require_admin_for_execute(_req([]))
    except HTTPException as exc:
        if exc.status_code != 403:
            failures.append(f"empty perms: expected 403, got {exc.status_code}")
    else:
        failures.append("empty perms: should have raised 403")

    # 2. developer-only perms → 403 (this is the original CVE path)
    try:
        _require_admin_for_execute(_req(["sessions.read", "apps.read"]))
    except HTTPException as exc:
        if exc.status_code != 403:
            failures.append(f"dev perms: expected 403, got {exc.status_code}")
    else:
        failures.append("dev perms: should have raised 403")

    # 3. wildcard '*' (admin) → passes through
    try:
        _require_admin_for_execute(_req(["*"]))
    except HTTPException as exc:
        failures.append(f"admin wildcard: unexpected {exc.status_code}")

    # 4. narrow 'modules.execute' perm → passes through (future RBAC
    #    setting that lets a non-admin service account keep using this)
    try:
        _require_admin_for_execute(_req(["modules.execute"]))
    except HTTPException as exc:
        failures.append(f"modules.execute perm: unexpected {exc.status_code}")

    if failures:
        print("FAIL - modules.execute admin guard:")
        for f in failures:
            print(f"  - {f}")
        return 1

    print("PASS - modules.execute admin guard rejects non-admins (403), "
          "passes admin (*) and narrow (modules.execute) perms")
    return 0


if __name__ == "__main__":
    sys.exit(run())
