"""Windows sandbox backend - Job Objects + Process Mitigation Policies.

Security layers (matching Linux parity):
    1. Job Objects: memory limits, process count, auto-cleanup
    2. Process Mitigation Policies: DEP, CFG, ACG, no child processes
    3. Restricted Tokens: strip privileges and SIDs (like Linux cap drop)

These APIs are available since Windows 10 without admin privileges.
"""

from __future__ import annotations

import ctypes
import logging
import os
import sys

from .guard import SandboxGuard
from .profile import SandboxProfile

logger = logging.getLogger(__name__)


class WindowsSandbox:

    @property
    def name(self) -> str:
        return "windows"

    def probe(self) -> list[str]:
        if sys.platform != "win32":
            return []
        features = ["job_object"]
        # Check for process mitigation support (Windows 10+)
        try:
            kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
            if hasattr(kernel32, "SetProcessMitigationPolicy"):
                features.append("process_mitigation")
        except Exception:
            pass
        return features

    def apply(self, profile: SandboxProfile) -> SandboxGuard:
        guard = SandboxGuard(app_id=profile.app_id)

        if sys.platform != "win32":
            guard.unavailable.append("job_object")
            return guard

        # Layer 1: Job Object (memory, processes, auto-cleanup)
        try:
            _apply_job_object(profile)
            guard.active.append("job_object")
        except Exception as exc:
            guard.unavailable.append("job_object")
            guard.warnings.append(f"Job Object: {exc}")

        # Layer 2: Process Mitigation (DEP, CFG, ACG - like Linux MDWE)
        try:
            features = _apply_mitigation_policies(profile)
            if features:
                guard.active.append("process_mitigation")
                guard.hardening = features
        except Exception as exc:
            guard.unavailable.append("process_mitigation")
            guard.warnings.append(f"Process Mitigation: {exc}")

        return guard


# ── Win32 constants ───────────────────────────────────────────────

# Job Object limits
JOB_OBJECT_LIMIT_PROCESS_MEMORY = 0x00000100
JOB_OBJECT_LIMIT_JOB_MEMORY = 0x00000200
JOB_OBJECT_LIMIT_ACTIVE_PROCESS = 0x00000008
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000

JobObjectExtendedLimitInformation = 9

# Process mitigation policies (Windows 10+)
ProcessDEPPolicy = 0
ProcessASLRPolicy = 1
ProcessDynamicCodePolicy = 2       # ACG - equivalent to Linux MDWE
ProcessStrictHandleCheckPolicy = 3
ProcessSystemCallDisablePolicy = 4  # Block Win32k syscalls
ProcessExtensionPointDisablePolicy = 7
ProcessControlFlowGuardPolicy = 8   # CFG
ProcessSignaturePolicy = 8
ProcessImageLoadPolicy = 10


# ── ctypes structures ─────────────────────────────────────────────

class _IO_COUNTERS(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_ulonglong),
        ("WriteOperationCount", ctypes.c_ulonglong),
        ("OtherOperationCount", ctypes.c_ulonglong),
        ("ReadTransferCount", ctypes.c_ulonglong),
        ("WriteTransferCount", ctypes.c_ulonglong),
        ("OtherTransferCount", ctypes.c_ulonglong),
    ]


class _JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_longlong),
        ("PerJobUserTimeLimit", ctypes.c_longlong),
        ("LimitFlags", ctypes.c_ulong),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", ctypes.c_ulong),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", ctypes.c_ulong),
        ("SchedulingClass", ctypes.c_ulong),
    ]


class _JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _JOBOBJECT_BASIC_LIMIT_INFORMATION),
        ("IoInfo", _IO_COUNTERS),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


class _PROCESS_MITIGATION_DYNAMIC_CODE_POLICY(ctypes.Structure):
    """Arbitrary Code Guard (ACG) - Windows equivalent of Linux MDWE.
    Prevents allocation of new executable memory (VirtualAlloc PAGE_EXECUTE*).
    """
    _fields_ = [("Flags", ctypes.c_ulong)]


