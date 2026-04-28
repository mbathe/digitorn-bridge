"""Sandbox backend protocol - interface for OS-specific implementations."""

from __future__ import annotations

from typing import Protocol

from .guard import SandboxGuard
from .profile import SandboxProfile


class SandboxBackend(Protocol):
    """Platform-specific sandbox enforcement."""

    @property
    def name(self) -> str: ...

    def probe(self) -> list[str]:
        """Return available features on this platform (e.g. ['landlock_v3', 'seccomp'])."""
        ...

    def apply(self, profile: SandboxProfile) -> SandboxGuard:
        """Apply OS-level isolation.  Irreversible for the calling process."""
        ...
