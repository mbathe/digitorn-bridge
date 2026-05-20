"""Sandbox guard: outcome of applying OS-level isolation."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class SandboxGuard:
    """Outcome of sandbox enforcement."""

    app_id: str
    active: list[str] = field(default_factory=list)
    unavailable: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    namespaces: list[str] = field(default_factory=list)
    hardening: list[str] = field(default_factory=list)
    notify_fd: int | None = None

    @property
    def is_enforced(self) -> bool:
        return len(self.active) > 0

    @property
    def is_partial(self) -> bool:
        return len(self.unavailable) > 0

    def to_dict(self) -> dict[str, list[str]]:
        return {
            "active": self.active,
            "unavailable": self.unavailable,
            "warnings": self.warnings,
            "namespaces": self.namespaces,
            "hardening": self.hardening,
        }
