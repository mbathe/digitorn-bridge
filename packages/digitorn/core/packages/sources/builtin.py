"""BuiltinSource - packages shipped with the daemon wheel."""

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
        """"""
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

            # Off-load: hashing every file across all builtins would
            # stall the bootstrap path for seconds.
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
        """Copy a builtin from the wheel into `dest`."""
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

        # Off-load: `rmtree` + `copytree` over `node_modules` /
        # `dist` stalls the loop for tens of seconds.
        import asyncio as _asyncio
        def _do_copy() -> None:
            if dest.exists():
                shutil.rmtree(dest)
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(source_dir, dest)
        await _asyncio.to_thread(_do_copy)

        # The package.toml lives at the root of the destination
        if not (dest / "package.toml").is_file():
            raise FetchError(
                f"BuiltinSource: copied {source_dir} → {dest} but "
                f"package.toml is missing"
            )
        return dest

    async def check_update(self, installed_uri: str, current_hash: str) -> str | None:
        """Return the new version string if the wheel-shipped package."""
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
