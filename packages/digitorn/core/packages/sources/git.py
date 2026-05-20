"""GitSource - STUB ONLY in v1."""

from __future__ import annotations

from pathlib import Path

from digitorn.core.packages.source import (
    AvailablePackage,
    PackageSource,
)

_NOT_IMPLEMENTED_MESSAGE = (
    "Git source is not available in this daemon (v1). "
    "See docs/APP_PACKAGES.md §16 - git source is on the deferred list. "
    "Until then, clone the repo manually and install via source_type='local'."
)

class GitSource(PackageSource):
    source_type = "git"

    async def list_available(self) -> list[AvailablePackage]:
        raise NotImplementedError(_NOT_IMPLEMENTED_MESSAGE)

    async def fetch(self, source_uri: str, dest: Path) -> Path:
        raise NotImplementedError(_NOT_IMPLEMENTED_MESSAGE)

    async def check_update(self, installed_uri: str, current_hash: str) -> str | None:
        raise NotImplementedError(_NOT_IMPLEMENTED_MESSAGE)
