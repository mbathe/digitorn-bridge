"""No-op sandbox backend - used when no OS isolation is available."""

from __future__ import annotations

import logging

from .guard import SandboxGuard
from .profile import SandboxProfile

logger = logging.getLogger(__name__)


class NoopSandbox:
    """Fallback: logs a warning but does not restrict anything."""

    @property
    def name(self) -> str:
        return "noop"

    def probe(self) -> list[str]:
        return []

    def apply(self, profile: SandboxProfile) -> SandboxGuard:
        logger.warning(
            "sandbox_noop app=%s - no OS-level isolation available. "
            "Software-level enforcement only.",
            profile.app_id,
        )
        return SandboxGuard(
            app_id=profile.app_id,
            unavailable=["all"],
            warnings=["No OS sandbox available on this platform"],
        )