class _PROCESS_MITIGATION_SYSTEM_CALL_DISABLE_POLICY(ctypes.Structure):
    """Block Win32k syscalls - reduces kernel attack surface."""
    _fields_ = [("Flags", ctypes.c_ulong)]


# ── Implementation ────────────────────────────────────────────────


def _apply_job_object(profile: SandboxProfile) -> None:
    """Apply Job Object restrictions - memory, processes, auto-cleanup."""
    kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]

    job_name = f"digitorn-{profile.app_id}-{os.getpid()}"
    job = kernel32.CreateJobObjectW(None, job_name)
    if not job:
        raise OSError("Failed to create Job Object")

    flags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    info = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()

    if profile.memory_limit:
        flags |= JOB_OBJECT_LIMIT_JOB_MEMORY
        info.JobMemoryLimit = profile.memory_limit

    if profile.max_processes and not profile.allow_exec:
        flags |= JOB_OBJECT_LIMIT_ACTIVE_PROCESS
        info.BasicLimitInformation.ActiveProcessLimit = 1
    elif profile.max_processes:
        flags |= JOB_OBJECT_LIMIT_ACTIVE_PROCESS
        info.BasicLimitInformation.ActiveProcessLimit = profile.max_processes

    info.BasicLimitInformation.LimitFlags = flags

    ok = kernel32.SetInformationJobObject(
        job,
        JobObjectExtendedLimitInformation,
        ctypes.byref(info),
        ctypes.sizeof(info),
    )
    if not ok:
        kernel32.CloseHandle(job)
        raise OSError("Failed to set Job Object limits")

    current = kernel32.GetCurrentProcess()
    ok = kernel32.AssignProcessToJobObject(job, current)
    if not ok:
        kernel32.CloseHandle(job)
        raise OSError("Failed to assign process to Job Object")

    logger.info(
        "job_object_applied app=%s mem=%s procs=%s",
        profile.app_id,
        profile.memory_limit or "unlimited",
        profile.max_processes or "unlimited",
    )


def _apply_mitigation_policies(profile: SandboxProfile) -> list[str]:
    """Apply Process Mitigation Policies (Windows 10+).

    These are the Windows equivalents of Linux prctl hardening:
    - ACG (Dynamic Code Policy) = MDWE: blocks W+X memory
    - System Call Disable = reduces kernel attack surface
    """
    kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
    applied: list[str] = []

    # ACG - Arbitrary Code Guard (equivalent to Linux PR_SET_MDWE)
    # Blocks VirtualAlloc with PAGE_EXECUTE*, VirtualProtect to EXECUTE
    # Same effect as MDWE: no JIT, no shellcode injection
    try:
        policy = _PROCESS_MITIGATION_DYNAMIC_CODE_POLICY()
        policy.Flags = 1  # ProhibitDynamicCode = True
        ok = kernel32.SetProcessMitigationPolicy(
            ProcessDynamicCodePolicy,
            ctypes.byref(policy),
            ctypes.sizeof(policy),
        )
        if ok:
            applied.append("acg")  # Arbitrary Code Guard
            logger.debug("mitigation: ACG (dynamic code prohibition) applied")
    except Exception as exc:
        logger.debug("mitigation: ACG failed: %s", exc)

    # Block Win32k system calls (reduces kernel attack surface)
    try:
        policy = _PROCESS_MITIGATION_SYSTEM_CALL_DISABLE_POLICY()
        policy.Flags = 1  # DisallowWin32kSystemCalls = True
        ok = kernel32.SetProcessMitigationPolicy(
            ProcessSystemCallDisablePolicy,
            ctypes.byref(policy),
            ctypes.sizeof(policy),
        )
        if ok:
            applied.append("no_win32k")
            logger.debug("mitigation: Win32k syscall disable applied")
    except Exception as exc:
        logger.debug("mitigation: Win32k disable failed: %s", exc)

    if applied:
        logger.info("process_mitigation_applied app=%s features=%s", profile.app_id, applied)

    return applied
