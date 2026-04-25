"""Process hardening via prctl(2) — defense-in-depth layer.

Applied inside the worker subprocess before Landlock/seccomp.
Each feature is independent — if one fails (kernel too old),
the rest still apply.

Features:
    - PR_SET_NO_NEW_PRIVS: prevent privilege escalation via suid
    - PR_SET_DUMPABLE=0: prevent core dumps (credential leaks)
    - PR_CAP_BSET_DROP: drop ALL Linux capabilities
    - PR_SET_MDWE: Memory-Deny-Write-Execute (kernel 6.3+)
      Blocks mmap(PROT_WRITE|PROT_EXEC) — prevents JIT exploitation
"""

from __future__ import annotations

import ctypes
import logging

from ._libc import get_libc

logger = logging.getLogger(__name__)

# ── prctl constants ──────────────────────────────────────────────

PR_SET_NO_NEW_PRIVS = 38
PR_SET_DUMPABLE = 4
PR_CAPBSET_DROP = 24
PR_SET_MDWE = 65            # kernel 6.3+
PR_MDWE_REFUSE_EXEC_GAIN = 1

# Total number of Linux capabilities (CAP_LAST_CAP + 1).
# As of kernel 6.10 there are 41 (0..40).  We try up to 64 to be
# forward-compatible — dropping a non-existent cap returns EINVAL
# which we silently ignore.
_MAX_CAPS = 64


# ── Public API ───────────────────────────────────────────────────


def apply_hardening(
    *,
    drop_caps: bool = True,
    no_new_privs: bool = True,
    no_dumpable: bool = True,
    mdwe: bool = True,
) -> list[str]:
    """Apply prctl-based process hardening.

    Returns list of features that were successfully activated.
    Each feature is independent — failures don't stop the others.
    """
    active: list[str] = []

    if no_new_privs:
        if _set_no_new_privs():
            active.append("no_new_privs")

    if no_dumpable:
        if _set_no_dumpable():
            active.append("no_dumpable")

    if drop_caps:
        n = _drop_all_caps()
        if n > 0:
            active.append(f"caps_dropped({n})")

    if mdwe:
        if _set_mdwe():
            active.append("mdwe")

    logger.info("hardening_applied features=%s", active)
    return active


# ── Individual features ──────────────────────────────────────────


def _set_no_new_privs() -> bool:
    """Prevent privilege escalation via setuid/setgid binaries."""
    _libc = get_libc()
    ret = _libc.prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0)
    if ret != 0:
        logger.debug("hardening: NO_NEW_PRIVS failed (errno %d)", ctypes.get_errno())
        return False
    return True


def _set_no_dumpable() -> bool:
    """Prevent core dumps — blocks /proc/self/mem reads and ptrace attach."""
    _libc = get_libc()
    ret = _libc.prctl(PR_SET_DUMPABLE, 0, 0, 0, 0)
    if ret != 0:
        logger.debug("hardening: DUMPABLE=0 failed (errno %d)", ctypes.get_errno())
        return False
    return True


def _drop_all_caps() -> int:
    """Drop every capability from the bounding set.

    After this, even if the process somehow regains euid 0,
    it has no capabilities.  Returns count of caps dropped.
    """
    _libc = get_libc()
    dropped = 0
    for cap in range(_MAX_CAPS):
        ret = _libc.prctl(PR_CAPBSET_DROP, cap, 0, 0, 0)
        if ret == 0:
            dropped += 1
        # EINVAL = cap doesn't exist on this kernel — stop scanning
        elif ctypes.get_errno() == 22:  # EINVAL
            break
    return dropped


def _set_mdwe() -> bool:
    """Activate Memory-Deny-Write-Execute (kernel 6.3+).

    Prevents creating memory mappings that are both writable and
    executable.  Blocks JIT-spray and most shellcode injection.
    """
    _libc = get_libc()
    ret = _libc.prctl(PR_SET_MDWE, PR_MDWE_REFUSE_EXEC_GAIN, 0, 0, 0)
    if ret != 0:
        errno = ctypes.get_errno()
        if errno == 22:  # EINVAL — kernel doesn't support MDWE
            logger.debug("hardening: MDWE not available (kernel < 6.3)")
        else:
            logger.debug("hardening: MDWE failed (errno %d)", errno)
        return False
    return True
