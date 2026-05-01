"""AuthService - central authentication and authorization service.

Orchestrates auth providers, JWT tokens, roles, and sessions.
Stateless where possible (JWT), stateful for refresh tokens and roles (DB).
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from digitorn_auth.jwt import JWTService, TokenPayload
from digitorn_auth.providers.base import AuthProvider, AuthResult

logger = logging.getLogger(__name__)

BUILTIN_ROLES = {
    "admin": {
        "description": "Full access to all features, users, and configuration",
        "permissions": ["*"],
    },
    "developer": {
        "description": "Create and manage apps, run agents, view sessions",
        "permissions": [
            "apps:read", "apps:write", "apps:deploy", "apps:undeploy",
            "sessions:read", "sessions:write", "sessions:delete",
            "agents:run", "agents:spawn",
            "mcp:read", "mcp:install",
        ],
    },
    "viewer": {
        "description": "Read-only access to apps and sessions",
        "permissions": [
            "apps:read",
            "sessions:read",
        ],
    },
}

@dataclass

class LoginResult:
    """Result of a login attempt."""

    success: bool
    access_token: str | None = None
    refresh_token: str | None = None
    user_id: str | None = None
    email: str | None = None
    display_name: str | None = None
    roles: list[str] | None = None
    permissions: list[str] | None = None
    error: str | None = None
    expires_in: int = 0

class AuthService:
    """Central auth service.

    Manages:
    - Authentication via pluggable providers
    - JWT access/refresh token lifecycle
    - Role-based access control (RBAC)
    - User session binding
    """

    def __init__(self, jwt_service: JWTService):
        self._jwt = jwt_service
        self._providers: dict[str, AuthProvider] = {}
        self._default_provider: str = "local"
        self._session_factory: Any = None
        self._revoked_jtis: dict[str, float] = {}

    async def start(self, config: dict[str, Any]) -> None:
        from digitorn_auth.database import get_session_factory

        self._session_factory = get_session_factory()

        providers_config = config.get("providers", [{"type": "local", "default": True}])
        for prov_config in providers_config:
            prov_type = prov_config.get("type", "local")
            # Allow multiple instances of the same type (e.g. one OAuth2
            # provider for Google AND one for Microsoft) by keying with
            # `id` if present, falling back to `type`.
            prov_id = prov_config.get("id", prov_type)
            provider = self._create_provider(prov_type)
            if provider:
                await provider.on_start(prov_config.get("config", {}))
                self._providers[prov_id] = provider
                if prov_config.get("default"):
                    self._default_provider = prov_id
                logger.info("auth_provider_loaded id=%s type=%s", prov_id, prov_type)

        await self._ensure_builtin_roles()
        await self._ensure_default_admin()

        # Warm the revocation cache from DB BEFORE serving any traffic.
        # Restart-safety: a freshly-started instance must already enforce
        # revocations created while it was down.
        await self.sync_revocations_from_db()

        # Background sync (multi-instance convergence). Stash on self so
        # ``stop()`` can cancel cleanly.
        self._revocation_sync_task = asyncio.create_task(
            self._revocation_sync_loop(),
        )

    def get_provider(self, name: str) -> "AuthProvider | None":
        """Retrieve a registered auth provider by name."""
        return self._providers.get(name)

    async def stop(self) -> None:
        """Cleanup all providers."""
        task = getattr(self, "_revocation_sync_task", None)
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        for provider in self._providers.values():
            await provider.on_stop()

    def _create_provider(self, prov_type: str) -> AuthProvider | None:
        """Factory for auth providers."""
        if prov_type == "local":
            from digitorn_auth.providers.local import LocalProvider
            return LocalProvider()
        elif prov_type == "ldap":
            from digitorn_auth.providers.ldap import LDAPProvider
            return LDAPProvider()
        elif prov_type in ("oauth2", "oidc"):
            from digitorn_auth.providers.oauth2 import OAuth2Provider
            return OAuth2Provider()
        elif prov_type == "api_key":
            from digitorn_auth.providers.api_key import APIKeyProvider
            return APIKeyProvider()
        else:
            logger.warning("auth_unknown_provider type=%s", prov_type)
            return None
    @property
    def _client_expires_in(self) -> int:
        """`expires_in` value to expose to clients via login/refresh.

        When ``access_ttl == 0`` (never expires) we MUST NOT return 0
        - many OAuth-style clients interpret 0 as "expired right
        now" and hammer ``/auth/refresh`` in a loop. The second call
        races against the one-time-use refresh-token revocation set
        by the first, which 401s, which trips the client's logout
        path. Return a 100y horizon so clients schedule their next
        refresh well past the heat death of this dev session.
        """
        ttl = self._jwt._access_ttl
        return ttl if ttl > 0 else 100 * 365 * 24 * 3600

    async def login(
        self,
        credentials: dict[str, Any],
        provider: str | None = None,
        device_info: str | None = None,
    ) -> LoginResult:
        """Authenticate a user and return JWT tokens.

        ``device_info`` carries the caller's User-Agent so the user can
        identify the session in /auth/sessions and revoke a specific
        browser/device when they spot one they don't recognise.
        """
        prov_id = provider or self._default_provider
        prov = self._providers.get(prov_id)
        if not prov:
            return LoginResult(success=False, error=f"Auth provider '{prov_id}' not configured")

        result = await prov.authenticate(credentials)
        if not result.success:
            return LoginResult(success=False, error=result.error)

        roles = await self._get_user_roles(result.user_id)
        permissions = await self._get_user_permissions(result.user_id)
        features = await self._get_account_features(result.user_id)

        access_token = self._jwt.generate_access_token(
            user_id=result.user_id,
            email=result.email,
            display_name=result.display_name,
            roles=roles,
            permissions=permissions,
            extra={"features": features} if features else None,
        )
        refresh_token, refresh_hash = self._jwt.generate_refresh_token(result.user_id)

        await self._store_refresh_token(result.user_id, refresh_hash, device_info)

        await self._update_last_seen(result.user_id)

        logger.info("user_login user_id=%s provider=%s", result.user_id, prov_id)

        return LoginResult(
            success=True,
            access_token=access_token,
            refresh_token=refresh_token,
            user_id=result.user_id,
            email=result.email,
            display_name=result.display_name,
            roles=roles,
            permissions=permissions,
            expires_in=self._client_expires_in,
        )

    async def change_password(
        self, user_id: str, current: str, new: str,
    ) -> bool:
        """Change the password for a local-provider user.

        Returns True on success. Raises RuntimeError with an actionable
        message on any failure (unknown user, wrong current, OAuth-only
        account) so the API layer can surface a clean 400 to the client.
        Only the local provider is supported - OAuth-managed accounts
        are managed by the upstream IdP.
        """
        prov = self._providers.get("local")
        if not prov:
            raise RuntimeError("Local auth provider not configured")

        from digitorn_auth.providers.local import LocalProvider
        if not isinstance(prov, LocalProvider):
            raise RuntimeError("Local provider not available")

        result = await prov.change_password(user_id, current, new)
        if not result.success:
            raise RuntimeError(result.error or "change_password failed")
        return True

    async def register(
        self,
        username: str,
        password: str,
        email: str | None = None,
        display_name: str | None = None,
        device_info: str | None = None,
    ) -> LoginResult:
        """Register a new local user and return tokens."""
        prov = self._providers.get("local")
        if not prov:
            return LoginResult(success=False, error="Local auth provider not configured")

        from digitorn_auth.providers.local import LocalProvider
        if not isinstance(prov, LocalProvider):
            return LoginResult(success=False, error="Local provider not available")

        result = await prov.register(username, password, email, display_name)
        if not result.success:
            return LoginResult(success=False, error=result.error)

        # First-user bootstrap: if no user currently has the admin
        # role, the first registration gets it. This gives solo
        # operators a usable daemon out of the box (they can PATCH
        # /api/config etc.) without hardcoding a default admin and
        # without manual CLI post-install steps. All subsequent
        # registrations default to `developer`.
        try:
            has_admin = await self._any_user_has_role("admin")
        except Exception:
            has_admin = True  # safe default
        role_to_assign = "admin" if not has_admin else "developer"
        # The user was just created two DB writes ago - we KNOW it has
        # no role yet. Skip the duplicate-check SELECT to shave off one
        # round-trip.
        await self._assign_role(
            result.user_id, role_to_assign, skip_existing_check=True,
        )
        if role_to_assign == "admin":
            logger.info(
                "bootstrap_admin user_id=%s username=%s - first user, "
                "granted admin role automatically",
                result.user_id, username,
            )

        # Generate tokens directly instead of calling ``self.login()`` -
        # login would re-verify the password (bcrypt ~250 ms) and
        # re-fetch the user row we already hold. On a remote DB (Neon,
        # 150 ms RTT), the round-trips compound: previously, register
        # took ~11 s = provider.register + full login. We already know
        # the user_id, email, and the role we just assigned, so skip
        # the redundant verify + user-row select + role select and fall
        # through to token issuance.
        permissions = list(
            BUILTIN_ROLES.get(role_to_assign, {}).get("permissions", [])
        )
        roles = [role_to_assign]
        # Fresh registration: no AccountFeatures row yet, so the
        # ``features`` claim is omitted. Daemons treat this as a free
        # / default account. The dashboard can later promote the user
        # to a paid plan via PATCH /auth/admin/account-features.
        features = await self._get_account_features(result.user_id)
        access_token = self._jwt.generate_access_token(
            user_id=result.user_id,
            email=email,
            display_name=display_name or username,
            roles=roles,
            permissions=permissions,
            extra={"features": features} if features else None,
        )
        refresh_token, refresh_hash = self._jwt.generate_refresh_token(
            result.user_id,
        )
        await self._store_refresh_token(result.user_id, refresh_hash, device_info)
        # Intentionally skip _update_last_seen here - the user was just
        # created, their ``created_at`` is effectively their last-seen.
        # Saves one UPDATE round-trip.

        return LoginResult(
            success=True,
            access_token=access_token,
            refresh_token=refresh_token,
            user_id=result.user_id,
            email=email,
            display_name=display_name or username,
            roles=roles,
            permissions=permissions,
            expires_in=self._client_expires_in,
        )

    async def refresh(self, refresh_token: str) -> LoginResult:
        """Exchange a refresh token for a new access + refresh token pair.

        Production mode (``refresh_ttl > 0``): rotation - the old
        refresh token is revoked, a new one is issued, one-time use,
        limits leak exposure.

        Dev mode (``refresh_ttl == 0`` - never expires): no rotation.
        The Flutter / web clients can fire concurrent refreshes (e.g.
        multiple in-flight requests racing on a 401) without the
        second call hitting a revoked-token 401 → spurious logout.
        Same refresh token is returned, only a fresh access token is
        minted. Safe in local dev where the token already lives ~100y.
        """
        try:
            payload = self._jwt.verify(refresh_token)
        except Exception as exc:
            return LoginResult(success=False, error=f"Invalid refresh token: {exc}")

        if payload.token_type != "refresh":
            return LoginResult(success=False, error="Not a refresh token")

        token_hash = self._jwt.hash_token(refresh_token)
        if not await self._verify_refresh_token(payload.user_id, token_hash):
            return LoginResult(success=False, error="Refresh token revoked or expired")

        roles = await self._get_user_roles(payload.user_id)
        permissions = await self._get_user_permissions(payload.user_id)
        features = await self._get_account_features(payload.user_id)

        access_token = self._jwt.generate_access_token(
            user_id=payload.user_id,
            email=payload.email,
            display_name=payload.display_name,
            roles=roles,
            permissions=permissions,
            extra={"features": features} if features else None,
        )

        if self._jwt._refresh_ttl > 0:
            # Prod-mode rotation. Inherit the device_info from the
            # original session row so the rotated row shows up under
            # the same browser/device label in /auth/sessions.
            from digitorn_auth.models import RefreshToken
            old_device_info: str | None = None
            try:
                async with self._session_factory() as session:
                    stmt = select(RefreshToken).where(
                        RefreshToken.user_id == payload.user_id,
                        RefreshToken.token_hash == token_hash,
                    )
                    old = (await session.execute(stmt)).scalar_one_or_none()
                    if old is not None:
                        old_device_info = old.device_info
            except Exception:
                pass
            await self._revoke_refresh_token(token_hash)
            new_refresh, new_hash = self._jwt.generate_refresh_token(payload.user_id)
            await self._store_refresh_token(payload.user_id, new_hash, old_device_info)
            returned_refresh = new_refresh
        else:
            # Dev-mode: keep the same refresh token alive across calls
            returned_refresh = refresh_token

        return LoginResult(
            success=True,
            access_token=access_token,
            refresh_token=returned_refresh,
            user_id=payload.user_id,
            email=payload.email,
            display_name=payload.display_name,
            roles=roles,
            permissions=permissions,
            expires_in=self._client_expires_in,
        )

    async def logout(
        self, refresh_token: str | None = None, access_token: str | None = None,
    ) -> bool:
        """Revoke a refresh token AND/OR an access token (logout).

        Both arguments are optional: the API accepts a missing body, in
        which case only the Authorization header's access token gets
        revoked. A stolen bearer must not stay usable for its full
        15-min TTL after an explicit logout.

        The access-token jti goes into BOTH the in-memory cache (fast
        local check) and the ``revoked_tokens`` table (durable, served
        via GET /auth/revocations so daemons can sync). Refresh-token
        revocation is handled by ``_revoke_refresh_token`` (separate
        ``refresh_tokens`` table flagged as ``revoked=true``).
        """
        any_revoked = False
        if refresh_token:
            try:
                token_hash = self._jwt.hash_token(refresh_token)
                if await self._revoke_refresh_token(token_hash):
                    any_revoked = True
            except Exception:
                logger.debug("refresh revoke failed", exc_info=True)
        if access_token:
            try:
                payload = self._jwt.verify(access_token)
                if payload.jti:
                    self._revoked_jtis[payload.jti] = float(payload.expires_at or 0)
                    await self._persist_revocation(
                        jti=payload.jti,
                        user_id=payload.user_id,
                        expires_at=float(payload.expires_at or 0),
                        reason="user_logout",
                    )
                    any_revoked = True
            except Exception:
                logger.debug("access revoke failed", exc_info=True)
            self._gc_revocations()
        return any_revoked

    async def revoke_jti(
        self,
        jti: str,
        user_id: str,
        expires_at: float,
        reason: str = "admin_revoke",
    ) -> None:
        """Explicitly revoke a token by jti.

        Called by the admin endpoint, the dashboard's
        "log out everywhere" button, and the security-incident path.
        Lands in BOTH the in-memory cache (this process) and the DB
        table (other replicas + daemon sync via GET /auth/revocations).
        """
        self._revoked_jtis[jti] = expires_at
        await self._persist_revocation(
            jti=jti, user_id=user_id, expires_at=expires_at, reason=reason,
        )
        self._gc_revocations()

    async def _persist_revocation(
        self,
        jti: str,
        user_id: str,
        expires_at: float,
        reason: str,
    ) -> None:
        from datetime import datetime, timezone as _tz
        from digitorn_auth.models import RevokedToken
        # When the original token had no `exp` claim (TTL=0 / never
        # expires), park the deny-list row in year 9999 so the GC and
        # the active-only filter both keep it forever.
        if expires_at and expires_at > 0:
            exp_dt = datetime.fromtimestamp(expires_at, tz=_tz.utc)
        else:
            exp_dt = datetime(9999, 12, 31, 23, 59, 59, tzinfo=_tz.utc)
        async with self._session_factory() as session:
            existing = await session.get(RevokedToken, jti)
            if existing is not None:
                return  # already revoked, idempotent
            row = RevokedToken(
                jti=jti,
                user_id=user_id,
                expires_at=exp_dt,
                reason=reason,
            )
            session.add(row)
            await session.commit()

    async def list_revocations(
        self,
        since: float = 0,
        only_active: bool = True,
    ) -> list[dict]:
        """Return revocations newer than ``since`` (epoch seconds).

        ``only_active=True`` (default) hides rows whose original
        ``expires_at`` is already past — daemons don't need to track
        them anymore, the signature check rejects them naturally.
        """
        from datetime import datetime, timezone as _tz
        from digitorn_auth.models import RevokedToken
        cutoff = datetime.fromtimestamp(since, tz=_tz.utc) if since else None
        async with self._session_factory() as session:
            stmt = select(RevokedToken).order_by(RevokedToken.revoked_at.asc())
            if cutoff is not None:
                stmt = stmt.where(RevokedToken.revoked_at > cutoff)
            if only_active:
                stmt = stmt.where(RevokedToken.expires_at > datetime.now(_tz.utc))
            rows = (await session.execute(stmt)).scalars().all()
            return [
                {
                    "jti": r.jti,
                    "user_id": r.user_id,
                    "expires_at": r.expires_at.isoformat(),
                    "reason": r.reason,
                    "revoked_at": r.revoked_at.isoformat(),
                }
                for r in rows
            ]

    async def revoke_all_for_user(self, user_id: str, reason: str = "admin_revoke_all") -> int:
        """Mass-revoke every refresh token for a user.

        Use case: 'log out everywhere' / suspected compromise / employee
        offboarding. Doesn't touch access tokens already in the wild —
        those expire on their own (15 min) but new logins via the
        revoked refreshes will fail.
        """
        from digitorn_auth.models import RefreshToken
        async with self._session_factory() as session:
            stmt = (
                update(RefreshToken)
                .where(RefreshToken.user_id == user_id, RefreshToken.revoked.is_(False))
                .values(revoked=True)
            )
            res = await session.execute(stmt)
            await session.commit()
            return int(res.rowcount or 0)

    def _gc_revocations(self) -> None:
        """Drop revocations whose original exp is already in the past."""
        now = time.time()
        stale = [jti for jti, exp in self._revoked_jtis.items() if exp and exp < now]
        for jti in stale:
            self._revoked_jtis.pop(jti, None)

    async def sync_revocations_from_db(self) -> int:
        """Refresh the in-memory revocation cache from the durable DB
        table. Run periodically (every 30s) by ``_revocation_sync_loop``
        so multi-instance deployments converge: a logout on instance A
        reaches instance B within one tick.

        Returns the number of jtis loaded.
        """
        from datetime import datetime, timezone as _tz
        from digitorn_auth.models import RevokedToken

        try:
            async with self._session_factory() as session:
                stmt = select(RevokedToken).where(
                    RevokedToken.expires_at > datetime.now(_tz.utc),
                )
                rows = (await session.execute(stmt)).scalars().all()
                fresh: dict[str, float] = {
                    r.jti: r.expires_at.timestamp() for r in rows
                }
        except Exception as exc:  # noqa: BLE001
            logger.warning("revocation_sync_failed: %s", exc)
            return -1

        # Replace wholesale - covers both new revocations (added) AND
        # old ones we'd kept locally past their expiry without DB
        # backing (e.g. process restarted).
        self._revoked_jtis = fresh
        return len(fresh)

    async def _revocation_sync_loop(self, interval_s: int = 30) -> None:
        """Background task started by ``start()``. Cancelled at stop.

        Keeps every instance's revocation deny-list in sync with the
        DB without needing to round-trip on every verify call. Trade-off:
        max 30s window where a freshly-revoked jti can still be honored
        on a different instance — acceptable for most flows, tunable
        via ``revocation_sync_interval`` setting if you need stricter.
        """
        while True:
            await asyncio.sleep(interval_s)
            await self.sync_revocations_from_db()

    def verify_access_token(self, token: str) -> TokenPayload:
        """Verify an access token. Raises on invalid/expired/revoked."""
        payload = self._jwt.verify(token)
        if payload.token_type != "access":
            raise ValueError("Not an access token")
        if payload.jti and payload.jti in self._revoked_jtis:
            raise ValueError("Token has been revoked")
        return payload
    async def _get_user_roles(self, user_id: str) -> list[str]:
        """Get all role names for a user."""
        from digitorn_auth.models import Role, UserRole

        async with self._session_factory() as session:
            stmt = (
                select(Role.name)
                .join(UserRole, UserRole.role_id == Role.id)
                .where(UserRole.user_id == user_id)
            )
            result = await session.execute(stmt)
            return [row[0] for row in result.fetchall()]

    async def _get_user_permissions(self, user_id: str) -> list[str]:
        """Get merged permissions from all user roles."""
        from digitorn_auth.models import Role, UserRole

        async with self._session_factory() as session:
            stmt = (
                select(Role.permissions)
                .join(UserRole, UserRole.role_id == Role.id)
                .where(UserRole.user_id == user_id)
            )
            result = await session.execute(stmt)
            perms = set()
            for (role_perms,) in result.fetchall():
                if role_perms:
                    perms.update(role_perms)
            return sorted(perms)

    async def _get_account_features(self, user_id: str) -> dict | None:
        """Read this user's feature flags + quotas, or None if no row.

        Returned dict is a flat snapshot baked straight into the JWT
        ``features`` claim. Daemons read it via ``RemoteAuthClaims.raw``
        to gate cloud usage, paid-only apps, etc. None means "row
        missing" - the daemon should treat the user as a free / default
        account in that case.
        """
        from digitorn_auth.models import AccountFeatures

        async with self._session_factory() as session:
            row = await session.get(AccountFeatures, user_id)
            if row is None:
                return None
            return {
                "plan_tier": row.plan_tier,
                "cloud_enabled": row.cloud_enabled,
                "self_host_enabled": row.self_host_enabled,
                "cloud_token_quota_monthly": row.cloud_token_quota_monthly,
                "max_paired_devices": row.max_paired_devices,
                "extra": row.flags or {},
            }

    async def _any_user_has_role(self, role_name: str) -> bool:
        """Return True if at least one user currently has `role_name`.

        Used by the bootstrap-admin path during registration - we want
        the very first user to self-promote to admin so solo deployments
        have a working config-edit flow out of the box.
        """
        from digitorn_auth.models import Role, UserRole

        async with self._session_factory() as session:
            stmt = (
                select(UserRole)
                .join(Role, Role.id == UserRole.role_id)
                .where(Role.name == role_name)
                .limit(1)
            )
            hit = (await session.execute(stmt)).scalar_one_or_none()
            return hit is not None

    async def _assign_role(
        self, user_id: str, role_name: str,
        granted_by: str | None = None,
        *,
        skip_existing_check: bool = False,
    ) -> bool:
        """Assign a role to a user.

        ``skip_existing_check=True`` skips the ``SELECT UserRole``
        duplicate-check - safe when the caller knows the user is
        brand-new (e.g. right after register). Saves one DB round-trip.
        """
        from digitorn_auth.models import Role, UserRole

        async with self._session_factory() as session:
            stmt = select(Role).where(Role.name == role_name)
            role = (await session.execute(stmt)).scalar_one_or_none()
            if not role:
                return False

            if not skip_existing_check:
                stmt = select(UserRole).where(
                    UserRole.user_id == user_id,
                    UserRole.role_id == role.id,
                )
                existing = (await session.execute(stmt)).scalar_one_or_none()
                if existing:
                    return True

            user_role = UserRole(user_id=user_id, role_id=role.id, granted_by=granted_by)
            session.add(user_role)
            await session.commit()
            return True

    async def _ensure_builtin_roles(self) -> None:
        """Create built-in roles if they don't exist."""
        from digitorn_auth.models import Role

        async with self._session_factory() as session:
            for name, info in BUILTIN_ROLES.items():
                stmt = select(Role).where(Role.name == name)
                existing = (await session.execute(stmt)).scalar_one_or_none()
                if not existing:
                    role = Role(
                        name=name,
                        description=info["description"],
                        is_builtin=True,
                        permissions=info["permissions"],
                    )
                    session.add(role)
                    logger.info("builtin_role_created name=%s", name)
            await session.commit()

    async def _ensure_default_admin(self) -> None:
        """Create a default admin account on first startup if no users exist."""
        from digitorn_auth.models import User

        async with self._session_factory() as session:
            stmt = select(User).limit(1)
            existing = (await session.execute(stmt)).scalar_one_or_none()
            if existing:
                return

        prov = self._providers.get("local")
        if not prov:
            return

        from digitorn_auth.providers.local import LocalProvider
        if not isinstance(prov, LocalProvider):
            return

        result = await prov.register(
            username="admin",
            password="admin1234admin",
            email="admin@digitorn.local",
            display_name="Administrator",
        )
        if result.success:
            await self._assign_role(result.user_id, "admin")
            logger.info(
                "default_admin_created username=admin password=admin1234admin - CHANGE THIS PASSWORD"
            )

    async def _store_refresh_token(
        self,
        user_id: str,
        token_hash: str,
        device_info: str | None = None,
    ) -> None:
        """Store a refresh token hash in DB.

        ``device_info`` carries the User-Agent (or any caller-provided
        free-form label) so the user can recognise the session in the
        ``/auth/sessions`` listing. Capped at 512 chars (column type).
        """
        from digitorn_auth.models import RefreshToken

        ttl = self._jwt._refresh_ttl
        # 100y in seconds - far enough future that "never" holds in
        # practice without needing a NULL column.
        effective_ttl = ttl if ttl > 0 else 100 * 365 * 24 * 3600

        async with self._session_factory() as session:
            rt = RefreshToken(
                user_id=user_id,
                token_hash=token_hash,
                device_info=(device_info or "")[:512] or None,
                expires_at=datetime.fromtimestamp(
                    time.time() + effective_ttl, tz=timezone.utc,
                ),
            )
            session.add(rt)
            await session.commit()

    async def list_sessions(self, user_id: str) -> list[dict]:
        """Return active (non-revoked, non-expired) refresh-token rows
        for the user. The hash is NEVER returned — only metadata the
        user might recognise (id, device_info, created_at, expires_at).
        """
        from digitorn_auth.models import RefreshToken

        async with self._session_factory() as session:
            stmt = (
                select(RefreshToken)
                .where(
                    RefreshToken.user_id == user_id,
                    RefreshToken.revoked.is_(False),
                    RefreshToken.expires_at > datetime.now(timezone.utc),
                )
                .order_by(RefreshToken.created_at.desc())
            )
            rows = (await session.execute(stmt)).scalars().all()
            return [
                {
                    "id": r.id,
                    "device_info": r.device_info,
                    "created_at": r.created_at.isoformat(),
                    "expires_at": r.expires_at.isoformat(),
                }
                for r in rows
            ]

    async def revoke_session(self, user_id: str, session_id: str) -> bool:
        """Revoke ONE refresh token by id. Returns True if a row was
        flipped from active → revoked. Ownership is enforced — passing
        another user's session_id silently no-ops (returns False).
        """
        from digitorn_auth.models import RefreshToken

        async with self._session_factory() as session:
            row = await session.get(RefreshToken, session_id)
            if row is None or row.user_id != user_id or row.revoked:
                return False
            row.revoked = True
            await session.commit()
            return True

    async def _verify_refresh_token(self, user_id: str, token_hash: str) -> bool:
        """Check if a refresh token exists in DB and is not revoked.

        When ``refresh_ttl == 0`` (never expires) the DB-level expiry
        check is skipped - the row's ``expires_at`` is a sentinel far
        future that we don't trust as a real boundary."""
        from digitorn_auth.models import RefreshToken

        async with self._session_factory() as session:
            stmt = select(RefreshToken).where(
                RefreshToken.user_id == user_id,
                RefreshToken.token_hash == token_hash,
                RefreshToken.revoked == False,
            )
            rt = (await session.execute(stmt)).scalar_one_or_none()
            if not rt:
                return False
            if self._jwt._refresh_ttl > 0:
                expires = rt.expires_at if rt.expires_at.tzinfo else rt.expires_at.replace(tzinfo=timezone.utc)
                if expires < datetime.now(timezone.utc):
                    return False
            return True

    async def _revoke_refresh_token(self, token_hash: str) -> bool:
        """Revoke a refresh token."""
        from digitorn_auth.models import RefreshToken

        async with self._session_factory() as session:
            stmt = (
                update(RefreshToken)
                .where(RefreshToken.token_hash == token_hash)
                .values(revoked=True)
            )
            result = await session.execute(stmt)
            await session.commit()
            return result.rowcount > 0

    async def _update_last_seen(self, user_id: str) -> None:
        """Update user's last_seen_at timestamp."""
        from digitorn_auth.models import User

        async with self._session_factory() as session:
            stmt = (
                update(User)
                .where(User.id == user_id)
                .values(last_seen_at=datetime.now(timezone.utc))
            )
            await session.execute(stmt)
            await session.commit()
