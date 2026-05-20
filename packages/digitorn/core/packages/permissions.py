"""`package.install` capability - gate on every install/upgrade/uninstall route."""

from __future__ import annotations

from typing import Iterable

from fastapi import HTTPException, Request

# The capability string. Add it to whatever permission catalog the
# daemon ships (currently the catalog is implicit - permissions are
# free-form strings on a user's profile, with `*` meaning admin).
PACKAGE_INSTALL_CAPABILITY = "package.install"

def has_install_permission(perms: Iterable[str] | None) -> bool:
    """True iff the user can install / upgrade / uninstall packages."""
    if not perms:
        return False
    perms_set = set(perms)
    return "*" in perms_set or PACKAGE_INSTALL_CAPABILITY in perms_set

def require_install_permission(request: Request) -> None:
    """FastAPI guard - raise 403 if the caller can't install."""
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
