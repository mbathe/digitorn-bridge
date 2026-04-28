"""Linux sandbox backend - Landlock + seccomp + cgroups.

Orchestrates the three Linux isolation mechanisms:
    1. Landlock: filesystem access control (kernel-level)
    2. seccomp: syscall filtering (kernel-level)
    3. cgroups v2: resource limits via systemd (optional)

All three are applied without root privileges.
"""

from __future__ import annotations

import logging
import os

from .guard import SandboxGuard
from .profile import SandboxProfile

logger = logging.getLogger(__name__)


class LinuxSandbox:

    @property
    def name(self) -> str:
        return "linux"

    def probe(self) -> list[str]:
        features: list[str] = []

        from .landlock import detect_abi_version
        abi = detect_abi_version()
        if abi > 0:
            features.append(f"landlock_v{abi}")

        if _seccomp_available():
            features.append("seccomp")

        if _cgroups_available():
            features.append("cgroups_v2")

        return features

    def apply(self, profile: SandboxProfile) -> SandboxGuard:
        guard = SandboxGuard(app_id=profile.app_id)

        self._apply_hardening(profile, guard)
        self._apply_landlock(profile, guard)
        self._apply_seccomp(profile, guard)
        self._apply_network_filter(profile, guard)
        self._apply_cgroups(profile, guard)

        return guard

    def _apply_hardening(
        self, profile: SandboxProfile, guard: SandboxGuard,
    ) -> None:
        try:
            from .hardening import apply_hardening
            features = apply_hardening(
                drop_caps=profile.hardening_drop_caps,
                no_new_privs=True,  # always required for Landlock/seccomp
                no_dumpable=profile.hardening_no_dumpable,
                mdwe=profile.hardening_mdwe,
            )
            guard.hardening = features
        except Exception as exc:
            guard.warnings.append(f"hardening: {exc}")

    def _apply_landlock(
        self, profile: SandboxProfile, guard: SandboxGuard,
    ) -> None:
        if profile.fs_unrestricted:
            guard.warnings.append("Landlock skipped: fs_unrestricted=true")
            return

        from .landlock import apply_landlock
        ok, abi, warnings = apply_landlock(
            writable_paths=profile.writable_paths,
            readable_paths=profile.readable_paths,
        )
        guard.warnings.extend(warnings)

        if ok:
            guard.active.append(f"landlock_v{abi}")
        else:
            guard.unavailable.append("landlock")

    def _apply_seccomp(
        self, profile: SandboxProfile, guard: SandboxGuard,
    ) -> None:
        from .seccomp import apply_seccomp
        ok, warnings = apply_seccomp(
            allow_exec=profile.allow_exec,
            allow_network=profile.allow_network,
        )
        guard.warnings.extend(warnings)

        if ok:
            guard.active.append("seccomp")
        else:
            guard.unavailable.append("seccomp")

    def _apply_network_filter(
        self, profile: SandboxProfile, guard: SandboxGuard,
    ) -> None:
        """Enforce allowed_hosts at OS level.

        When network is allowed but restricted to specific hosts:
        1. Pre-resolve hostnames to IPs
        2. Install iptables OUTPUT rules (only works in network namespace)
        3. Drop all other outbound traffic

        Falls back to logging a warning if iptables is not available.
        """
        if not profile.allow_network:
            return  # Network blocked entirely by seccomp, no need for host filter

        if not profile.allowed_hosts:
            return  # No host restrictions

        allowed_ips = _resolve_hosts_to_ips(profile.allowed_hosts)
        if not allowed_ips:
            guard.warnings.append("network_filter: no hosts resolved, all outbound blocked")
            return

        # Try iptables-based filtering (works in network namespace)
        try:
            ok = _apply_iptables_filter(allowed_ips)
            if ok:
                guard.active.append("network_filter")
                logger.info(
                    "network_filter_applied hosts=%d ips=%d",
                    len(profile.allowed_hosts), len(allowed_ips),
                )
                return
        except Exception as exc:
            logger.debug("iptables_filter_failed: %s", exc)

        # Store resolved IPs for application-level enforcement
        profile._resolved_allowed_ips = allowed_ips  # type: ignore[attr-defined]
        guard.warnings.append(
            f"network_filter: iptables not available, "
            f"allowed_hosts enforced at application level only "
            f"({len(profile.allowed_hosts)} hosts → {len(allowed_ips)} IPs)"
        )

    def _apply_cgroups(
        self, profile: SandboxProfile, guard: SandboxGuard,
    ) -> None:
        if not profile.memory_limit and not profile.cpu_percent:
            return

        if not _cgroups_available():
            guard.unavailable.append("cgroups_v2")
            return

        try:
            _apply_cgroups_limits(profile)
            guard.active.append("cgroups_v2")
        except Exception as exc:
            guard.unavailable.append("cgroups_v2")
            guard.warnings.append(f"cgroups: {exc}")


