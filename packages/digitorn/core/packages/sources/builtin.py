"""BuiltinSource - packages shipped with the daemon wheel.

Scans ``packages/digitorn/builtins/`` for sub-directories that
contain a ``package.toml``. Each match is an available built-in
package. The bootstrap loop installs them automatically at first
boot and re-installs them when the wheel-shipped hash changes
(typically after ``pip install -U digitorn``).

This source never reaches the network and never modifies the
wheel files - it only **reads**. The ``fetch`` step is a regular
``shutil.copytree`` from the read-only wheel location into the
user's writable install root.

Source URI format: ``bundle://digitorn/<package_id>``.
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path

from digitorn.core.packages.hash import compute_package_hash
from digitorn.core.packages.manifest import PackageManifest
from digitorn.core.packages.source import (
    AvailablePackage,
    FetchError,
    PackageSource,
)

logger = logging.getLogger(__name__)


class BuiltinSource(PackageSource):
    source_type = "builtin"

    def __init__(self, builtins_dir: Path) -> None:
        """Args:
            builtins_dir: usually ``packages/digitorn/builtins/`` - the
                directory inside the wheel where built-in packages live.
        """
        self._builtins_dir = builtins_dir

    async def list_available(self) -> list[AvailablePackage]:
        """Scan the builtins dir for valid packages."""
        if not self._builtins_dir.is_dir():
            logger.debug(
                "BuiltinSource: builtins dir %s does not exist - no builtins",
                self._builtins_dir,
            )
            return []

        result: list[AvailablePackage] = []
        for entry in sorted(self._builtins_dir.iterdir()):
            if not entry.is_dir():
                continue
            toml_path = entry / "package.toml"
            if not toml_path.is_file():
                logger.debug(
                    "BuiltinSource: %s has no package.toml - skipping",
                    entry.name,
                )
                continue
            try:
                manifest = PackageManifest.from_path(toml_path)
            except Exception as exc:
                logger.warning(
                    "BuiltinSource: cannot load %s - %s", toml_path, exc,
                )
                continue

            # Off-loop: hashing every file in every builtin at startup
            # walks 50+ packages × N files each = seconds of stall on
            # the bootstrap path. Off-load so the FastAPI lifespan can
            # finish and start serving HTTP while builtins are still
            # being discovered.
            import asyncio as _asyncio
            try:
                pkg_hash = await _asyncio.to_thread(compute_package_hash, entry)
            except Exception as exc:
                logger.warning(
                    "BuiltinSource: cannot hash %s - %s", entry, exc,
                )
                pkg_hash = ""

            result.append(AvailablePackage(
                package_id=manifest.id,
                version=manifest.version,
                source_type=self.source_type,
                source_uri=f"bundle://digitorn/{manifest.id}",
                package_dir=entry,
                manifest=manifest.to_dict(),
                hash=pkg_hash,
            ))
        return result

    async def fetch(self, source_uri: str, dest: Path) -> Path:
        """Copy a builtin from the wheel into ``dest``.

        ``source_uri`` is ``bundle://digitorn/<package_id>``. We
        translate that into a local subdirectory under
        ``self._builtins_dir`` and copytree it.
        """
        if not source_uri.startswith("bundle://digitorn/"):
            raise FetchError(
                f"BuiltinSource: invalid source_uri {source_uri!r}, "
                f"expected bundle://digitorn/<package_id>"
            )
        package_id = source_uri[len("bundle://digitorn/"):]
        source_dir = self._builtins_dir / package_id
        if not source_dir.is_dir():
            raise FetchError(
                f"BuiltinSource: built-in package {package_id!r} not found "
                f"in {self._builtins_dir}"
            )

        # Wipe any stale dest then copytree
        if dest.exists():
            shutil.rmtree(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(source_dir, dest)

        # The package.toml lives at the root of the destination
        if not (dest / "package.toml").is_file():
            raise FetchError(
                f"BuiltinSource: copied {source_dir} → {dest} but "
                f"package.toml is missing"
            )
        return dest

    async def check_update(self, installed_uri: str, current_hash: str) -> str | None:
        """Return the new version string if the wheel-shipped package
        has been upgraded since the user installed it.

        Compares the hash on disk in the wheel with the hash recorded
        in the registry. If they differ, the wheel was upgraded and
        a re-install is in order.
        """
        try:
            packages = await self.list_available()
        except Exception:
            return None
        if not installed_uri.startswith("bundle://digitorn/"):
            return None
        package_id = installed_uri[len("bundle://digitorn/"):]
        for pkg in packages:
            if pkg.package_id == package_id and pkg.hash != current_hash:
                return pkg.version
        return None
