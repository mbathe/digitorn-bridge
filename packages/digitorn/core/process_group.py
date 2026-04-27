"""Cross-platform process group / job management for the daemon.

Goal: when the daemon process dies for ANY reason (clean shutdown,
Ctrl+C, terminal close, crash, kill -9), every child it ever spawned
(Vite dev servers, npm install, taskkill subprocesses, sandbox workers,
preview managers, MCP servers, ...) dies with it. **No orphans.**

Three platforms, three mechanisms — same public API.

Windows
-------
A **Job Object** with the ``JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`` flag.
The daemon assigns itself to a fresh job at startup. When the daemon
process exits, Windows automatically terminates every other process
in the job. Children spawned via ``subprocess.Popen`` inherit the job
by default (since Windows 8).

Linux
-----
``os.setsid()`` puts the daemon in its own session and process group.
``prctl(PR_SET_PDEATHSIG, SIGKILL)`` on each child arranges for the
kernel to send SIGKILL to the child the moment its parent (the
daemon) dies. As a backstop, on shutdown the daemon also sends
``SIGTERM`` to the entire process group via ``os.killpg``.

macOS
-----
Same as Linux except ``PR_SET_PDEATHSIG`` does not exist. We rely on
``setsid()`` + the shutdown-time ``killpg`` plus a small reaper that
walks ``psutil`` children if available.
"""

from __future__ import annotations

import atexit
import logging
import os
import signal
import sys
from typing import Any

logger = logging.getLogger(__name__)

_initialized = False
_job_handle: Any = None


def install() -> None:
    """Set up the OS-level process group for the running daemon.

    Idempotent. Safe to call multiple times. Must be called BEFORE any
    ``subprocess.Popen`` so the children inherit the group/job.
    """
    global _initialized
    if _initialized:
        return
    _initialized = True

    if sys.platform.startswith("win"):
        _install_windows_job()
    else:
        _install_unix_session()

    _install_signal_handlers()
    atexit.register(_cleanup_at_exit)
    logger.info("process_group: installed (platform=%s)", sys.platform)


def _install_windows_job() -> None:
    """Create a Job Object that kills children when the daemon dies."""
    global _job_handle
    try:
        import ctypes
        from ctypes import wintypes
    except Exception as exc:
        logger.warning("process_group: ctypes unavailable on Windows: %s", exc)
        return

    JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
    JobObjectExtendedLimitInformation = 9

    class IO_COUNTERS(ctypes.Structure):
        _fields_ = [
            ("ReadOperationCount", ctypes.c_ulonglong),
            ("WriteOperationCount", ctypes.c_ulonglong),
            ("OtherOperationCount", ctypes.c_ulonglong),
            ("ReadTransferCount", ctypes.c_ulonglong),
            ("WriteTransferCount", ctypes.c_ulonglong),
            ("OtherTransferCount", ctypes.c_ulonglong),
        ]

    class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", wintypes.LARGE_INTEGER),
            ("PerJobUserTimeLimit", wintypes.LARGE_INTEGER),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
            ("IoInfo", IO_COUNTERS),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p]
    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    kernel32.SetInformationJobObject.argtypes = [
        wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
    ]
    kernel32.SetInformationJobObject.restype = wintypes.BOOL
    kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
    kernel32.GetCurrentProcess.argtypes = []
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL

    job = kernel32.CreateJobObjectW(None, None)
    if not job:
        err = ctypes.get_last_error()
        logger.warning("process_group: CreateJobObjectW failed (err=%d)", err)
        return

    info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
    info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    if not kernel32.SetInformationJobObject(
        job,
        JobObjectExtendedLimitInformation,
        ctypes.byref(info),
        ctypes.sizeof(info),
    ):
        err = ctypes.get_last_error()
        logger.warning("process_group: SetInformationJobObject failed (err=%d)", err)
        kernel32.CloseHandle(job)
        return

    current = kernel32.GetCurrentProcess()
    if not kernel32.AssignProcessToJobObject(job, current):
        err = ctypes.get_last_error()
        logger.warning(
            "process_group: AssignProcessToJobObject failed (err=%d) — "
            "the daemon may already be inside an outer job (e.g. a debugger)",
            err,
        )
        kernel32.CloseHandle(job)
        return

    _job_handle = job
    logger.info(
        "process_group: Windows Job Object created with KILL_ON_JOB_CLOSE — "
        "all child processes will die when the daemon exits"
    )


def _install_unix_session() -> None:
    """Put the daemon in its own session/process group."""
    try:
        if os.getpgrp() != os.getpid():
            os.setpgrp()
    except OSError as exc:
        logger.warning("process_group: setpgrp failed: %s", exc)
        return
    logger.info(
        "process_group: Unix process group %d created — children will be killed on shutdown",
        os.getpgrp(),
    )


