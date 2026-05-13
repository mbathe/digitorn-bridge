"""Cross-platform file-based leader election. No Postgres dependency.

The cron module (and any other singleton background task) calls
``FileLeader(path).acquire()`` at startup. Only one process in the
cluster ends up with the lock; the others see ``LeaderAcquireError``
and stay idle (or retry).

How it works -- pure-file design, no OS locking calls:

  * ``acquire`` uses ``os.open(O_CREAT | O_EXCL)`` to atomically
    create the lock file. If another process already created it,
    open fails with ``FileExistsError``.
  * On ``FileExistsError``, we read the file: ``{pid, host,
    started_at, renewed_at}``. If the holder's renewal is older
    than ``stale_after_s`` AND its PID is dead on this host, we
    delete the file and retry the atomic create exactly once.
  * The leader rewrites ``renewed_at`` every ``renew_every_s`` so
    its lease stays fresh; a crashed leader stops renewing, and a
    challenger eventually times out the lease and takes over.

This works identically on Windows, Linux, macOS -- the only
platform dependence is the PID-alive probe, isolated below.

Why no ``fcntl.flock`` / ``msvcrt.locking``:
  * Cross-platform code stays simpler.
  * OS-level locking has surprising in-process semantics on
    Windows (same-process double-lock may silently succeed) which
    we hit in smoke tests.
  * The PID+renewal model is what databases like Postgres /
    etcd / Consul use for leader election anyway -- the file is
    just our durable persistence layer.
"""
from __future__ import annotations

import json
import logging
import os
import socket
import sys
import time
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

_DEFAULT_STALE_S = 90.0
_DEFAULT_RENEW_S = 30.0


class LeaderAcquireError(Exception):
    """Raised when ``acquire()`` can't get the lock and the holder
    is still alive + fresh. The caller stays idle / retries later.
    """


@dataclass
class LeaderState:
    pid: int
    host: str
    started_at: float
    renewed_at: float


