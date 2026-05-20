"""Sandbox backend protocol."""

from __future__ import annotations

from typing import Protocol

from .guard import SandboxGuard
from .profile import SandboxProfile


class SandboxBackend(Protocol):
    """Platform-specific sandbox enforcement."""

    @property
    def name(self) -> str: ...

    def probe(self) -> list[str]: ...

    def apply(self, profile: SandboxProfile) -> SandboxGuard: ...
