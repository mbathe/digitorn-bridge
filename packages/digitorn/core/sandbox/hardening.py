"""Process hardening via prctl(2)."""

from __future__ import annotations

import ctypes
import logging

from ._libc import get_libc

logger = logging.getLogger(__name__)

PR_SET_NO_NEW_PRIVS = 38
PR_SET_DUMPABLE = 4
PR_CAPBSET_DROP = 24
PR_SET_MDWE = 65
PR_MDWE_REFUSE_EXEC_GAIN = 1

_MAX_CAPS = 64


def apply_hardening(
    *,
    drop_caps: bool = True,
    no_new_privs: bool = True,
    no_dumpable: bool = True,
    mdwe: bool = True,
) -> list[str]:
    """Apply prctl-based process hardening."""
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


def _set_no_new_privs() -> bool:
    _libc = get_libc()
    ret = _libc.prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0)
    if ret != 0:
        logger.debug("hardening: NO_NEW_PRIVS failed (errno %d)", ctypes.get_errno())
        return False
    return True


def _set_no_dumpable() -> bool:
    _libc = get_libc()
    ret = _libc.prctl(PR_SET_DUMPABLE, 0, 0, 0, 0)
    if ret != 0:
        logger.debug("hardening: DUMPABLE=0 failed (errno %d)", ctypes.get_errno())
        return False
    return True


def _drop_all_caps() -> int:
    _libc = get_libc()
    dropped = 0
    for cap in range(_MAX_CAPS):
        ret = _libc.prctl(PR_CAPBSET_DROP, cap, 0, 0, 0)
        if ret == 0:
            dropped += 1
        elif ctypes.get_errno() == 22:
            break
    return dropped


def _set_mdwe() -> bool:
    _libc = get_libc()
    ret = _libc.prctl(PR_SET_MDWE, PR_MDWE_REFUSE_EXEC_GAIN, 0, 0, 0)
    if ret != 0:
        errno = ctypes.get_errno()
        if errno == 22:
            logger.debug("hardening: MDWE not available (kernel < 6.3)")
        else:
            logger.debug("hardening: MDWE failed (errno %d)", errno)
        return False
    return True
