"""GitSource — STUB ONLY in v1.

Same treatment as ``HubSource``: full interface, every method
raises ``NotImplementedError``. When implemented, this source
will clone a git repo at a specific ref and install the package
from inside it.

When wired (deferred), the methods will:

1. ``list_available()`` → returns ``[]`` (git URIs are never enumerated)
2. ``fetch()`` → ``git clone --depth 1 --branch <ref>`` into a cache,
   then copy the package subdir into the install dest
3. ``check_update()`` → ``git ls-remote`` to read the latest tag/SHA
"""

from __future__ import annotations

from pathlib import Path

from digitorn.core.packages.source import (
    AvailablePackage,
    PackageSource,
)


_NOT_IMPLEMENTED_MESSAGE = (
    "Git source is not available in this daemon (v1). "
    "See docs/APP_PACKAGES.md §16 — git source is on the deferred list. "
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
