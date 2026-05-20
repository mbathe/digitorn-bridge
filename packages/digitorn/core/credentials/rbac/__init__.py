"""RBAC for credential operations."""

from __future__ import annotations

from digitorn.core.credentials.rbac.roles import (
    Role,
    can_read_scope,
    can_write_scope,
    has_role,
    require_role,
    require_scope_read,
    require_scope_write,
)

__all__ = [
    "Role",
    "can_read_scope",
    "can_write_scope",
    "has_role",
    "require_role",
    "require_scope_read",
    "require_scope_write",
]
