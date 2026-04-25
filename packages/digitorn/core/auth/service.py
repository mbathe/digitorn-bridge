"""AuthService — central authentication and authorization service.

Orchestrates auth providers, JWT tokens, roles, and sessions.
Stateless where possible (JWT), stateful for refresh tokens and roles (DB).
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from digitorn.core.auth.jwt import JWTService, TokenPayload
from digitorn.core.auth.providers.base import AuthProvider, AuthResult

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
        """Initialize the auth service with configuration."""
        from digitorn.core.database import get_session_factory

        self._session_factory = get_session_factory()

        providers_config = config.get("providers", [{"type": "local", "default": True}])
        for prov_config in providers_config:
            prov_type = prov_config.get("type", "local")
            provider = self._create_provider(prov_type)
            if provider:
                await provider.on_start(prov_config.get("config", {}))
                self._providers[prov_type] = provider
                if prov_config.get("default"):
                    self._default_provider = prov_type
                logger.info("auth_provider_loaded type=%s", prov_type)

        await self._ensure_builtin_roles()
        await self._ensure_default_admin()

    def get_provider(self, name: str) -> "AuthProvider | None":
        """Retrieve a registered auth provider by name."""
        return self._providers.get(name)

    async def stop(self) -> None:
        """Cleanup all providers."""
        for provider in self._providers.values():
            await provider.on_stop()

    def _create_provider(self, prov_type: str) -> AuthProvider | None:
        """Factory for auth providers."""
        if prov_type == "local":
            from digitorn.core.auth.providers.local import LocalProvider
            return LocalProvider()
        elif prov_type == "ldap":
            from digitorn.core.auth.providers.ldap import LDAPProvider
            return LDAPProvider()
        elif prov_type in ("oauth2", "oidc"):
            from digitorn.core.auth.providers.oauth2 import OAuth2Provider
            return OAuth2Provider()
        elif prov_type == "api_key":
            from digitorn.core.auth.providers.api_key import APIKeyProvider
            return APIKeyProvider()
        else:
            logger.warning("auth_unknown_provider type=%s", prov_type)
            return None
    async def login(
        self,
        credentials: dict[str, Any],
        provider: str | None = None,
    ) -> LoginResult:
        """Authenticate a user and return JWT tokens."""
        prov_id = provider or self._default_provider
        prov = self._providers.get(prov_id)
        if not prov:
            return LoginResult(success=False, error=f"Auth provider '{prov_id}' not configured")

        result = await prov.authenticate(credentials)
        if not result.success:
            return LoginResult(success=False, error=result.error)

        roles = await self._get_user_roles(result.user_id)
        permissions = await self._get_user_permissions(result.user_id)

        access_token = self._jwt.generate_access_token(
            user_id=result.user_id,
            email=result.email,
            display_name=result.display_name,
            roles=roles,
            permissions=permissions,
        )
        refresh_token, refresh_hash = self._jwt.generate_refresh_token(result.user_id)

        await self._store_refresh_token(result.user_id, refresh_hash)

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
            expires_in=self._jwt._access_ttl,
        )

    async def register(
        self,
        username: str,
        password: str,
        email: str | None = None,
        display_name: str | None = None,
    ) -> LoginResult:
        """Register a new local user and return tokens."""
        prov = self._providers.get("local")
        if not prov:
            return LoginResult(success=False, error="Local auth provider not configured")

        from digitorn.core.auth.providers.local import LocalProvider
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
        await self._assign_role(result.user_id, role_to_assign)
        if role_to_assign == "admin":
            logger.info(
                "bootstrap_admin user_id=%s username=%s — first user, "
                "granted admin role automatically",
                result.user_id, username,
            )

        return await self.login({"username": username, "password": password})

    async def refresh(self, refresh_token: str) -> LoginResult:
        """Exchange a refresh token for a new access + refresh token pair.

        The old refresh token is revoked (one-time use).  This limits the
        exposure window if a refresh token is leaked.
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

        # Revoke the old refresh token (one-time use)
        await self._revoke_refresh_token(token_hash)

        roles = await self._get_user_roles(payload.user_id)
        permissions = await self._get_user_permissions(payload.user_id)

        access_token = self._jwt.generate_access_token(
            user_id=payload.user_id,
            email=payload.email,
            display_name=payload.display_name,
            roles=roles,
            permissions=permissions,
        )

        # Issue a new refresh token
        new_refresh, new_hash = self._jwt.generate_refresh_token(payload.user_id)
        await self._store_refresh_token(payload.user_id, new_hash)

        return LoginResult(
            success=True,
            access_token=access_token,
            refresh_token=new_refresh,
            user_id=payload.user_id,
            email=payload.email,
            display_name=payload.display_name,
            roles=roles,
            permissions=permissions,
            expires_in=self._jwt._access_ttl,
        )

    async def logout(
        self, refresh_token: str | None = None, access_token: str | None = None,
    ) -> bool:
        """Revoke a refresh token AND/OR an access token (logout).

        Both arguments are optional: the API accepts a missing body, in
        which case only the Authorization header's access token gets
        revoked. A stolen bearer must not stay usable for its full
        15-min TTL after an explicit logout.
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
                    # Keep the jti in the deny-list only until its exp —
                    # pyjwt rejects expired tokens anyway, so past that
                    # point the entry is pure dead weight.
                    self._revoked_jtis[payload.jti] = float(payload.expires_at or 0)
                    any_revoked = True
            except Exception:
                logger.debug("access revoke failed", exc_info=True)
            self._gc_revocations()
        return any_revoked

    def _gc_revocations(self) -> None:
        """Drop revocations whose original exp is already in the past."""
        now = time.time()
        stale = [jti for jti, exp in self._revoked_jtis.items() if exp and exp < now]
        for jti in stale:
            self._revoked_jtis.pop(jti, None)

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
        from digitorn.core.models import Role, UserRole

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
        from digitorn.core.models import Role, UserRole

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

    async def _any_user_has_role(self, role_name: str) -> bool:
        """Return True if at least one user currently has `role_name`.

        Used by the bootstrap-admin path during registration — we want
        the very first user to self-promote to admin so solo deployments
        have a working config-edit flow out of the box.
        """
        from digitorn.core.models import Role, UserRole

        async with self._session_factory() as session:
            stmt = (
                select(UserRole)
                .join(Role, Role.id == UserRole.role_id)
                .where(Role.name == role_name)
                .limit(1)
            )
            hit = (await session.execute(stmt)).scalar_one_or_none()
            return hit is not None

    async def _assign_role(self, user_id: str, role_name: str, granted_by: str | None = None) -> bool:
        """Assign a role to a user."""
        from digitorn.core.models import Role, UserRole

        async with self._session_factory() as session:
            stmt = select(Role).where(Role.name == role_name)
            role = (await session.execute(stmt)).scalar_one_or_none()
            if not role:
                return False

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
        from digitorn.core.models import Role

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
        from digitorn.core.models import User

        async with self._session_factory() as session:
            stmt = select(User).limit(1)
            existing = (await session.execute(stmt)).scalar_one_or_none()
            if existing:
                return

        prov = self._providers.get("local")
        if not prov:
            return

        from digitorn.core.auth.providers.local import LocalProvider
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
                "default_admin_created username=admin password=admin1234admin — CHANGE THIS PASSWORD"
            )

    async def _store_refresh_token(self, user_id: str, token_hash: str) -> None:
        """Store a refresh token hash in DB."""
        from digitorn.core.models import RefreshToken

        async with self._session_factory() as session:
            rt = RefreshToken(
                user_id=user_id,
                token_hash=token_hash,
                expires_at=datetime.fromtimestamp(
                    time.time() + self._jwt._refresh_ttl, tz=timezone.utc,
                ),
            )
            session.add(rt)
            await session.commit()

    async def _verify_refresh_token(self, user_id: str, token_hash: str) -> bool:
        """Check if a refresh token exists in DB and is not revoked."""
        from digitorn.core.models import RefreshToken

        async with self._session_factory() as session:
            stmt = select(RefreshToken).where(
                RefreshToken.user_id == user_id,
                RefreshToken.token_hash == token_hash,
                RefreshToken.revoked == False,
            )
            rt = (await session.execute(stmt)).scalar_one_or_none()
            if not rt:
                return False
            expires = rt.expires_at if rt.expires_at.tzinfo else rt.expires_at.replace(tzinfo=timezone.utc)
            if expires < datetime.now(timezone.utc):
                return False
            return True

    async def _revoke_refresh_token(self, token_hash: str) -> bool:
        """Revoke a refresh token."""
        from digitorn.core.models import RefreshToken

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
        from digitorn.core.models import User

        async with self._session_factory() as session:
            stmt = (
                update(User)
                .where(User.id == user_id)
                .values(last_seen_at=datetime.now(timezone.utc))
            )
            await session.execute(stmt)
            await session.commit()
