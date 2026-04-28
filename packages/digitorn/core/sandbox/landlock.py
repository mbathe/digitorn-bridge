"""Landlock LSM - kernel-level filesystem restriction (Linux 5.13+).

Landlock is a Linux Security Module that restricts filesystem access
at the kernel level without root privileges.  Once applied, even a
complete Python exploit cannot access paths outside the ruleset.

ABI versions:
    v1 (5.13): basic FS access control
    v2 (5.19): + REFER (cross-directory rename/link)
    v3 (6.2):  + TRUNCATE
    v4 (6.7):  + TCP bind/connect
    v5 (6.10): + IOCTL on device files

This module detects the ABI at runtime and degrades gracefully.
"""

from __future__ import annotations

import ctypes
import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

# ── Landlock constants ────────────────────────────────────────────

# Syscall numbers (x86_64).  Other archs use different numbers but
# we call via libc wrappers where possible.
SYS_landlock_create_ruleset = 444
SYS_landlock_add_rule = 445
SYS_landlock_restrict_self = 446

LANDLOCK_CREATE_RULESET_VERSION = 1 << 0

# Rule types
LANDLOCK_RULE_PATH_BENEATH = 1
LANDLOCK_RULE_NET_PORT = 2  # ABI v4+

# FS access rights (bitflags)
LANDLOCK_ACCESS_FS_EXECUTE = 1 << 0
LANDLOCK_ACCESS_FS_WRITE_FILE = 1 << 1
LANDLOCK_ACCESS_FS_READ_FILE = 1 << 2
LANDLOCK_ACCESS_FS_READ_DIR = 1 << 3
LANDLOCK_ACCESS_FS_REMOVE_DIR = 1 << 4
LANDLOCK_ACCESS_FS_REMOVE_FILE = 1 << 5
LANDLOCK_ACCESS_FS_MAKE_CHAR = 1 << 6
LANDLOCK_ACCESS_FS_MAKE_DIR = 1 << 7
LANDLOCK_ACCESS_FS_MAKE_REG = 1 << 8
LANDLOCK_ACCESS_FS_MAKE_SOCK = 1 << 9
LANDLOCK_ACCESS_FS_MAKE_FIFO = 1 << 10
LANDLOCK_ACCESS_FS_MAKE_BLOCK = 1 << 11
LANDLOCK_ACCESS_FS_MAKE_SYM = 1 << 12
LANDLOCK_ACCESS_FS_REFER = 1 << 13        # ABI v2+
LANDLOCK_ACCESS_FS_TRUNCATE = 1 << 14     # ABI v3+
LANDLOCK_ACCESS_FS_IOCTL_DEV = 1 << 15    # ABI v5+

# Aggregate masks per ABI version
_READ_ACCESS = (
    LANDLOCK_ACCESS_FS_READ_FILE
    | LANDLOCK_ACCESS_FS_READ_DIR
    | LANDLOCK_ACCESS_FS_EXECUTE
)

_WRITE_ACCESS = (
    LANDLOCK_ACCESS_FS_WRITE_FILE
    | LANDLOCK_ACCESS_FS_REMOVE_DIR
    | LANDLOCK_ACCESS_FS_REMOVE_FILE
    | LANDLOCK_ACCESS_FS_MAKE_DIR
    | LANDLOCK_ACCESS_FS_MAKE_REG
    | LANDLOCK_ACCESS_FS_MAKE_SYM
)


# ── ctypes structures ─────────────────────────────────────────────


class _RulesetAttr(ctypes.Structure):
    _fields_ = [
        ("handled_access_fs", ctypes.c_uint64),
        ("handled_access_net", ctypes.c_uint64),
    ]


class _PathBeneathAttr(ctypes.Structure):
    _fields_ = [
        ("allowed_access", ctypes.c_uint64),
        ("parent_fd", ctypes.c_int32),
    ]


# ── Syscall wrappers ──────────────────────────────────────────────

