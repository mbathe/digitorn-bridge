"""Centralized path policy for agent-facing tools.

Every module that resolves a path supplied by the agent (filesystem,
shell, workspace, mcp, future modules) goes through ``PathPolicy``
instead of rolling its own check. The contract is single-source-of-
truth:

  1. The agent ONLY sees its workdir. Any absolute path outside the
     workdir is rejected with ``PermissionDeniedError``.
  2. Relative paths are rebased against the workdir.
  3. Symlinks are resolved BEFORE the check — a symlink inside the
     workdir pointing outside is rejected.
  4. Apps may grant extra roots via YAML
     ``constraints.allowed_paths: [...]`` (e.g. ``~/.cache``, ``/tmp``
     when an app legitimately needs them).
  5. ``constraints.unrestricted: true`` is the global escape hatch
     for migration / debug. Even in unrestricted mode the daemon-
     secret denylist still bites (signing keys, master key, DB,
     credentials store) — those are NEVER reachable from any tool.

A single ``PathPolicy`` lives on ``AgentContext.path_policy`` for the
lifetime of a session. Modules pull it via the context (or the legacy
``self._context`` accessor) instead of re-implementing the logic.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


# Daemon-private files that must NEVER be reachable from any agent
# tool, even when the policy is opened wide via ``unrestricted: true``.
# Hash-pinned signing keys / master encryption keys / DB / credentials
# store - any leak here lets the caller forge tokens, decrypt every
# user's secrets, or impersonate the daemon. Mirror of the legacy
# ``_assert_not_daemon_secret`` in filesystem; centralized here so
# every module inherits the same denylist.
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
        # Workdirs of OTHER users — cross-user leakage prevention.
        # A user must never reach into another user's workdir even
        # when they share the same daemon process. We don't have the
        # user_id at policy construction here (set by caller) so the
        # full prefix is locked by the workdir scope itself; this
        # extra block is for raw absolute path attempts.
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
    """Per-session sandbox for every agent-facing path input.

    ``workdir`` is the canonical agent root. ``allowed_extra`` is an
    additive list of roots the YAML granted (e.g. ``~/.cache``).
    ``unrestricted`` disables the workdir confinement but the daemon-
    secret denylist still applies.
    """

    workdir: Path
    allowed_extra: tuple[Path, ...] = field(default_factory=tuple)
    unrestricted: bool = False

    # ── Cached resolved roots (computed once) ────────────────────────

    def __post_init__(self) -> None:
        # Resolve symlinks once so every check compares against the
        # canonical form. ``strict=False`` accepts non-existent paths
        # (the workdir may have just been mkdir'd but not flushed).
        object.__setattr__(self, "_workdir_resolved", self.workdir.resolve(strict=False))
        object.__setattr__(
            self,
            "_extras_resolved",
            tuple(p.resolve(strict=False) for p in self.allowed_extra),
        )

    # ── Public API ───────────────────────────────────────────────────

    def resolve(self, raw: str | os.PathLike) -> Path:
        """Normalize a path input. Relative paths are rebased against
        the workdir. ``~`` is NOT expanded (the agent's ``~`` doesn't
        belong to its workdir by default; if the YAML allows it, the
        absolute path will be permitted by ``is_allowed``).
        """
        s = os.fspath(raw)
        if not s:
            return self._workdir_resolved  # type: ignore[attr-defined]
        if not os.path.isabs(s):
            s = os.path.join(str(self._workdir_resolved), s)  # type: ignore[attr-defined]
        return Path(s).resolve(strict=False)

    def is_allowed(self, abs_path: Path) -> bool:
        """True if ``abs_path`` (post-resolve) is reachable from the
        agent. Daemon secrets are ALWAYS rejected, even when
        ``unrestricted=True``.
        """
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
        """Resolve + confine. Raises ``PermissionDeniedError`` when
        the resulting path is outside the sandbox.
        """
        abs_path = self.resolve(raw)
        if not self.is_allowed(abs_path):
            # Late import to dodge cycle with modules.exceptions which
            # pulls in module manifests etc.
            from digitorn.modules.exceptions import PermissionDeniedError
            raise PermissionDeniedError(
                action="enforce",
                module="path_policy",
                profile="workdir-sandbox",
            )
        return abs_path

    # ── Construction helpers ─────────────────────────────────────────

    @classmethod
    def from_constraints(
        cls,
        workdir: str | os.PathLike,
        constraints: dict | None = None,
    ) -> "PathPolicy":
        """Build a policy from a workdir + a module's ``constraints``
        dict. Recognized keys:

          - ``unrestricted: bool`` — full bypass (denylist still bites)
          - ``allowed_paths: list[str]`` — extra roots (``~`` expanded)
        """
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
