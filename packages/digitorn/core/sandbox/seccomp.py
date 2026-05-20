"""seccomp-bpf syscall filtering (Linux 3.17+)."""

from __future__ import annotations

import ctypes
import logging
import struct

logger = logging.getLogger(__name__)

PR_SET_SECCOMP = 22
PR_SET_NO_NEW_PRIVS = 38
SECCOMP_MODE_FILTER = 2
SECCOMP_RET_ALLOW = 0x7FFF0000
SECCOMP_RET_ERRNO = 0x00050000
SECCOMP_RET_LOG = 0x7FFC0000

BPF_LD = 0x00
BPF_W = 0x00
BPF_ABS = 0x20
BPF_JMP = 0x05
BPF_JEQ = 0x10
BPF_K = 0x00
BPF_RET = 0x06

AUDIT_ARCH_X86_64 = 0xC000003E
AUDIT_ARCH_AARCH64 = 0xC00000B7

_ALWAYS_BLOCKED_X86_64: dict[str, int] = {
    "mount": 165,
    "umount2": 166,
    "pivot_root": 155,
    "reboot": 169,
    "kexec_load": 246,
    "init_module": 175,
    "finit_module": 313,
    "delete_module": 176,
    "ptrace": 101,
    "process_vm_readv": 310,
    "process_vm_writev": 311,
    "keyctl": 250,
    "add_key": 248,
    "request_key": 249,
    "swapon": 167,
    "swapoff": 168,
    "sethostname": 170,
    "setdomainname": 171,
    "iopl": 172,
    "ioperm": 173,
}

_EXEC_SYSCALLS_X86_64: dict[str, int] = {
    "execve": 59,
    "execveat": 322,
}

_NETWORK_SYSCALLS_X86_64: dict[str, int] = {
    "socket": 41,
    "connect": 42,
    "bind": 49,
    "listen": 50,
    "accept": 43,
    "accept4": 288,
}

_ALWAYS_BLOCKED_AARCH64: dict[str, int] = {
    "mount": 40,
    "umount2": 39,
    "pivot_root": 41,
    "reboot": 142,
    "kexec_load": 104,
    "init_module": 105,
    "finit_module": 273,
    "delete_module": 106,
    "ptrace": 117,
    "keyctl": 219,
    "add_key": 217,
    "request_key": 218,
    "swapon": 224,
    "swapoff": 225,
    "sethostname": 161,
    "setdomainname": 162,
}

_EXEC_SYSCALLS_AARCH64: dict[str, int] = {
    "execve": 221,
    "execveat": 281,
}

_NETWORK_SYSCALLS_AARCH64: dict[str, int] = {
    "socket": 198,
    "connect": 203,
    "bind": 200,
    "listen": 201,
    "accept": 202,
    "accept4": 242,
}


def _bpf_stmt(code: int, k: int) -> bytes:
    return struct.pack("HBBI", code, 0, 0, k)


def _bpf_jump(code: int, k: int, jt: int, jf: int) -> bytes:
    return struct.pack("HBBI", code, jt, jf, k)


def _build_filter(blocked_nrs: set[int], arch: str) -> bytes:
    # validates seccomp_data.arch first; blocks i386 compat bypass on x86_64
    audit_arch = AUDIT_ARCH_X86_64 if arch == "x86_64" else AUDIT_ARCH_AARCH64
    instructions: list[bytes] = []

    instructions.append(_bpf_stmt(BPF_LD | BPF_W | BPF_ABS, 4))
    instructions.append(_bpf_jump(BPF_JMP | BPF_JEQ | BPF_K, audit_arch, 1, 0))
    instructions.append(_bpf_stmt(BPF_RET | BPF_K, SECCOMP_RET_ERRNO | 1))

    instructions.append(_bpf_stmt(BPF_LD | BPF_W | BPF_ABS, 0))

    deny_offset = len(blocked_nrs)
    for i, nr in enumerate(sorted(blocked_nrs)):
        jt = deny_offset - i
        instructions.append(
            _bpf_jump(BPF_JMP | BPF_JEQ | BPF_K, nr, jt, 0)
        )

    instructions.append(_bpf_stmt(BPF_RET | BPF_K, SECCOMP_RET_ALLOW))
    instructions.append(_bpf_stmt(BPF_RET | BPF_K, SECCOMP_RET_ERRNO | 1))

    return b"".join(instructions)


class _SockFprog(ctypes.Structure):
    _fields_ = [
        ("len", ctypes.c_ushort),
        ("filter", ctypes.c_void_p),
    ]


def _detect_arch() -> str:
    import platform
    machine = platform.machine()
    if machine in ("x86_64", "AMD64"):
        return "x86_64"
    if machine in ("aarch64", "arm64"):
        return "aarch64"
    return machine


def _get_syscall_tables(arch: str) -> tuple[dict[str, int], dict[str, int], dict[str, int]]:
    if arch == "aarch64":
        return _ALWAYS_BLOCKED_AARCH64, _EXEC_SYSCALLS_AARCH64, _NETWORK_SYSCALLS_AARCH64
    return _ALWAYS_BLOCKED_X86_64, _EXEC_SYSCALLS_X86_64, _NETWORK_SYSCALLS_X86_64


from ._libc import get_libc as _get_libc


def apply_seccomp(
    *,
    allow_exec: bool = False,
    allow_network: bool = False,
) -> tuple[bool, list[str]]:
    """Apply seccomp-bpf syscall filter (irreversible)."""
    arch = _detect_arch()
    if arch not in ("x86_64", "aarch64"):
        return False, [f"seccomp: unsupported architecture '{arch}'"]

    always_blocked, exec_syscalls, net_syscalls = _get_syscall_tables(arch)
    warnings: list[str] = []

    blocked_nrs: set[int] = set(always_blocked.values())

    if not allow_exec:
        blocked_nrs.update(exec_syscalls.values())

    if not allow_network:
        blocked_nrs.update(net_syscalls.values())

    bpf_prog = _build_filter(blocked_nrs, arch)
    n_instructions = len(bpf_prog) // 8

    # NO_NEW_PRIVS required for unprivileged seccomp
    libc = _get_libc()
    ret = libc.prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0)
    if ret != 0:
        return False, ["seccomp: failed to set NO_NEW_PRIVS"]

    buf = ctypes.create_string_buffer(bpf_prog)
    fprog = _SockFprog(
        len=n_instructions,
        filter=ctypes.cast(buf, ctypes.c_void_p).value,
    )

    ret = libc.prctl(
        ctypes.c_int(PR_SET_SECCOMP),
        ctypes.c_ulong(SECCOMP_MODE_FILTER),
        ctypes.byref(fprog),
        ctypes.c_ulong(0),
        ctypes.c_ulong(0),
    )
    if ret != 0:
        err = ctypes.get_errno()
        return False, [f"seccomp: prctl failed (errno {err})"]

    logger.info(
        "seccomp_applied arch=%s blocked=%d allow_exec=%s allow_net=%s",
        arch, len(blocked_nrs), allow_exec, allow_network,
    )
    return True, warnings
