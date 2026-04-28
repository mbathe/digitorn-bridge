"""seccomp-notify - real-time syscall audit via SECCOMP_RET_USER_NOTIF.

Unlike standard seccomp (block/allow), seccomp-notify forwards
intercepted syscalls to a supervisor process (the daemon) which can
inspect, log, rate-limit, or deny them in real-time.

This is something Docker CANNOT do - Docker's seccomp only has
static block/allow rules with no runtime inspection.

Architecture:
    Worker installs seccomp filter with SECCOMP_RET_USER_NOTIF for
    audited syscalls → returns notification FD to daemon → daemon
    runs async reader on the FD → logs/audits each intercepted call.

Requires kernel 5.9+ for SECCOMP_IOCTL_NOTIF_RECV.
"""

from __future__ import annotations

import asyncio
import ctypes
import logging
import os
import struct
from dataclasses import dataclass, field
from typing import Any, Callable

logger = logging.getLogger(__name__)

# ── Constants ────────────────────────────────────────────────────

SECCOMP_SET_MODE_FILTER = 1
SECCOMP_FILTER_FLAG_NEW_LISTENER = 1 << 3  # kernel 5.0+

SECCOMP_RET_USER_NOTIF = 0x7FC00000
SECCOMP_RET_ALLOW = 0x7FFF0000

# ioctl commands for seccomp notification FD
SECCOMP_IOCTL_NOTIF_RECV = 0xC0502100
SECCOMP_IOCTL_NOTIF_SEND = 0xC0182101
SECCOMP_IOCTL_NOTIF_ID_VALID = 0x40082102

# BPF opcodes
BPF_LD = 0x00
BPF_W = 0x00
BPF_ABS = 0x20
BPF_JMP = 0x05
BPF_JEQ = 0x10
BPF_K = 0x00
BPF_RET = 0x06

# seccomp_notif structure size (kernel 5.9+)
# struct seccomp_notif { __u64 id; __u32 pid; __u32 flags; seccomp_data data; }
# seccomp_data: { int nr; __u32 arch; __u64 instruction_pointer; __u64 args[6]; }
SIZEOF_SECCOMP_DATA = 4 + 4 + 8 + 6 * 8  # 64 bytes
SIZEOF_SECCOMP_NOTIF = 8 + 4 + 4 + SIZEOF_SECCOMP_DATA  # 80 bytes

# Syscalls worth auditing (x86_64)
_AUDIT_SYSCALLS_X86_64: dict[str, int] = {
    "open": 2,
    "openat": 257,
    "connect": 42,
    "execve": 59,
    "execveat": 322,
    "bind": 49,
    "listen": 50,
    "unlink": 87,
    "unlinkat": 263,
    "rename": 82,
    "renameat2": 316,
    "chmod": 90,
    "fchmodat": 268,
}

_AUDIT_SYSCALLS_AARCH64: dict[str, int] = {
    "openat": 56,
    "connect": 203,
    "execve": 221,
    "execveat": 281,
    "bind": 200,
    "listen": 201,
    "unlinkat": 35,
    "renameat2": 276,
    "fchmodat": 53,
}

from ._libc import get_libc as _get_libc


# ── Data types ───────────────────────────────────────────────────


@dataclass
class SyscallEvent:
    """A single intercepted syscall."""
    notification_id: int
    pid: int
    syscall_nr: int
    syscall_name: str
    args: tuple[int, ...]
    timestamp: float = 0.0


@dataclass
class AuditConfig:
    """Configuration for the seccomp-notify audit layer."""
    # Syscalls to intercept (names). Empty = default set.
    audit_syscalls: set[str] = field(default_factory=set)
    # Callback for each intercepted syscall
    on_event: Callable[[SyscallEvent], None] | None = None
    # Whether to allow (True) or deny (False) audited syscalls after logging
    allow_after_audit: bool = True


# ── BPF filter builder ───────────────────────────────────────────


def _bpf_stmt(code: int, k: int) -> bytes:
    return struct.pack("HBBI", code, 0, 0, k)


def _bpf_jump(code: int, k: int, jt: int, jf: int) -> bytes:
    return struct.pack("HBBI", code, jt, jf, k)


class _SockFprog(ctypes.Structure):
    _fields_ = [
        ("len", ctypes.c_ushort),
        ("filter", ctypes.c_void_p),
    ]


def _build_notify_filter(audit_nrs: set[int]) -> bytes:
    """Build BPF filter: USER_NOTIF for audited syscalls, ALLOW rest."""
    instructions: list[bytes] = []

    # Load syscall number
    instructions.append(_bpf_stmt(BPF_LD | BPF_W | BPF_ABS, 0))

    # For each audited syscall → jump to notify
    notify_offset = len(audit_nrs)
    for i, nr in enumerate(sorted(audit_nrs)):
        remaining = notify_offset - i - 1
        instructions.append(
            _bpf_jump(BPF_JMP | BPF_JEQ | BPF_K, nr, remaining, 0)
        )

    # Default: allow
    instructions.append(_bpf_stmt(BPF_RET | BPF_K, SECCOMP_RET_ALLOW))

    # Notify: forward to supervisor
    instructions.append(_bpf_stmt(BPF_RET | BPF_K, SECCOMP_RET_USER_NOTIF))

    return b"".join(instructions)


# ── Public API ───────────────────────────────────────────────────


