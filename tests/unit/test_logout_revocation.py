"""BUG-057 + BUG-058: logout must accept empty body AND invalidate
the bearer access token.

Before this change, /auth/logout returned 422 when the SDK sent no
body, AND - even after a successful refresh-token revoke - the
access token remained valid until its natural expiry (up to 24h).
"""
from __future__ import annotations
import asyncio
import sys
import time
from pathlib import Path
from unittest.mock import AsyncMock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "packages"))

from digitorn.core.auth.jwt import JWTService  # noqa: E402
from digitorn.core.auth.service import AuthService  # noqa: E402


def _make_service() -> AuthService:
    jwt = JWTService(secret_key="x" * 32, algorithm="HS256")
    svc = AuthService(jwt)
    # Short-circuit the DB path - the test doesn't need refresh persistence.
    svc._revoke_refresh_token = AsyncMock(return_value=True)
    return svc


async def run() -> int:
    svc = _make_service()
    failures: list[str] = []

    # 1. access token before logout → verifies cleanly
    access = svc._jwt.generate_access_token(
        user_id="u1", roles=["developer"], permissions=["apps:read"],
    )
    try:
        payload = svc.verify_access_token(access)
        if payload.user_id != "u1":
            failures.append("pre-logout: wrong user_id")
        if not payload.jti:
            failures.append("pre-logout: missing jti claim (revocation requires it)")
    except Exception as exc:
        failures.append(f"pre-logout verify should succeed: {exc}")

    # 2. logout with access_token only (empty refresh_token) → still revokes
    ok = await svc.logout(refresh_token=None, access_token=access)
    if not ok:
        failures.append("logout(access_token only) should report success")

    # 3. verify_access_token on the same token → raises (revoked)
    try:
        svc.verify_access_token(access)
        failures.append("post-logout: token should be rejected after logout")
    except ValueError as exc:
        if "revoked" not in str(exc).lower():
            failures.append(f"post-logout: wrong error: {exc}")

    # 4. logout(None, None) → no crash, returns False (nothing to revoke)
    ok2 = await svc.logout(None, None)
    if ok2 is not False:
        failures.append("logout(None, None) should return False")

    # 5. GC removes stale entries - fake an expired jti
    svc._revoked_jtis["stale_jti"] = time.time() - 10  # exp in past
    svc._gc_revocations()
    if "stale_jti" in svc._revoked_jtis:
        failures.append("GC should drop expired revocations")

    # 6. a *new* access token minted after logout still works (only
    #    the revoked jti is blocked, not the user)
    new_access = svc._jwt.generate_access_token(user_id="u1")
    try:
        svc.verify_access_token(new_access)
    except Exception as exc:
        failures.append(f"new token after logout should work: {exc}")

    if failures:
        print("FAIL - logout revocation:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("PASS - logout revokes access token, accepts empty body, GCs stale entries")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(run()))
