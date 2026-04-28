"""PackageSource - abstract base class for all install sources.

Each concrete subclass knows how to **fetch** a package from
somewhere (filesystem, network, git) and **list** what it knows
about. The install flow orchestrator doesn't care which source
type it's dealing with - it just calls ``source.fetch(uri, dest)``
and the source does the right thing.

V1 implementations:

- :class:`BuiltinSource` - scans ``packages/digitorn/builtins/``
- :class:`LocalSource`   - copies/symlinks from a local directory
- :class:`HubSource`     - STUB (raises NotImplementedError)
- :class:`GitSource`     - STUB (raises NotImplementedError)

Adding a new source = subclass + register in the install flow's
source map. No core changes needed.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class AvailablePackage:
    """A package this source knows about but hasn't necessarily installed.

    Returned by ``PackageSource.list_available()``. Used by the
    daemon's bootstrap loop to discover built-ins and by the (future)
    hub UI to list community packages.
    """

    package_id: str
    version: str
    source_type: str
    source_uri: str
    package_dir: Path  # where the source has the files locally (cached or original)
    manifest: dict[str, Any] = field(default_factory=dict)
    hash: str = ""  # filled lazily by the install flow if not provided


class FetchError(Exception):
    """Raised when a source can't fetch a package (network, missing dir, etc.)."""


class PackageSource(ABC):
    """Abstract source - one per ``source_type``."""

    source_type: str = "abstract"

    @abstractmethod
    async def list_available(self) -> list[AvailablePackage]:
        """Return packages this source knows about.

        For ``builtin`` this scans the wheel-shipped directory.
        For ``local`` this is a no-op (you install local packages
        explicitly by path, you don't enumerate them).
        For ``hub`` and ``git`` this would query the remote.
        """

    @abstractmethod
    async def fetch(self, source_uri: str, dest: Path) -> Path:
        """Materialise a package at ``dest`` and return the package directory.

        - ``builtin``: copy from the wheel into ``dest``
        - ``local``: copy or symlink from the user's path
        - ``hub`` / ``git``: download / clone

        The returned path is the directory containing ``package.toml``
        (which may be ``dest`` itself or a subdirectory).
        """

    async def check_update(self, installed_uri: str, current_hash: str) -> str | None:
        """Return the latest version available, or None if no update.

        Default implementation returns None (no update detection).
        Concrete sources override.
        """
        return None
