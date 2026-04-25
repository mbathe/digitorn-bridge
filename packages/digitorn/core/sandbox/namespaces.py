"""Linux namespace isolation — unprivileged per-session containers.

Creates user/PID/network/mount namespaces via unshare(2).
All operations are unprivileged (CLONE_NEWUSER enables the rest).

This module is designed to be called from inside the worker subprocess,
AFTER bootstrap but BEFORE Landlock/seccomp.

Namespace stacking order (critical — user ns must be first):
    1. CLONE_NEWUSER   — enables unprivileged ns creation
    2. CLONE_NEWPID    — hide host processes
    3. CLONE_NEWNET    — loopback only (IPC is stdin/stdout)
    4. CLONE_NEWNS     — private mount table (optional)
"""

from __future__ import annotations

import ctypes
import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

# ── clone flags ──────────────────────────────────────────────────

CLONE_NEWUSER = 0x10000000
CLONE_NEWPID = 0x20000000
CLONE_NEWNET = 0x40000000
CLONE_NEWNS = 0x00020000

_NS_FLAGS = {
    "user": CLONE_NEWUSER,
    "pid": CLONE_NEWPID,
    "net": CLONE_NEWNET,
    "mount": CLONE_NEWNS,
}

from ._libc import get_libc as _get_libc


# ── Public API ───────────────────────────────────────────────────


def unshare_namespaces(namespaces: set[str]) -> list[str]:
    """Create unprivileged Linux namespaces via unshare(2).

    Args:
        namespaces: Set of namespace names to create.
            Valid: "user", "pid", "net", "mount"

    Returns:
        List of successfully created namespace names.

    The user namespace is ALWAYS created first (even if not
    explicitly requested) when any other namespace is requested,
    because unprivileged namespace creation requires it.
    """
    if not namespaces:
        return []

    requested = namespaces & _NS_FLAGS.keys()
    if not requested:
        return []

    # User namespace must be first — it enables the rest without root
    needs_user = bool(requested - {"user"})
    if needs_user and "user" not in requested:
        requested = requested | {"user"}

    active: list[str] = []

    # Save real UID/GID BEFORE unshare — after CLONE_NEWUSER they become 65534
    real_uid = os.getuid()
    real_gid = os.getgid()

    # Phase 1: unshare user namespace (enables the rest)
    if "user" in requested:
        if _unshare(CLONE_NEWUSER):
            active.append("user")
            _write_uid_gid_mappings(real_uid, real_gid)
        else:
            # Without user ns, other namespaces need root — abort
            logger.warning("namespaces: CLONE_NEWUSER failed, skipping all")
            return []

    # Phase 2: unshare remaining namespaces
    # Order: PID → NET → NS (mount last, it may depend on others)
    for ns_name in ("pid", "net", "mount"):
        if ns_name not in requested:
            continue
        flag = _NS_FLAGS[ns_name]
        if _unshare(flag):
            active.append(ns_name)

    logger.info("namespaces_applied active=%s", active)
    return active


def setup_loopback() -> bool:
    """Bring up the loopback interface in a new network namespace.

    After CLONE_NEWNET the lo interface exists but is DOWN.
    We need it UP for localhost IPC (e.g. database connections).
    """
    try:
        import subprocess
        result = subprocess.run(
            ["ip", "link", "set", "lo", "up"],
            capture_output=True, timeout=5,
        )
        if result.returncode == 0:
            logger.debug("namespaces: loopback UP")
            return True
        logger.debug("namespaces: loopback failed: %s", result.stderr.decode())
        return False
    except Exception as exc:
        logger.debug("namespaces: loopback failed: %s", exc)
        return False


def setup_minimal_mount(workspace: str, extra_readable: set[str] | None = None) -> bool:
    """Set up a minimal mount namespace with only workspace visible.

    This is optional (Landlock already restricts filesystem), but adds
    defense-in-depth by making paths invisible, not just inaccessible.

    Requires CLONE_NEWNS to have been created first.
    """
    import tempfile

    readable = {
        "/usr", "/lib", "/lib64", "/bin", "/sbin",
        "/etc/ssl", "/etc/resolv.conf", "/etc/hosts",
        "/proc", "/dev/null", "/dev/urandom", "/dev/zero",
        "/tmp",
    }
    if extra_readable:
        readable |= extra_readable

    try:
        # Create a new root
        newroot = tempfile.mkdtemp(prefix="digitorn-root-")

        # Bind-mount workspace and essential paths
        for target in sorted(readable | {workspace}):
            if not Path(target).exists():
                continue
            mount_point = Path(newroot) / target.lstrip("/")
            mount_point.mkdir(parents=True, exist_ok=True)
            _bind_mount(target, str(mount_point))

        # pivot_root
        old_root = Path(newroot) / ".old_root"
        old_root.mkdir(exist_ok=True)
        _pivot_root(newroot, str(old_root))

        # Unmount old root
        _umount(str(Path("/.old_root")), detach=True)

        logger.info("minimal_mount workspace=%s", workspace)
        return True
    except Exception as exc:
        logger.warning("minimal_mount_failed: %s", exc)
        return False


# ── Internal helpers ─────────────────────────────────────────────


def _unshare(flags: int) -> bool:
    """Call unshare(2) with the given flags."""
    ret = _get_libc().unshare(ctypes.c_int(flags))
    if ret != 0:
        errno = ctypes.get_errno()
        logger.debug("unshare(0x%x) failed errno=%d (%s)", flags, errno, os.strerror(errno))
        return False
    return True


def _write_uid_gid_mappings(uid: int, gid: int) -> None:
    """Write UID/GID mappings for the new user namespace.

    Maps the host UID/GID to 0 inside the namespace.
    uid/gid MUST be captured BEFORE unshare(CLONE_NEWUSER),
    because after unshare they become 65534 (nobody).
    """

    try:
        # Must write setgroups=deny before gid_map (kernel requirement)
        Path("/proc/self/setgroups").write_text("deny")
    except OSError:
        pass  # May already be denied or not writable

    try:
        Path("/proc/self/uid_map").write_text(f"0 {uid} 1\n")
    except OSError as exc:
        logger.debug("uid_map write failed: %s", exc)

    try:
        Path("/proc/self/gid_map").write_text(f"0 {gid} 1\n")
    except OSError as exc:
        logger.debug("gid_map write failed: %s", exc)


def _bind_mount(source: str, target: str) -> bool:
    """Bind-mount source onto target via mount(2)."""
    MS_BIND = 4096
    MS_REC = 16384
    ret = _get_libc().mount(
        source.encode(), target.encode(),
        None, ctypes.c_ulong(MS_BIND | MS_REC), None,
    )
    if ret != 0:
        logger.debug("bind_mount %s -> %s failed: %s", source, target, os.strerror(ctypes.get_errno()))
        return False
    return True


def _pivot_root(new_root: str, put_old: str) -> bool:
    """pivot_root(2) — change the root filesystem."""
    ret = _get_libc().syscall(
        ctypes.c_long(155),  # SYS_pivot_root (x86_64)
        new_root.encode(),
        put_old.encode(),
    )
    if ret != 0:
        logger.debug("pivot_root failed: %s", os.strerror(ctypes.get_errno()))
        return False
    return True


def _umount(target: str, *, detach: bool = False) -> bool:
    """umount2(2) — unmount a filesystem."""
    MNT_DETACH = 2
    flags = MNT_DETACH if detach else 0
    ret = _get_libc().umount2(target.encode(), ctypes.c_int(flags))
    if ret != 0:
        logger.debug("umount %s failed: %s", target, os.strerror(ctypes.get_errno()))
        return False
    return True
