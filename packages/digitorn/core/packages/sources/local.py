"""LocalSource - install a package from a directory on disk.

The user points the daemon at any directory containing a
``package.toml`` and the daemon installs it. Two modes:

- **copy** (default): the daemon copies the entire directory into
  ``~/.digitorn/packages/<id>/``. The original is no longer
  consulted - edits to the user's source tree don't affect the
  installed copy.
- **symlink**: the install dir becomes a symlink to the user's
  directory. Edits to the source are immediately visible to the
  daemon. **Not recommended for production**, but excellent for
  iterating on a package being developed in place.

Source URI format: ``file://<absolute-path>``.
"""

from __future__ import annotations

import logging
import os
import shutil
from pathlib import Path

from digitorn.core.packages.manifest import PackageManifest
from digitorn.core.packages.source import (
    AvailablePackage,
    FetchError,
    PackageSource,
)


def _parse_local_uri(source_uri: str) -> Path:
    """Normalise every reasonable file URI / raw path into a Path.

    Handles the four common forms (POSIX RFC-8089, Windows
    forward-slash file://, Windows backslash file://, raw paths)
    so the user can type whatever's natural on their OS without
    having to remember the exact RFC.
    """
    if not source_uri.startswith("file://"):
        return Path(source_uri)

    raw = source_uri[len("file://"):]
    # Form 1: file:///C:/... or file:///abs/path  (RFC-compliant)
    if raw.startswith("/"):
        # On POSIX this is the absolute path. On Windows file:///C:/...
        # we strip the leading / so we get C:/...
        if os.name == "nt" and len(raw) > 2 and raw[2] == ":":
            return Path(raw[1:])
        return Path(raw)
    # Form 2: file://C:/path  or  file://C:\path  (common Windows)
    return Path(raw)

logger = logging.getLogger(__name__)


class LocalSource(PackageSource):
    source_type = "local"

    def __init__(self, *, link_mode: str = "copy") -> None:
        """Args:
            link_mode: "copy" (default) or "symlink".
        """
        if link_mode not in ("copy", "symlink"):
            raise ValueError(f"link_mode must be copy or symlink, got {link_mode!r}")
        self._link_mode = link_mode

    async def list_available(self) -> list[AvailablePackage]:
        """Local sources don't enumerate. The user installs by path."""
        return []

    async def fetch(self, source_uri: str, dest: Path) -> Path:
        """Copy or symlink the local package into ``dest``.

        ``source_uri`` accepts every reasonable form a user might
        type:

        - ``file:///abs/path/to/package``     (proper RFC-8089)
        - ``file://C:/path/to/package``       (common Windows)
        - ``file://C:\\path\\to\\package``    (also common Windows)
        - ``/abs/path/to/package``            (raw POSIX path)
        - ``C:\\abs\\path\\to\\package``      (raw Windows path)
        """
        source_path = _parse_local_uri(source_uri)

        if not source_path.is_dir():
            raise FetchError(
                f"LocalSource: source path {source_path} is not a directory"
            )

        toml_path = source_path / "package.toml"
        if not toml_path.is_file():
            raise FetchError(
                f"LocalSource: {source_path} has no package.toml - "
                f"is this really an AppPackage?"
            )

        # Validate the manifest before we do any IO on the install dest
        try:
            PackageManifest.from_path(toml_path)
        except Exception as exc:
            raise FetchError(
                f"LocalSource: invalid package.toml at {source_path}: {exc}"
            ) from exc

        # Wipe existing dest
        if dest.exists() or dest.is_symlink():
            if dest.is_symlink():
                dest.unlink()
            else:
                shutil.rmtree(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)

        if self._link_mode == "symlink":
            try:
                os.symlink(source_path, dest, target_is_directory=True)
            except OSError as exc:
                # Windows symlinks need elevated permissions or developer
                # mode. Fall back to copy + warn loudly.
                logger.warning(
                    "LocalSource: symlink failed (%s) - falling back to copy",
                    exc,
                )
                shutil.copytree(source_path, dest)
        else:
            shutil.copytree(source_path, dest)

        return dest
