"""HubSource — STUB ONLY in v1.

The hub server (``hub.digitorn.io``) is a separate project that
will let users publish, browse, and download community packages.
The daemon-side wiring is in place but every method here raises
``NotImplementedError`` so:

- The interface contract is locked (the day the hub ships, this
  file is the only one that needs to be filled in)
- The HTTP install route returns 501 with a clear "available in v2"
  message instead of silently doing nothing
- Tests can verify the stub behaviour to catch any premature use

When implemented, the methods will:

1. ``list_available()`` → ``GET hub.digitorn.io/api/packages``
2. ``fetch()`` → stream a ``.dtpkg`` bundle, verify signature, untar
3. ``check_update()`` → ``GET /api/packages/{id}/latest``
"""

from __future__ import annotations

from pathlib import Path

from digitorn.core.packages.source import (
    AvailablePackage,
    PackageSource,
)


_NOT_IMPLEMENTED_MESSAGE = (
    "Hub source is not available in this daemon (v1). "
    "The hub server (hub.digitorn.io) is a separate project — "
    "see docs/APP_PACKAGES.md §14 'Digitorn Hub — future protocol'. "
    "Until then, install packages via source_type='local' from a directory."
)


class HubSource(PackageSource):
    source_type = "hub"

    async def list_available(self) -> list[AvailablePackage]:
        raise NotImplementedError(_NOT_IMPLEMENTED_MESSAGE)

    async def fetch(self, source_uri: str, dest: Path) -> Path:
        raise NotImplementedError(_NOT_IMPLEMENTED_MESSAGE)

    async def check_update(self, installed_uri: str, current_hash: str) -> str | None:
        raise NotImplementedError(_NOT_IMPLEMENTED_MESSAGE)
