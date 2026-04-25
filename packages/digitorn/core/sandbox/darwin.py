"""macOS sandbox backend — Seatbelt profiles + resource limits.

Security layers (matching Linux parity):
    1. Seatbelt (sandbox-exec): filesystem, network, process, IPC isolation
    2. setrlimit: memory and process count limits
    3. Resource monitoring via libproc

Seatbelt is the macOS equivalent of Landlock + seccomp combined — it
restricts filesystem paths, network access, process spawning, and
specific syscall families, all in a single declarative profile.

sandbox-exec is deprecated since macOS 10.15 but still functional
through macOS 14+. Apple uses the same Seatbelt engine internally.
"""

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
            # Disable core dumps (equivalent to Linux PR_SET_DUMPABLE=0)
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

        # Store for worker_main to use when re-exec'ing
        os.environ["DIGITORN_SEATBELT_PROFILE"] = str(sb_path)
        guard.active.append("seatbelt")
        logger.info("seatbelt_profile_written path=%s", sb_path)


def generate_seatbelt_profile(profile: SandboxProfile) -> str:
    """Generate a Seatbelt .sb profile from a SandboxProfile.

    Seatbelt is the macOS equivalent of Landlock + seccomp:
    - (deny default): block everything not explicitly allowed
    - file-read*/file-write*: filesystem access (like Landlock)
    - process-fork/process-exec: process control (like seccomp execve)
    - network*: network access (like seccomp socket + net namespace)
    - mach-lookup: IPC control (macOS-specific)
    """
    rules: list[str] = [
        "(version 1)",
        "(deny default)",
        "",
        ";; ─── System reads (always needed) ───",
        '(allow file-read* (subpath "/usr"))',
        '(allow file-read* (subpath "/System"))',
        '(allow file-read* (subpath "/Library/Frameworks"))',
        '(allow file-read* (subpath "/private/etc"))',
        '(allow file-read* (subpath "/dev/null"))',
        '(allow file-read* (subpath "/dev/urandom"))',
        '(allow file-read* (subpath "/dev/random"))',
        '(allow file-read* (subpath "/var/folders"))',  # temp files
        "",
        ";; ─── Python runtime ───",
        '(allow file-read* (subpath "/opt/homebrew"))',
        '(allow file-read* (subpath "/usr/local"))',
        '(allow file-read* (subpath "/Library/Developer"))',
    ]

    # Readable paths
    all_readable = sorted(profile.readable_paths | profile.writable_paths)
    if all_readable:
        rules.append("")
        rules.append(";; ─── App readable paths ───")
        for path in all_readable:
            rules.append(f'(allow file-read* (subpath "{path}"))')

    # Writable paths (like Landlock writable_paths)
    if profile.writable_paths:
        rules.append("")
        rules.append(";; ─── App writable paths ───")
        for path in sorted(profile.writable_paths):
            rules.append(f'(allow file-write* (subpath "{path}"))')

    # Private tmp (writable)
    rules.append("")
    rules.append(";; ─── Temp directory ───")
    rules.append('(allow file-read* (subpath "/private/tmp"))')
    rules.append('(allow file-write* (subpath "/private/tmp"))')
    rules.append('(allow file-read* (subpath "/tmp"))')
    rules.append('(allow file-write* (subpath "/tmp"))')

    # Process execution (like seccomp execve filter)
    rules.append("")
    if profile.allow_exec:
        rules.append(";; ─── Process execution: ALLOWED ───")
        rules.append("(allow process-fork)")
        rules.append("(allow process-exec)")
    else:
        rules.append(";; ─── Process execution: DENIED ───")
        rules.append(";; (fork/exec blocked by default deny)")

    # Network (like seccomp socket filter + net namespace)
    rules.append("")
    if profile.allow_network:
        rules.append(";; ─── Network: ALLOWED ───")
        rules.append("(allow network*)")
    else:
        rules.append(";; ─── Network: DENIED ───")
        # Allow Unix domain sockets only (for local IPC)
        rules.append("(allow network-outbound (remote unix-socket))")
        rules.append("(allow network-inbound (local unix-socket))")

    # Minimal IPC (required for process operation on macOS)
    rules.append("")
    rules.append(";; ─── Minimal IPC (required) ───")
    rules.append("(allow mach-lookup)")
    rules.append("(allow ipc-posix-shm-read*)")
    rules.append("(allow ipc-posix-shm-write-create)")
    rules.append("(allow sysctl-read)")
    rules.append("(allow signal (target self))")

    # Deny dangerous operations explicitly (defense in depth)
    rules.append("")
    rules.append(";; ─── Explicit denials (audit) ───")
    rules.append("(deny file-write* (subpath \"/System\") (with send-signal SIGKILL))")
    rules.append("(deny file-write* (subpath \"/usr\") (with send-signal SIGKILL))")

    return "\n".join(rules) + "\n"


def exec_with_seatbelt(profile_path: str, argv: list[str]) -> None:
    """Re-exec the current process under sandbox-exec."""
    os.execvp("sandbox-exec", ["sandbox-exec", "-f", profile_path, *argv])
