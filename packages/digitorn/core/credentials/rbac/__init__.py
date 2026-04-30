"""RBAC for credential operations.

Four roles, mapped on the `users` table via the `role` column:

  - `daemon_admin`: CRUD on every scope, audit access, KMS rotation
  - `app_owner`:    CRUD on `per_app_shared` for apps they own
                    + RO on `system_wide`
  - `user`:         CRUD on their own `per_user` + `per_app_per_user`
  - `viewer`:       RO on their own credentials (status only, never fields)

The role granted to a user determines which API endpoints they can
hit, which scopes they can write to, and what they see in the audit
log.

Public surface:

    Role           enum
    has_role       check if a user has at least the given role level
    require_role   FastAPI dependency that 403s on insufficient role
    can_read_scope / can_write_scope  scope-level checks
"""

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
