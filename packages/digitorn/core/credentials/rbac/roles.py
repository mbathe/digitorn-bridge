"""Role enumeration + scope permission matrix + FastAPI guards.

The role hierarchy is total-ordered: each role implicitly includes
the powers of all roles below it.

    daemon_admin > app_owner > user > viewer

`has_role(actual, required)` returns True when `actual >= required`.

The scope permission matrix is the single source of truth for "who
can do what":

                       | system_wide | per_app_shared | per_user | per_app_per_user
    daemon_admin       | RW          | RW             | RO*      | RO*
    app_owner (own app)| RO          | RW             | -        | -
    user               | RO          | RO             | RW       | RW
    viewer             | RO-meta     | RO-meta        | RO-meta  | RO-meta

*daemon_admin can READ user-scoped creds for support/debugging but
never write to them. Audit log captures every such read.

The matrix is encoded as data, not branches, so adding a new scope or
role only requires updating the constants here.
"""

from __future__ import annotations

import logging
from enum import Enum
from typing import Any

from fastapi import Depends, HTTPException, Request, status

logger = logging.getLogger(__name__)


class Role(str, Enum):
    DAEMON_ADMIN = "daemon_admin"
    APP_OWNER = "app_owner"
    USER = "user"
    VIEWER = "viewer"


# Total order. Higher = more privileged.
_ROLE_RANK = {
    Role.VIEWER: 0,
    Role.USER: 1,
    Role.APP_OWNER: 2,
    Role.DAEMON_ADMIN: 3,
}


# Scope strings (kept in sync with credentials.store.Scope).
SCOPE_SYSTEM_WIDE = "system_wide"
SCOPE_PER_APP_SHARED = "per_app_shared"
SCOPE_PER_USER = "per_user"
SCOPE_PER_APP_PER_USER = "per_app_per_user"


# Permission matrix. Each entry is a tuple of allowed Roles.
# `None` for a (role, scope) means "denied" - we use empty set for
# clarity.
_READ_PERMISSIONS: dict[str, set[Role]] = {
    SCOPE_SYSTEM_WIDE: {
        Role.DAEMON_ADMIN, Role.APP_OWNER, Role.USER, Role.VIEWER,
    },
    SCOPE_PER_APP_SHARED: {
        Role.DAEMON_ADMIN, Role.APP_OWNER, Role.USER, Role.VIEWER,
    },
    SCOPE_PER_USER: {
        Role.DAEMON_ADMIN, Role.USER, Role.VIEWER,
    },
    SCOPE_PER_APP_PER_USER: {
        Role.DAEMON_ADMIN, Role.USER, Role.VIEWER,
    },
}

_WRITE_PERMISSIONS: dict[str, set[Role]] = {
    SCOPE_SYSTEM_WIDE: {Role.DAEMON_ADMIN},
    SCOPE_PER_APP_SHARED: {Role.DAEMON_ADMIN, Role.APP_OWNER},
    SCOPE_PER_USER: {Role.DAEMON_ADMIN, Role.USER},
    SCOPE_PER_APP_PER_USER: {Role.DAEMON_ADMIN, Role.USER},
}


def has_role(actual: Role | str, required: Role | str) -> bool:
    """True when `actual` is at least as privileged as `required`."""
    a = actual if isinstance(actual, Role) else Role(actual)
    r = required if isinstance(required, Role) else Role(required)
    return _ROLE_RANK[a] >= _ROLE_RANK[r]


def can_read_scope(role: Role | str, scope: str) -> bool:
    """True when the role can READ credentials at the given scope.

    Note: 'read' here means seeing metadata (status, label, masked
    preview). Decrypting fields is gated additionally by ownership
    checks (a user reading another user's per_user cred is denied
    even if both have role=user; the API layer enforces ownership)."""
    r = role if isinstance(role, Role) else Role(role)
    return r in _READ_PERMISSIONS.get(scope, set())


def can_write_scope(role: Role | str, scope: str) -> bool:
    """True when the role can CREATE / UPDATE / DELETE credentials
    at the given scope. App-owner ownership of the specific app is
    NOT checked here (caller responsibility - they know which app)."""
    r = role if isinstance(role, Role) else Role(role)
    return r in _WRITE_PERMISSIONS.get(scope, set())


# ─── FastAPI dependencies ──────────────────────────────────────────


def _get_role_from_request(request: Request) -> Role:
    """Extract the caller's role from the auth middleware-populated
    `request.state`. The auth middleware sets `state.role` to the
    string value of the Role enum (or `viewer` for unauthenticated
    /shared paths)."""
    raw = getattr(request.state, "role", Role.VIEWER.value)
    try:
        return Role(raw)
    except ValueError:
        # Unknown role string → treat as viewer (least privilege).
        logger.warning("unknown_role_in_request actual=%r → viewer", raw)
        return Role.VIEWER


def require_role(min_role: Role) -> Any:
    """FastAPI dependency: 403 when the caller's role is below
    `min_role`. Use as `@router.get(..., dependencies=[Depends(require_role(Role.DAEMON_ADMIN))])`.
    """
    def _dep(request: Request) -> None:
        actual = _get_role_from_request(request)
        if not has_role(actual, min_role):
            logger.info(
                "rbac_denied actual=%s required=%s path=%s",
                actual.value, min_role.value, request.url.path,
            )
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"requires role {min_role.value} (have {actual.value})",
            )
    return Depends(_dep)


def require_scope_read(scope: str) -> Any:
    """FastAPI dependency: 403 when the role can't read the scope."""
    def _dep(request: Request) -> None:
        actual = _get_role_from_request(request)
        if not can_read_scope(actual, scope):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"role {actual.value} cannot read scope {scope}",
            )
    return Depends(_dep)


def require_scope_write(scope: str) -> Any:
    """FastAPI dependency: 403 when the role can't write the scope."""
    def _dep(request: Request) -> None:
        actual = _get_role_from_request(request)
        if not can_write_scope(actual, scope):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"role {actual.value} cannot write scope {scope}",
            )
    return Depends(_dep)
