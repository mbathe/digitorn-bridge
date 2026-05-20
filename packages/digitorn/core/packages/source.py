"""PackageSource - abstract base class for all install sources."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

@dataclass
class AvailablePackage:
    """A package this source knows about but hasn't necessarily installed."""

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
    """Abstract source - one per `source_type`."""

    source_type: str = "abstract"

    @abstractmethod
    async def list_available(self) -> list[AvailablePackage]:
        """Return packages this source knows about."""

    @abstractmethod
    async def fetch(self, source_uri: str, dest: Path) -> Path:
        """Materialise a package at `dest` and return the package directory."""

    async def check_update(self, installed_uri: str, current_hash: str) -> str | None:
        """Return the latest version available, or None if no update."""
        return None
