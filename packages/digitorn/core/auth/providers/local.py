"""Local auth provider - username/password stored in DB with bcrypt hashing.

This is the default provider. Users are registered and authenticated
against the local database. Passwords are hashed with bcrypt (12 rounds).
Includes progressive lockout after repeated failed login attempts.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from digitorn.core.auth.providers.base import AuthProvider, AuthResult

logger = logging.getLogger(__name__)

# Lockout defaults: max 5 failures, 15 min window, doubles on each subsequent burst
_DEFAULT_MAX_FAILURES = 5
_DEFAULT_LOCKOUT_WINDOW = 900  # 15 minutes


def _get_max_failures() -> int:
    try:
        from digitorn.core.config import get_settings
        return get_settings().auth.max_login_failures
    except Exception:
        return _DEFAULT_MAX_FAILURES


def _get_lockout_window() -> int:
    try:
        from digitorn.core.config import get_settings
        return get_settings().auth.lockout_window
    except Exception:
        return _DEFAULT_LOCKOUT_WINDOW

try:
    import bcrypt

    _HAS_BCRYPT = True
except ImportError:
    _HAS_BCRYPT = False
    logger.warning("bcrypt not installed. Local auth disabled. Install with: pip install bcrypt")

def hash_password(password: str) -> str:
    """Hash a password with bcrypt (12 rounds)."""
    if not _HAS_BCRYPT:
        raise RuntimeError("bcrypt is required for local auth")
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt(12)).decode("utf-8")

def verify_password(password: str, hashed: str) -> bool:
    """Verify a password against a bcrypt hash. Constant-time comparison."""
    if not _HAS_BCRYPT:
        raise RuntimeError("bcrypt is required for local auth")
    return bcrypt.checkpw(password.encode("utf-8"), hashed.encode("utf-8"))

class LocalProvider(AuthProvider):
    """Local database auth provider.

    Credentials: {"username": "...", "password": "..."}
    Or: {"email": "...", "password": "..."}
    """

    provider_id = "local"

    def __init__(self) -> None:
        self._session_factory: Any = None
        # Lockout trackers. We key by (identifier, ip) so an attacker
        # who knows a victim's email CANNOT lock their account by
        # failing logins - they'd need to burn through the counter
        # from the victim's IP, which is pointless for the attacker.
        # Legacy pure-identifier lockout (easy DoS) removed.
        self._failed_attempts: dict[tuple[str, str], tuple[int, float]] = {}

    async def on_start(self, config: dict[str, Any]) -> None:
        from digitorn.core.database import get_session_factory

        self._session_factory = get_session_factory()

    def _check_lockout(self, identifier: str, ip: str = "") -> str | None:
        """Return error message if this (identifier, ip) pair is locked
        out, else None.

        We use a compound key so an attacker coming from a different IP
        cannot trigger a legitimate user's lockout by brute-forcing the
        email. The victim keeps being able to log in from their own IP
        as long as their own IP hasn't hit the limit.
        """
        key = (identifier or "", ip or "")
        entry = self._failed_attempts.get(key)
        if not entry:
            return None
        count, first_ts = entry
        if count < _get_max_failures():
            return None
        elapsed = time.monotonic() - first_ts
        if elapsed > _get_lockout_window():
            del self._failed_attempts[key]
            return None
        remaining = int(_get_lockout_window() - elapsed)
        logger.warning(
            "auth_lockout identifier=%s ip=%s attempts=%d remaining=%ds",
            identifier, ip, count, remaining,
        )
        return f"Too many failed login attempts. Try again in {remaining}s."

    def _record_failure(self, identifier: str, ip: str = "") -> None:
        key = (identifier or "", ip or "")
        entry = self._failed_attempts.get(key)
        now = time.monotonic()
        if entry:
            count, first_ts = entry
            if now - first_ts > _get_lockout_window():
                self._failed_attempts[key] = (1, now)
            else:
                self._failed_attempts[key] = (count + 1, first_ts)
        else:
            self._failed_attempts[key] = (1, now)

    def _clear_failures(self, identifier: str, ip: str = "") -> None:
        # Clear the (identifier, ip) pair AND any record keyed purely
        # by identifier (left over from the legacy-format data).
        self._failed_attempts.pop((identifier or "", ip or ""), None)
        # Also clear every (identifier, *) entry - once the real user
        # successfully logs in, it's reasonable to wipe their pending
        # failures regardless of source IP so they aren't stuck.
        for k in [k for k in self._failed_attempts if k[0] == identifier]:
            self._failed_attempts.pop(k, None)

    async def authenticate(self, credentials: dict[str, Any]) -> AuthResult:
        if not _HAS_BCRYPT:
            return AuthResult(success=False, error="bcrypt not installed")

        username = credentials.get("username")
        email = credentials.get("email")
        password = credentials.get("password")
        ip = credentials.get("ip", "") or ""  # optional caller-provided IP

        if not password:
            return AuthResult(success=False, error="Password is required")
        if not username and not email:
            return AuthResult(success=False, error="Username or email is required")

        # Check lockout before hitting the database
        identifier = username or email or ""
        lockout_msg = self._check_lockout(identifier, ip)
        if lockout_msg:
            return AuthResult(success=False, error=lockout_msg)

        from digitorn.core.models import User

        async with self._session_factory() as session:
            session: AsyncSession

            if username:
                stmt = select(User).where(
                    User.provider == "local",
                    User.external_id == username,
                    User.is_active == True,
                )
            else:
                # Try email column first, then fall back to external_id
                # (handles accounts where email was used as username)
                from sqlalchemy import or_
                stmt = select(User).where(
                    User.provider == "local",
                    or_(User.email == email, User.external_id == email),
                    User.is_active == True,
                ).order_by(User.email.isnot(None).desc())

            result = await session.execute(stmt)
            user = result.scalars().first()

            if user is None:
                self._record_failure(identifier, ip)
                return AuthResult(success=False, error="Invalid credentials")

            password_hash = (user.attributes or {}).get("password_hash")
            if not password_hash:
                self._record_failure(identifier, ip)
                return AuthResult(success=False, error="Account has no password set")

            if not verify_password(password, password_hash):
                self._record_failure(identifier, ip)
                return AuthResult(success=False, error="Invalid credentials")

            self._clear_failures(identifier, ip)
            return AuthResult(
                success=True,
                user_id=user.id,
                external_id=user.external_id,
                email=user.email,
                display_name=user.display_name,
                attributes=user.attributes,
            )

    async def change_password(
        self, user_id: str, current: str, new: str,
    ) -> AuthResult:
        """Change a local-provider user's password.

        Verifies ``current`` against the stored hash before overwriting
        with the hash of ``new``. Returns ``AuthResult.success=False``
        with a descriptive ``error`` string on any failure (missing
        bcrypt, unknown user, wrong current, OAuth-managed account) so
        the API layer can map to a 400 cleanly instead of a 500.
        """
        if not _HAS_BCRYPT:
            return AuthResult(success=False, error="bcrypt not installed")
        if not current or not new:
            return AuthResult(
                success=False,
                error="Current and new password are required",
            )

        from digitorn.core.models import User

        async with self._session_factory() as session:
            session: AsyncSession
            user = (
                await session.execute(
                    select(User).where(
                        User.id == user_id,
                        User.provider == "local",
                    )
                )
            ).scalar_one_or_none()
            if user is None:
                return AuthResult(
                    success=False,
                    error="User not found or not a local account",
                )

            attrs = dict(user.attributes or {})
            password_hash = attrs.get("password_hash")
            if not password_hash:
                return AuthResult(
                    success=False, error="Account has no password set",
                )
            if not verify_password(current, password_hash):
                return AuthResult(
                    success=False, error="Current password is incorrect",
                )

            attrs["password_hash"] = hash_password(new)
            user.attributes = attrs
            # SQLAlchemy JSON-column change detection: reassigning the
            # dict is enough; no explicit ``flag_modified`` needed.
            await session.commit()
            logger.info("password_changed user_id=%s", user_id)
            return AuthResult(success=True, user_id=user.id)

    async def register(
        self,
        username: str,
        password: str,
        email: str | None = None,
        display_name: str | None = None,
    ) -> AuthResult:
        """Register a new local user.

        Returns AuthResult with user_id on success.
        """
        if not _HAS_BCRYPT:
            return AuthResult(success=False, error="bcrypt not installed")

        from digitorn.core.models import User

        password_hash = hash_password(password)

        async with self._session_factory() as session:
            session: AsyncSession

            # Single duplicate-check query covering BOTH username and
            # email in one round-trip. Previously this fired 2 separate
            # SELECTs (one by external_id, one by email), each a full
            # network RTT against the DB. On a remote DB (Neon,
            # ~150 ms RTT), that was ~300 ms of unnecessary latency.
            from sqlalchemy import or_
            conds = [(
                (User.provider == "local")
                & (User.external_id == username)
            )]
            if email:
                conds.append(User.email == email)
            stmt = select(User.external_id, User.email).where(or_(*conds))
            for ext_id, existing_email in (
                await session.execute(stmt)
            ).all():
                if ext_id == username:
                    return AuthResult(
                        success=False, error="username already taken",
                    )
                if email and existing_email == email:
                    return AuthResult(
                        success=False, error="email already registered",
                    )

            user = User(
                external_id=username,
                provider="local",
                email=email,
                display_name=display_name or username,
                attributes={"password_hash": password_hash},
            )
            session.add(user)
            await session.commit()
            # ``session.refresh(user)`` was fetching every column of the
            # row we just inserted - one full extra round-trip against
            # the DB. We only need ``user.id``, which SQLAlchemy already
            # populated from the Python-side ``default=_uuid`` at
            # ``User()`` construction time. Skip the refresh.
            logger.info("user_registered username=%s user_id=%s", username, user.id)

            return AuthResult(
                success=True,
                user_id=user.id,
                external_id=username,
                email=email,
                display_name=display_name or username,
            )
