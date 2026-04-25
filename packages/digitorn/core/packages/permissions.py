"""``package.install`` capability — gate on every install/upgrade/uninstall route.

Locked design D11: no "open dev mode". Even on a solo machine the
first user (admin) inherits the permission via their ``*`` wildcard,
so the installer never has to set anything up — but a future
multi-user deployment can revoke ``package.install`` from regular
users without code changes.

This module is intentionally tiny: one constant and one helper. The
helper is called from the route layer (see ``core/api/packages.py``
in Phase C). The Phase A foundation only needs the helper to exist
so the registry / install flow can validate independently.
"""

from __future__ import annotations

from typing import Iterable

from fastapi import HTTPException, Request


# The capability string. Add it to whatever permission catalog the
# daemon ships (currently the catalog is implicit — permissions are
# free-form strings on a user's profile, with ``*`` meaning admin).
PACKAGE_INSTALL_CAPABILITY = "package.install"


def has_install_permission(perms: Iterable[str] | None) -> bool:
    """True iff the user can install / upgrade / uninstall packages.

    Two ways to qualify:

    1. ``*`` wildcard (admin) — bypasses every permission check
    2. Explicit ``package.install`` in the user's perms list
    """
    if not perms:
        return False
    perms_set = set(perms)
    return "*" in perms_set or PACKAGE_INSTALL_CAPABILITY in perms_set


def require_install_permission(request: Request) -> None:
    """FastAPI guard — raise 403 if the caller can't install.

    Reads ``request.state.permissions`` which is populated by the
    auth middleware. Raises ``HTTPException(403)`` with a clear
    actionable error message when the check fails.
    """
    perms = getattr(request.state, "permissions", None) or []
    if not has_install_permission(perms):
        raise HTTPException(
            status_code=403,
            detail=(
                "Installing or modifying app packages requires the "
                f"'{PACKAGE_INSTALL_CAPABILITY}' capability or admin "
                f"permission ('*'). Ask your administrator to grant it "
                f"to your user."
            ),
        )
