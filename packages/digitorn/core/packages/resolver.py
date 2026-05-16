"""Unified install-dir resolution.

Single source of truth for "where does this app live on disk?" used
by every code path that needs to read an app's files (preview warmup,
static dist serving, asset loading, etc).

Deterministic layout:

  - System scope: ``~/.digitorn/apps/<app_id>/``
  - User scope:   ``~/.digitorn/apps/_@<uid>__<app_id>/`` (via ``_scoped_slug``)
  - Source-tree builtins fallback: ``packages/digitorn/builtins/<app_id>/`` —
    used at first-boot before ``bootstrap_builtins`` has copied the
    package into its canonical location.

Returns ``None`` only when no candidate contains an ``app.yaml``.
"""

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
    """Deterministic install dir for ``app_id`` under
    ``~/.digitorn/apps/``.

    System: ``~/.digitorn/apps/<app_id>/``
    User:   ``~/.digitorn/apps/_@<uid>__<app_id>/``
    """
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
    """Resolve the on-disk install dir for ``app_id``.

    Lookup order:
      1. User-scoped dir (when ``user_id`` is set).
      2. System-scoped dir.
      3. Source-tree builtin (last-resort for first-boot).
    """
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
    except Exception:
        pass

    return None