def install_notify_filter(
    audit_syscalls: set[str] | None = None,
) -> int | None:
    """Install a seccomp-notify filter and return the notification FD.

    The notification FD should be passed to the daemon for async reading.
    Returns None if seccomp-notify is not available (kernel < 5.9).

    Must be called AFTER PR_SET_NO_NEW_PRIVS.
    """
    import platform
    machine = platform.machine()

    if machine in ("x86_64", "AMD64"):
        syscall_table = _AUDIT_SYSCALLS_X86_64
    elif machine in ("aarch64", "arm64"):
        syscall_table = _AUDIT_SYSCALLS_AARCH64
    else:
        logger.debug("seccomp_notify: unsupported arch %s", machine)
        return None

    if audit_syscalls:
        audit_nrs = {
            syscall_table[name]
            for name in audit_syscalls
            if name in syscall_table
        }
    else:

        audit_nrs = {
            syscall_table[name]
            for name in ("openat", "connect", "execve")
            if name in syscall_table
        }

    if not audit_nrs:
        return None

    bpf_prog = _build_notify_filter(audit_nrs)
    n_instructions = len(bpf_prog) // 8

    buf = ctypes.create_string_buffer(bpf_prog)
    fprog = _SockFprog(
        len=n_instructions,
        filter=ctypes.cast(buf, ctypes.c_void_p).value,
    )

    # seccomp(SECCOMP_SET_MODE_FILTER, SECCOMP_FILTER_FLAG_NEW_LISTENER, &fprog)
    # Returns the notification FD on success, -1 on error
    SYS_seccomp = 317 if machine in ("x86_64", "AMD64") else 277  # aarch64

    notify_fd = _get_libc().syscall(
        ctypes.c_long(SYS_seccomp),
        ctypes.c_ulong(SECCOMP_SET_MODE_FILTER),
        ctypes.c_ulong(SECCOMP_FILTER_FLAG_NEW_LISTENER),
        ctypes.byref(fprog),
    )

    if notify_fd < 0:
        errno = ctypes.get_errno()
        if errno == 22:  # EINVAL - kernel doesn't support NEW_LISTENER
            logger.debug("seccomp_notify: not available (kernel < 5.0)")
        else:
            logger.debug("seccomp_notify: install failed errno=%d", errno)
        return None

    logger.info("seccomp_notify_installed fd=%d audited=%d", notify_fd, len(audit_nrs))
    return notify_fd


async def run_notify_reader(
    notify_fd: int,
    config: AuditConfig,
    *,
    syscall_names: dict[int, str] | None = None,
) -> None:
    """Async reader for seccomp notification FD (runs in daemon process).

    Reads intercepted syscall notifications and:
    1. Logs them to the audit trail
    2. Calls the on_event callback if configured
    3. Responds with allow/deny

    This coroutine runs until the FD is closed (worker exits).
    """
    import time

    loop = asyncio.get_event_loop()
    names = syscall_names or {}

    while True:
        try:
            # Read notification (blocking → run in executor)
            event = await loop.run_in_executor(
                None, _recv_notification, notify_fd, names,
            )
            if event is None:
                break

            event.timestamp = time.time()

            # Log
            logger.info(
                "seccomp_audit pid=%d syscall=%s(%d) args=%s",
                event.pid, event.syscall_name, event.syscall_nr, event.args[:3],
            )

            # Callback
            if config.on_event:
                try:
                    config.on_event(event)
                except Exception as exc:
                    logger.debug("audit_callback_error: %s", exc)

            # Respond: allow or deny
            _send_response(
                notify_fd, event.notification_id,
                allow=config.allow_after_audit,
            )

        except OSError:
            break  # FD closed, worker exited
        except Exception as exc:
            logger.debug("notify_reader_error: %s", exc)
            break

    try:
        os.close(notify_fd)
    except OSError:
        pass


def _recv_notification(
    notify_fd: int, names: dict[int, str],
) -> SyscallEvent | None:
    """Blocking read of one seccomp notification."""
    buf = bytearray(SIZEOF_SECCOMP_NOTIF)

    ret = _get_libc().ioctl(notify_fd, SECCOMP_IOCTL_NOTIF_RECV, ctypes.c_char_p(bytes(buf)))
    if ret != 0:
        return None

    # Parse: id(8) + pid(4) + flags(4) + nr(4) + arch(4) + ip(8) + args(6×8)
    notif_id = struct.unpack_from("<Q", buf, 0)[0]
    pid = struct.unpack_from("<I", buf, 8)[0]
    syscall_nr = struct.unpack_from("<i", buf, 16)[0]
    args = struct.unpack_from("<6Q", buf, 28)

    return SyscallEvent(
        notification_id=notif_id,
        pid=pid,
        syscall_nr=syscall_nr,
        syscall_name=names.get(syscall_nr, f"syscall_{syscall_nr}"),
        args=args,
    )


def _send_response(notify_fd: int, notif_id: int, *, allow: bool) -> None:
    """Send allow/deny response for a notification."""
    # struct seccomp_notif_resp { __u64 id; __s64 val; __s32 error; __u32 flags; }
    if allow:
        resp = struct.pack("<QqiI", notif_id, 0, 0, 0)  # val=0, error=0 → continue
    else:
        resp = struct.pack("<QqiI", notif_id, 0, -1, 0)  # error=-EPERM

    _get_libc().ioctl(notify_fd, SECCOMP_IOCTL_NOTIF_SEND, ctypes.c_char_p(resp))
