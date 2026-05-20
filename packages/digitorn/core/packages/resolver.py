"""Unified install-dir resolution."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

def _has_app_yaml(p: Path) -> bool:
    try:
        return (p / "app.yaml").is_file()
    except OSError:
        return False

def _app_dir(app_id: str, *, user_id: str | None = None) -> Path:
    from digitorn.core.app.manager_v2._models import _scoped_slug

    scope = "user" if user_id else "system"
    slug = _scoped_slug(app_id, scope, user_id or "")
    return Path.home() / ".digitorn" / "apps" / slug

async def resolve_app_install_dir(
    app_id: str,
    *,
    user_id: str | None = None,
    registry: Any | None = None,  # accepted for signature compat; unused
) -> Path | None:
    """Resolve the on-disk install dir for `app_id`."""
    if not app_id:
        return None

    if user_id:
        p = _app_dir(app_id, user_id=user_id)
        if _has_app_yaml(p):
            return p

    p = _app_dir(app_id)
    if _has_app_yaml(p):
        return p

    try:
        import digitorn as _digitorn_pkg

        builtins_root = (
            Path(_digitorn_pkg.__file__).parent / "builtins" / app_id
        )
        if _has_app_yaml(builtins_root):
            logger.debug(
                "resolver_fallback_builtin app=%s path=%s",
                app_id, builtins_root,
            )
            return builtins_root
    except Exception as exc:
        logger.debug("resolver best-effort block failed: %s", exc)

    return None
