"""LDAP/Active Directory auth provider.

Authenticates users against an LDAP directory (Active Directory,
OpenLDAP, FreeIPA). Supports auto-provisioning - users are created
in the local DB on first login.

Requires: pip install ldap3
"""

from __future__ import annotations

import logging
from typing import Any

from digitorn_auth.providers.base import AuthProvider, AuthResult

logger = logging.getLogger(__name__)

try:
    from ldap3 import Connection, Server, SUBTREE, ALL

    _HAS_LDAP = True
except ImportError:
    _HAS_LDAP = False

class LDAPProvider(AuthProvider):
    """LDAP/Active Directory authentication.

    Config:
        server: LDAP server URL (ldap://ad.company.com or ldaps://...)
        base_dn: Base DN for user search (dc=company,dc=com)
        bind_dn: Service account DN for search (optional for anonymous bind)
        bind_password: Service account password
        user_search: LDAP filter template ({username} is replaced)
        email_attr: Attribute for email (default: mail)
        name_attr: Attribute for display name (default: displayName)
        auto_provision: Create local user on first login (default: true)
    """

    provider_id = "ldap"

    def __init__(self) -> None:
        self._server_url: str = ""
        self._base_dn: str = ""
        self._bind_dn: str | None = None
        self._bind_password: str | None = None
        self._user_search: str = "(sAMAccountName={username})"
        self._email_attr: str = "mail"
        self._name_attr: str = "displayName"
        self._auto_provision: bool = True
        self._session_factory: Any = None

    async def on_start(self, config: dict[str, Any]) -> None:
        if not _HAS_LDAP:
            logger.error("ldap3 not installed. Install with: pip install ldap3")
            return

        self._server_url = config.get("server", "")
        self._base_dn = config.get("base_dn", "")
        self._bind_dn = config.get("bind_dn")
        self._bind_password = config.get("bind_password")
        self._user_search = config.get("user_search", "(sAMAccountName={username})")
        self._email_attr = config.get("email_attr", "mail")
        self._name_attr = config.get("name_attr", "displayName")
        self._auto_provision = config.get("auto_provision", True)

        from digitorn_auth.database import get_session_factory
        self._session_factory = get_session_factory()

    async def authenticate(self, credentials: dict[str, Any]) -> AuthResult:
        if not _HAS_LDAP:
            return AuthResult(success=False, error="ldap3 not installed")

        username = credentials.get("username", "")
        password = credentials.get("password", "")

        if not username or not password:
            return AuthResult(success=False, error="Username and password required")

        try:
            server = Server(self._server_url, get_info=ALL)

            if self._bind_dn:
                search_conn = Connection(
                    server, self._bind_dn, self._bind_password, auto_bind=True,
                )
            else:
                search_conn = Connection(server, auto_bind=True)

            from ldap3.utils.conv import escape_filter_chars
            safe_username = escape_filter_chars(username)
            search_filter = self._user_search.replace("{username}", safe_username)
            search_conn.search(
                self._base_dn,
                search_filter,
                search_scope=SUBTREE,
                attributes=[self._email_attr, self._name_attr, "distinguishedName"],
            )

            if not search_conn.entries:
                search_conn.unbind()
                return AuthResult(success=False, error="Invalid credentials")

            entry = search_conn.entries[0]
            user_dn = str(entry.entry_dn)
            email = str(getattr(entry, self._email_attr, "")) or None
            display_name = str(getattr(entry, self._name_attr, "")) or username
            search_conn.unbind()

            user_conn = Connection(server, user_dn, password, auto_bind=True)
            user_conn.unbind()

            user_id = None
            if self._auto_provision and self._session_factory:
                user_id = await self._ensure_user(username, email, display_name)

            return AuthResult(
                success=True,
                user_id=user_id,
                external_id=username,
                email=email,
                display_name=display_name,
            )

        except Exception as exc:
            logger.debug("ldap_auth_failed username=%s error=%s", username, exc)
            return AuthResult(success=False, error="Invalid credentials")

    async def _ensure_user(self, username: str, email: str | None, display_name: str) -> str:
        """Create or update user in local DB. Returns user_id."""
        from digitorn_auth.models import User
        from sqlalchemy import select

        async with self._session_factory() as session:
            stmt = select(User).where(
                User.provider == "ldap",
                User.external_id == username,
            )
            user = (await session.execute(stmt)).scalar_one_or_none()

            if user:
                user.email = email
                user.display_name = display_name
                await session.commit()
                return user.id

            user = User(
                external_id=username,
                provider="ldap",
                email=email,
                display_name=display_name,
            )
            session.add(user)
            await session.commit()
            await session.refresh(user)
            logger.info("ldap_user_provisioned username=%s user_id=%s", username, user.id)
            return user.id