def _install_signal_handlers() -> None:
    """Trap shutdown signals and trigger a clean child kill before exit."""
    handled = (signal.SIGTERM, signal.SIGINT)
    if not sys.platform.startswith("win"):
        handled = handled + (signal.SIGHUP,)

    def _handler(signum, _frame):
        # CRITICAL: reset to default handler BEFORE calling _kill_children.
        # _kill_children does ``os.killpg(pgid, SIGTERM)`` which sends the
        # signal to every member of our process group — INCLUDING us. If
        # the handler is still installed when that signal hits, we re-enter
        # _handler → _kill_children → killpg → infinite recursion until
        # ``RecursionError`` exhausts the stack and the process dies with
        # exit 3 ("NOTIMPLEMENTED" in systemd's tongue).
        signal.signal(signum, signal.SIG_DFL)
        logger.info("process_group: signal %s received — killing children", signum)
        _kill_children()
        try:
            os.kill(os.getpid(), signum)
        except Exception:
            os._exit(128 + int(signum))

    for sig in handled:
        try:
            signal.signal(sig, _handler)
        except (ValueError, OSError) as exc:
            logger.debug("process_group: cannot install handler for %s: %s", sig, exc)


def _cleanup_at_exit() -> None:
    """Atexit hook — fires on clean Python shutdown.

    Reset the SIGTERM/SIGINT/SIGHUP handlers to default BEFORE calling
    ``_kill_children``. Same recursion safety as ``_handler`` above:
    ``killpg`` sends signals to ourselves and we don't want to re-enter
    a now-defunct interpreter via the still-installed handlers (this
    used to manifest as 30+ frames of ``_handler → _kill_children``
    spam in the journal followed by ``RecursionError`` — exit 3).
    """
    if not sys.platform.startswith("win"):
        for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            try:
                signal.signal(sig, signal.SIG_DFL)
            except (ValueError, OSError):
                pass
    _kill_children()


def _kill_children() -> None:
    """Best-effort kill of every child process spawned by this daemon.

    On Windows the Job Object handles this automatically when the
    process exits, so we just close the handle. On Unix we send SIGTERM
    to the process group, then a brief grace period, then SIGKILL.
    Falls back to walking ``psutil`` children when available.
    """
    if sys.platform.startswith("win"):
        _close_windows_job()
        return

    try:
        pgid = os.getpgrp()
        os.killpg(pgid, signal.SIGTERM)
        logger.info("process_group: SIGTERM sent to pgid=%d", pgid)
    except (ProcessLookupError, PermissionError, OSError) as exc:
        logger.debug("process_group: killpg SIGTERM failed: %s", exc)

    try:
        import psutil
        me = psutil.Process(os.getpid())
        children = me.children(recursive=True)
        for c in children:
            try:
                c.terminate()
            except psutil.NoSuchProcess:
                pass
        gone, alive = psutil.wait_procs(children, timeout=2)
        for p in alive:
            try:
                p.kill()
            except psutil.NoSuchProcess:
                pass
        if children:
            logger.info(
                "process_group: psutil reaped %d direct/indirect children",
                len(children),
            )
    except Exception:
        pass


def _close_windows_job() -> None:
    global _job_handle
    if _job_handle is None:
        return
    try:
        import ctypes
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CloseHandle(_job_handle)
        logger.info("process_group: Windows job handle closed — children will be terminated")
    except Exception as exc:
        logger.debug("process_group: CloseHandle failed: %s", exc)
    finally:
        _job_handle = None


def set_pdeathsig_on_child(popen_kwargs: dict[str, Any]) -> dict[str, Any]:
    """Add ``preexec_fn`` to a subprocess.Popen kwargs dict so that
    each child gets ``SIGKILL`` from the kernel when its parent dies.

    Linux only — on Windows the Job Object handles this. On macOS
    PR_SET_PDEATHSIG does not exist; the child relies on the
    daemon's shutdown-time killpg.
    """
    if sys.platform != "linux":
        return popen_kwargs

    def _preexec() -> None:
        try:
            import ctypes
            libc = ctypes.CDLL("libc.so.6", use_errno=True)
            PR_SET_PDEATHSIG = 1
            libc.prctl(PR_SET_PDEATHSIG, signal.SIGKILL, 0, 0, 0)
        except Exception:
            pass

    existing = popen_kwargs.get("preexec_fn")
    if existing is None:
        popen_kwargs["preexec_fn"] = _preexec
    else:
        def _chained() -> None:
            existing()
            _preexec()
        popen_kwargs["preexec_fn"] = _chained
    return popen_kwargs