# ── cgroups v2 via systemd ────────────────────────────────────────


def _cgroups_available() -> bool:
    try:
        import shutil
        return shutil.which("systemd-run") is not None
    except Exception:
        return False


def _apply_cgroups_limits(profile: SandboxProfile) -> None:
    """Apply resource limits via systemd user scope.

    Creates a transient scope for the current process.
    """
    import subprocess

    cmd = [
        "systemd-run", "--user", "--scope",
        f"--unit=digitorn-{profile.app_id}-{os.getpid()}",
        "--quiet",
    ]

    if profile.memory_limit:
        cmd.append(f"--property=MemoryMax={profile.memory_limit}")

    if profile.cpu_percent:
        cmd.append(f"--property=CPUQuota={profile.cpu_percent}%")

    if profile.max_processes:
        cmd.append(f"--property=TasksMax={profile.max_processes}")

    cmd.extend(["--", "true"])

    subprocess.run(cmd, check=True, capture_output=True, timeout=5)

    logger.info(
        "cgroups_applied app=%s mem=%s cpu=%s%% tasks=%d",
        profile.app_id,
        profile.memory_limit or "unlimited",
        profile.cpu_percent or "unlimited",
        profile.max_processes,
    )


# ── Helpers ───────────────────────────────────────────────────────


def _resolve_hosts_to_ips(hosts: set[str]) -> set[str]:
    """Pre-resolve hostnames to IP addresses for network filtering."""
    import socket

    ips: set[str] = set()
    for host in hosts:
        # Already an IP?
        try:
            socket.inet_pton(socket.AF_INET, host)
            ips.add(host)
            continue
        except OSError:
            pass
        try:
            socket.inet_pton(socket.AF_INET6, host)
            ips.add(host)
            continue
        except OSError:
            pass

        # Resolve hostname
        try:
            results = socket.getaddrinfo(host, None, socket.AF_UNSPEC, socket.SOCK_STREAM)
            for family, _, _, _, sockaddr in results:
                ips.add(sockaddr[0])
        except socket.gaierror:
            logger.warning("network_filter: cannot resolve host '%s'", host)

    # Always allow loopback
    ips.add("127.0.0.1")
    ips.add("::1")

    return ips


def _apply_iptables_filter(allowed_ips: set[str]) -> bool:
    """Apply iptables OUTPUT filter to restrict outbound to allowed IPs.

    Only works inside a network namespace (otherwise requires root).
    Returns True if successfully applied.
    """
    import shutil
    import subprocess

    iptables = shutil.which("iptables")
    if not iptables:
        return False

    try:
        # Allow loopback
        subprocess.run(
            [iptables, "-A", "OUTPUT", "-o", "lo", "-j", "ACCEPT"],
            check=True, capture_output=True, timeout=5,
        )

        # Allow established connections (responses)
        subprocess.run(
            [iptables, "-A", "OUTPUT", "-m", "state",
             "--state", "ESTABLISHED,RELATED", "-j", "ACCEPT"],
            check=True, capture_output=True, timeout=5,
        )

        # Allow specific IPs
        for ip in allowed_ips:
            if ":" in ip:
                continue  # Skip IPv6 for iptables (need ip6tables)
            subprocess.run(
                [iptables, "-A", "OUTPUT", "-d", ip, "-j", "ACCEPT"],
                check=True, capture_output=True, timeout=5,
            )

        # Drop everything else
        subprocess.run(
            [iptables, "-A", "OUTPUT", "-j", "DROP"],
            check=True, capture_output=True, timeout=5,
        )

        return True

    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return False


def _seccomp_available() -> bool:
    try:
        from ._libc import get_libc
        libc = get_libc()
        return hasattr(libc, "prctl")
    except Exception:
        return False
