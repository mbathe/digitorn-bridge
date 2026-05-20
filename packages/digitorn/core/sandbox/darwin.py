"""macOS sandbox backend: Seatbelt profiles + resource limits."""

from __future__ import annotations

import logging
import os
import resource
import shutil
import tempfile
from pathlib import Path

from .guard import SandboxGuard
from .profile import SandboxProfile

logger = logging.getLogger(__name__)


class DarwinSandbox:

    @property
    def name(self) -> str:
        return "darwin"

    def probe(self) -> list[str]:
        features: list[str] = []
        if shutil.which("sandbox-exec"):
            features.append("seatbelt")
        features.append("setrlimit")
        return features

    def apply(self, profile: SandboxProfile) -> SandboxGuard:
        guard = SandboxGuard(app_id=profile.app_id)

        self._apply_rlimits(profile, guard)
        self._write_seatbelt_profile(profile, guard)

        return guard

    def _apply_rlimits(
        self, profile: SandboxProfile, guard: SandboxGuard,
    ) -> None:
        applied = []
        try:
            if profile.memory_limit:
                resource.setrlimit(
                    resource.RLIMIT_AS,
                    (profile.memory_limit, profile.memory_limit),
                )
                applied.append("memory")
            if profile.max_processes:
                resource.setrlimit(
                    resource.RLIMIT_NPROC,
                    (profile.max_processes, profile.max_processes),
                )
                applied.append("nproc")
            resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
            applied.append("no_coredump")

            guard.active.append("setrlimit")
            guard.hardening = applied
        except (ValueError, OSError) as exc:
            guard.unavailable.append("setrlimit")
            guard.warnings.append(f"setrlimit: {exc}")

    def _write_seatbelt_profile(
        self, profile: SandboxProfile, guard: SandboxGuard,
    ) -> None:
        if not shutil.which("sandbox-exec"):
            guard.unavailable.append("seatbelt")
            guard.warnings.append("sandbox-exec not found")
            return

        sb = generate_seatbelt_profile(profile)
        sb_path = Path(tempfile.gettempdir()) / f"digitorn-{profile.app_id}.sb"
        sb_path.write_text(sb)

        os.environ["DIGITORN_SEATBELT_PROFILE"] = str(sb_path)
        guard.active.append("seatbelt")
        logger.info("seatbelt_profile_written path=%s", sb_path)


def generate_seatbelt_profile(profile: SandboxProfile) -> str:
    """Generate a Seatbelt .sb profile from a SandboxProfile."""
    rules: list[str] = [
        "(version 1)",
        "(deny default)",
        "",
        '(allow file-read* (subpath "/usr"))',
        '(allow file-read* (subpath "/System"))',
        '(allow file-read* (subpath "/Library/Frameworks"))',
        '(allow file-read* (subpath "/private/etc"))',
        '(allow file-read* (subpath "/dev/null"))',
        '(allow file-read* (subpath "/dev/urandom"))',
        '(allow file-read* (subpath "/dev/random"))',
        '(allow file-read* (subpath "/var/folders"))',
        "",
        '(allow file-read* (subpath "/opt/homebrew"))',
        '(allow file-read* (subpath "/usr/local"))',
        '(allow file-read* (subpath "/Library/Developer"))',
    ]

    all_readable = sorted(profile.readable_paths | profile.writable_paths)
    if all_readable:
        rules.append("")
        for path in all_readable:
            rules.append(f'(allow file-read* (subpath "{path}"))')

    if profile.writable_paths:
        rules.append("")
        for path in sorted(profile.writable_paths):
            rules.append(f'(allow file-write* (subpath "{path}"))')

    rules.append("")
    rules.append('(allow file-read* (subpath "/private/tmp"))')
    rules.append('(allow file-write* (subpath "/private/tmp"))')
    rules.append('(allow file-read* (subpath "/tmp"))')
    rules.append('(allow file-write* (subpath "/tmp"))')

    rules.append("")
    if profile.allow_exec:
        rules.append("(allow process-fork)")
        rules.append("(allow process-exec)")

    rules.append("")
    if profile.allow_network:
        rules.append("(allow network*)")
    else:
        rules.append("(allow network-outbound (remote unix-socket))")
        rules.append("(allow network-inbound (local unix-socket))")

    rules.append("")
    rules.append("(allow mach-lookup)")
    rules.append("(allow ipc-posix-shm-read*)")
    rules.append("(allow ipc-posix-shm-write-create)")
    rules.append("(allow sysctl-read)")
    rules.append("(allow signal (target self))")

    rules.append("")
    rules.append("(deny file-write* (subpath \"/System\") (with send-signal SIGKILL))")
    rules.append("(deny file-write* (subpath \"/usr\") (with send-signal SIGKILL))")

    return "\n".join(rules) + "\n"


def exec_with_seatbelt(profile_path: str, argv: list[str]) -> None:
    """Re-exec the current process under sandbox-exec."""
    os.execvp("sandbox-exec", ["sandbox-exec", "-f", profile_path, *argv])