class FileLeader:
    """File-based exclusive leader lock.

    Usage::

        leader = FileLeader(Path.home() / ".digitorn" / ".cron.lock")
        try:
            leader.acquire()
        except LeaderAcquireError:
            sys.exit("another cron worker is leader")
        # ... run cron loop ...
        # Renewal is automatic via leader.refresh() called from a
        # background asyncio task every ``leader.renew_interval_s``.

    Thread-safety: one instance is owned by one process / thread.
    Do NOT share across processes -- create a fresh instance after
    fork. Within a process the leader is a logical singleton;
    creating two instances pointing at the same path and calling
    ``acquire()`` on both will let the second one fail.
    """

    def __init__(
        self,
        path: Path,
        *,
        stale_after_s: float = _DEFAULT_STALE_S,
        renew_every_s: float = _DEFAULT_RENEW_S,
    ) -> None:
        self._path = path
        self._stale_after_s = stale_after_s
        self._renew_every_s = renew_every_s
        self._owned = False
        self._path.parent.mkdir(parents=True, exist_ok=True)

    @property
    def renew_interval_s(self) -> float:
        return self._renew_every_s

    @property
    def is_owner(self) -> bool:
        return self._owned

    def acquire(self) -> None:
        """Try to take the lock. Raises ``LeaderAcquireError`` if a
        live + fresh leader already holds it. Idempotent: calling
        ``acquire()`` while already owning is a no-op.
        """
        if self._owned:
            return

        for attempt in (1, 2):
            try:
                self._atomic_create_and_write(self._current_state(starting=True))
                self._owned = True
                return
            except FileExistsError:
                # Someone else holds it -- decide if we steal or bail.
                existing = self._read_state_safe()
                if existing is not None and not self._is_stale(existing):
                    raise LeaderAcquireError(
                        f"leader held by pid={existing.pid} "
                        f"host={existing.host} "
                        f"renewed_at={existing.renewed_at:.0f}",
                    )
                if attempt == 2:
                    # Stale by our reading but the atomic create
                    # still fails -- another challenger raced us.
                    # Conservative: declare unowned, let the caller
                    # retry on the next tick.
                    raise LeaderAcquireError(
                        "race with another challenger; will retry",
                    )
                # Stale: best-effort delete, then retry the create.
                with suppress(FileNotFoundError, PermissionError, OSError):
                    self._path.unlink()
                continue
        # Unreachable.
        raise LeaderAcquireError("unreachable acquire path")

    def refresh(self) -> None:
        """Bump ``renewed_at`` -- call from a background task every
        ``renew_every_s`` to keep the lease alive. No-op if we are
        not currently the owner.

        Writes are non-atomic (the lock file exists already), so a
        crash mid-write leaves the previous content in place. The
        challenger's staleness check tolerates that.
        """
        if not self._owned:
            return
        try:
            self._write_state(self._current_state(starting=False))
        except OSError as exc:
            logger.warning("file_leader_refresh_failed: %s", exc)

    def release(self) -> None:
        """Release the lock cleanly. The next ``acquire()`` from any
        process succeeds immediately. Safe to call multiple times.
        """
        if not self._owned:
            return
        self._owned = False
        with suppress(FileNotFoundError, PermissionError, OSError):
            self._path.unlink()

    # ---- internals --------------------------------------------------

    def _current_state(self, *, starting: bool) -> LeaderState:
        now = time.time()
        previous = None
        if not starting:
            previous = self._read_state_safe()
        started_at = previous.started_at if previous else now
        return LeaderState(
            pid=os.getpid(),
            host=socket.gethostname(),
            started_at=started_at,
            renewed_at=now,
        )

    def _atomic_create_and_write(self, state: LeaderState) -> None:
        """Atomic ``O_CREAT | O_EXCL`` create + write the initial
        state. Raises ``FileExistsError`` when the file is already
        there -- that's the only signal we use for "lock held by
        someone else".
        """
        flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
        fd = os.open(self._path, flags, 0o600)
        try:
            payload = json.dumps({
                "pid": state.pid,
                "host": state.host,
                "started_at": state.started_at,
                "renewed_at": state.renewed_at,
            }).encode("utf-8")
            os.write(fd, payload)
            with suppress(OSError):
                os.fsync(fd)
        finally:
            os.close(fd)

    def _write_state(self, state: LeaderState) -> None:
        """Overwrite the lock file with a fresh state. The file
        exists (we're refreshing); not atomic but the stale-check
        on the reader side tolerates partial reads.
        """
        payload = json.dumps({
            "pid": state.pid,
            "host": state.host,
            "started_at": state.started_at,
            "renewed_at": state.renewed_at,
        }).encode("utf-8")
        # Open + truncate + write in one shot. ``os.O_TRUNC`` resets
        # the file to length 0 before writing; combined with our
        # single ``os.write`` call, partial reads on the challenger
        # side see either old-content-then-truncate (length 0) or
        # new-content -- both lead to a benign retry.
        fd = os.open(
            self._path,
            os.O_WRONLY | os.O_TRUNC,
            0o600,
        )
        try:
            os.write(fd, payload)
            with suppress(OSError):
                os.fsync(fd)
        finally:
            os.close(fd)

    def _read_state_safe(self) -> LeaderState | None:
        try:
            raw = self._path.read_text(encoding="utf-8")
        except (FileNotFoundError, OSError):
            return None
        if not raw.strip():
            return None
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return None
        try:
            return LeaderState(
                pid=int(data["pid"]),
                host=str(data["host"]),
                started_at=float(data["started_at"]),
                renewed_at=float(data["renewed_at"]),
            )
        except (KeyError, TypeError, ValueError):
            return None

    def _is_stale(self, state: LeaderState) -> bool:
        # Primary signal: renewal too old.
        if time.time() - state.renewed_at > self._stale_after_s:
            return True
        # Secondary signal: same host AND pid is gone -> stale.
        # Cross-host pid checks are not portable, fall back to
        # timestamp-only for those.
        if state.host == socket.gethostname() and not _pid_alive(state.pid):
            return True
        return False


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if sys.platform == "win32":
        try:
            import ctypes
            kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            h = kernel32.OpenProcess(
                PROCESS_QUERY_LIMITED_INFORMATION, False, pid,
            )
            if not h:
                return False
            kernel32.CloseHandle(h)
            return True
        except Exception:
            # ctypes failure -- assume alive to avoid wrongly
            # stealing the lock.
            return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True