def _syscall(nr: int, *args: int) -> int:
    from ._libc import get_libc
    ret = get_libc().syscall(ctypes.c_long(nr), *[ctypes.c_long(a) for a in args])
    if ret < 0:
        err = ctypes.get_errno()
        raise OSError(err, os.strerror(err))
    return ret


# ── Public API ────────────────────────────────────────────────────


def detect_abi_version() -> int:
    """Return the Landlock ABI version (0 if unavailable)."""
    try:
        attr = _RulesetAttr(handled_access_fs=0, handled_access_net=0)
        version = _syscall(
            SYS_landlock_create_ruleset,
            0,  # NULL attr
            0,  # size 0
            LANDLOCK_CREATE_RULESET_VERSION,
        )
        return version
    except OSError:
        return 0


def _best_handled_access(abi: int) -> int:
    """Return the full set of FS access rights for the detected ABI."""
    handled = _READ_ACCESS | _WRITE_ACCESS
    if abi >= 2:
        handled |= LANDLOCK_ACCESS_FS_REFER
    if abi >= 3:
        handled |= LANDLOCK_ACCESS_FS_TRUNCATE
    if abi >= 5:
        handled |= LANDLOCK_ACCESS_FS_IOCTL_DEV
    return handled


def apply_landlock(
    writable_paths: set[str],
    readable_paths: set[str],
) -> tuple[bool, int, list[str]]:
    """Apply Landlock filesystem restrictions.

    Returns (success, abi_version, warnings).
    Once applied this is IRREVERSIBLE for the calling process.
    """
    abi = detect_abi_version()
    if abi == 0:
        return False, 0, ["Landlock not available (kernel < 5.13 or disabled)"]

    warnings: list[str] = []
    handled = _best_handled_access(abi)

    if abi < 2:
        warnings.append("Landlock ABI v1: cross-directory rename/link not restricted")
    if abi < 3:
        warnings.append("Landlock ABI v2: TRUNCATE not restricted")

    # prctl(PR_SET_NO_NEW_PRIVS) is required before landlock_restrict_self
    from ._libc import get_libc
    get_libc().prctl(38, 1, 0, 0, 0)  # PR_SET_NO_NEW_PRIVS = 38

    # Create ruleset
    attr = _RulesetAttr(handled_access_fs=handled, handled_access_net=0)
    ruleset_fd = _syscall(
        SYS_landlock_create_ruleset,
        ctypes.addressof(attr),
        ctypes.sizeof(attr),
        0,
    )

    try:
        _add_path_rules(ruleset_fd, writable_paths, _READ_ACCESS | _WRITE_ACCESS, handled)
        _add_path_rules(ruleset_fd, readable_paths, _READ_ACCESS, handled)

        # Restrict self - IRREVERSIBLE
        _syscall(SYS_landlock_restrict_self, ruleset_fd, 0)
    finally:
        os.close(ruleset_fd)

    logger.info(
        "landlock_applied abi=v%d writable=%d readable=%d",
        abi, len(writable_paths), len(readable_paths),
    )
    return True, abi, warnings


def _add_path_rules(
    ruleset_fd: int,
    paths: set[str],
    access_mask: int,
    handled_access: int,
) -> None:
    """Add Landlock rules for a set of paths."""
    # The access mask on each rule MUST be a subset of handled_access_fs
    access_mask = access_mask & handled_access

    for path_str in paths:
        path = Path(path_str)
        if not path.exists():
            logger.debug("landlock_skip_missing path=%s", path_str)
            continue

        try:
            fd = os.open(str(path), os.O_PATH | os.O_CLOEXEC)
        except OSError as exc:
            logger.debug("landlock_skip_open path=%s error=%s", path_str, exc)
            continue

        try:
            rule = _PathBeneathAttr(allowed_access=access_mask, parent_fd=fd)
            try:
                _syscall(
                    SYS_landlock_add_rule,
                    ruleset_fd,
                    LANDLOCK_RULE_PATH_BENEATH,
                    ctypes.addressof(rule),
                    0,
                )
            except OSError as exc:
                logger.debug("landlock_rule_skip path=%s error=%s", path_str, exc)
        finally:
            os.close(fd)
