"""Sandbox guard — result of applying OS-level isolation.

Returned by ``apply_sandbox()`` so callers know what protections
are active and which ones could not be applied.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class SandboxGuard:
    """Outcome of sandbox enforcement."""

    app_id: str
    active: list[str] = field(default_factory=list)
    unavailable: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    # Per-session sandbox extensions
    namespaces: list[str] = field(default_factory=list)
    hardening: list[str] = field(default_factory=list)
    notify_fd: int | None = None  # seccomp-notify FD for real-time audit

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
