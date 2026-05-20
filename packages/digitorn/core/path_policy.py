"""Centralized path policy for agent-facing tools."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


# Daemon-private files that must NEVER be reachable from any agent tool,
# even when the policy is opened wide via unrestricted: true.
def _daemon_secret_denylist() -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Return (exact_files, prefix_dirs), both lowercased forward-slash."""
    home = os.path.expanduser("~").replace("\\", "/").lower()
    exact = (
        f"{home}/.digitorn/jwt.key",
        f"{home}/.digitorn/master.key",
        f"{home}/.digitorn/server.key",
        f"{home}/.digitorn/digitorn.db",
        f"{home}/.digitorn/config.yaml",
        f"{home}/.claude/.credentials.json",
    )
    prefixes = (
        f"{home}/.digitorn/kv/",
        f"{home}/.digitorn/sessions/",
        f"{home}/.digitorn/state/",
        f"{home}/.digitorn/logs/",
    )
    return exact, prefixes


def _is_daemon_secret(abs_path: Path) -> bool:
    norm = str(abs_path).replace("\\", "/").lower()
    exact, prefixes = _daemon_secret_denylist()
    if norm in exact:
        return True
    return any(norm.startswith(p) for p in prefixes)


@dataclass
class PathPolicy:
    """Per-session sandbox for every agent-facing path input."""

    workdir: Path
    allowed_extra: tuple[Path, ...] = field(default_factory=tuple)
    unrestricted: bool = False

    def __post_init__(self) -> None:
        # Resolve once; strict=False accepts paths just mkdir'd but unflushed
        object.__setattr__(self, "_workdir_resolved", self.workdir.resolve(strict=False))
        object.__setattr__(
            self,
            "_extras_resolved",
            tuple(p.resolve(strict=False) for p in self.allowed_extra),
        )

    def resolve(self, raw: str | os.PathLike) -> Path:
        """Normalize a path input; relative paths rebase against workdir."""
        s = os.fspath(raw)
        if not s:
            return self._workdir_resolved  # type: ignore[attr-defined]
        if not os.path.isabs(s):
            s = os.path.join(str(self._workdir_resolved), s)  # type: ignore[attr-defined]
        return Path(s).resolve(strict=False)

    def is_allowed(self, abs_path: Path) -> bool:
        """True if abs_path is reachable from the agent (daemon secrets always rejected)."""
        resolved = abs_path.resolve(strict=False)
        if _is_daemon_secret(resolved):
            return False
        if self.unrestricted:
            return True
        try:
            resolved.relative_to(self._workdir_resolved)  # type: ignore[attr-defined]
            return True
        except ValueError:
            pass
        for extra in self._extras_resolved:  # type: ignore[attr-defined]
            try:
                resolved.relative_to(extra)
                return True
            except ValueError:
                continue
        return False

    def enforce(self, raw: str | os.PathLike) -> Path:
        """Resolve + confine. Raises PermissionDeniedError on out-of-sandbox."""
        abs_path = self.resolve(raw)
        if not self.is_allowed(abs_path):
            # late import dodges cycle with modules.exceptions
            from digitorn.modules.exceptions import PermissionDeniedError
            raise PermissionDeniedError(
                action="enforce",
                module="path_policy",
                profile="workdir-sandbox",
            )
        return abs_path

    @classmethod
    def from_constraints(
        cls,
        workdir: str | os.PathLike,
        constraints: dict | None = None,
    ) -> "PathPolicy":
        """Build a policy from a workdir + a module's constraints dict."""
        c = constraints or {}
        extras_raw = c.get("allowed_paths") or []
        extras = tuple(
            Path(os.path.expanduser(str(p))) for p in extras_raw if p
        )
        return cls(
            workdir=Path(workdir),
            allowed_extra=extras,
            unrestricted=bool(c.get("unrestricted", False)),
        )
