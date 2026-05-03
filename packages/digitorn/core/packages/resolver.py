"""Unified install-dir resolution.

Single source of truth for "where does this app live on disk?" used
by every code path that needs to read an app's bundle (preview
warmup, static dist serving, asset loading, etc).

Resolution chain (most specific wins):

  1. **Registry USER scope** (when ``user_id`` is provided) - per-user
     overrides shadow the system install, same convention as Linux
     ``~/.local/bin`` overriding ``/usr/local/bin``.
  2. **Registry SYSTEM scope** - the canonical shared install for
     all users on this daemon.
  3. **Disk USER location** - ``~/.digitorn/users/{user_id}/packages/{app_id}/``.
     Picked up when the registry row hasn't been written yet (boot
     race, fresh install in progress).
  4. **Disk SYSTEM location** - ``~/.digitorn/packages/{app_id}/``.
     Same rationale: the canonical system install dir, even when
     the registry hasn't been populated.
  5. **Source-tree builtins** - ``packages/digitorn/builtins/{app_id}/``.
     Last resort for builtins that ship with the daemon but
     ``bootstrap_builtins`` hasn't yet copied them to the canonical
     location (first-boot race, broken bootstrap, dev mode).

Returns ``None`` only when none of the five resolves to a directory
containing an ``app.yaml``. Caller can then 404 / skip preview / etc.

The function is async because step 1 + 2 hit the database via the
package registry. Steps 3-5 are pure ``stat`` calls.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def _has_app_yaml(p: Path) -> bool:
    """True when ``p`` looks like a valid app install directory."""
    try:
        return (p / "app.yaml").is_file()
    except OSError:
        return False


async def resolve_app_install_dir(
    app_id: str,
    *,
    user_id: str | None = None,
    registry: Any | None = None,
) -> Path | None:
    """Resolve the on-disk install directory for ``app_id``.

    Args:
        app_id: The package id (e.g. ``digitorn-react-sandbox``).
        user_id: When set, USER-scoped installs for this user are
            checked FIRST. The convention is "user shadows system".
            When ``None``, only system + disk fallbacks are tried.
        registry: Optional ``PackageRegistry``. When None, only the
            disk fallbacks (steps 3-5) are tried - useful in code
            paths that don't have access to the registry instance
            (e.g. early bootstrap, tests).

    Returns:
        The resolved ``Path`` to the install dir, or ``None`` if none
        of the five candidates exists.
    """
    if not app_id:
        return None

    # ── 1 + 2: Registry lookups ──────────────────────────────────
    if registry is not None:
        try:
            from digitorn.core.packages.registry import Scope

            if user_id:
                row = await registry.get(
                    app_id, scope=Scope.USER, owner_user_id=user_id,
                )
                p = _path_from_row(row)
                if p is not None and _has_app_yaml(p):
                    return p

            row = await registry.get(app_id, scope=Scope.SYSTEM)
            p = _path_from_row(row)
            if p is not None and _has_app_yaml(p):
                return p
        except Exception as exc:
            # Defensive: a registry blip mustn't 500 the warmup or
            # the preview-serve route. Fall through to disk lookups.
            logger.debug(
                "resolver_registry_lookup_failed app=%s user=%s: %s",
                app_id, user_id, exc,
            )

    # ── 3: Disk USER location ────────────────────────────────────
    if user_id:
        try:
            user_disk = (
                Path.home() / ".digitorn" / "users" / user_id
                / "packages" / app_id
            )
            if _has_app_yaml(user_disk):
                logger.debug(
                    "resolver_fallback_user_disk app=%s user=%s path=%s",
                    app_id, user_id, user_disk,
                )
                return user_disk
        except Exception:
            pass

    # ── 4: Disk SYSTEM location ──────────────────────────────────
    try:
        system_disk = Path.home() / ".digitorn" / "packages" / app_id
        if _has_app_yaml(system_disk):
            logger.debug(
                "resolver_fallback_system_disk app=%s path=%s",
                app_id, system_disk,
            )
            return system_disk
    except Exception:
        pass

    # ── 5: Source-tree builtins ──────────────────────────────────
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


def _path_from_row(row: Any) -> Path | None:
    """Extract ``install_dir`` from a registry row (dict or ORM)."""
    if row is None:
        return None
    if isinstance(row, dict):
        install_dir = row.get("install_dir")
    else:
        install_dir = getattr(row, "install_dir", None)
    if not install_dir:
        return None
    try:
        return Path(install_dir)
    except Exception:
        return None
