"""OS-level sandbox: kernel-enforced isolation derived from app YAML."""

from __future__ import annotations

import logging
import sys
from typing import TYPE_CHECKING

from .builder import build_sandbox_profile
from .guard import SandboxGuard
from .profile import SandboxProfile
from .worker import AppSandboxWorker, SandboxWorker, WorkerState

if TYPE_CHECKING:
    from .base import SandboxBackend

logger = logging.getLogger(__name__)

__all__ = [
    "build_sandbox_profile",
    "apply_sandbox",
    "get_backend",
    "SandboxProfile",
    "SandboxGuard",
    "SandboxWorker",
    "WorkerState",
]


def get_backend() -> SandboxBackend:
    """Return the best available sandbox backend for this OS."""
    if sys.platform == "linux":
        from .linux import LinuxSandbox
        return LinuxSandbox()

    if sys.platform == "darwin":
        from .darwin import DarwinSandbox
        return DarwinSandbox()

    if sys.platform == "win32":
        from .windows import WindowsSandbox
        return WindowsSandbox()

    from .noop import NoopSandbox
    return NoopSandbox()


def apply_sandbox(profile: SandboxProfile) -> SandboxGuard:
    """Apply OS-level isolation for the given profile (irreversible)."""
    backend = get_backend()
    features = backend.probe()

    if not features:
        logger.info("sandbox_skip app=%s backend=%s (no features)", profile.app_id, backend.name)

    guard = backend.apply(profile)

    logger.info(
        "sandbox_applied app=%s backend=%s active=%s unavailable=%s",
        profile.app_id,
        backend.name,
        guard.active,
        guard.unavailable,
    )

    for w in guard.warnings:
        logger.warning("sandbox_warning app=%s: %s", profile.app_id, w)

    return guard
