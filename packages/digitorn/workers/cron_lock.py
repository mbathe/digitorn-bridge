"""Cross-platform file-based leader election. No Postgres dependency."""
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
    """Raised when `acquire()` can't get the lock and the holder."""

@dataclass
class LeaderState:
    pid: int
    host: str
    started_at: float
    renewed_at: float

class FileLeader:
    """File-based exclusive leader lock."""

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
        """Try to take the lock. Raises `LeaderAcquireError`."""
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
                    # Atomic create lost the race; caller retries.
                    raise LeaderAcquireError(
                        "race with another challenger; will retry",
                    )
                with suppress(FileNotFoundError, PermissionError, OSError):
                    self._path.unlink()
                continue
        # Unreachable.
        raise LeaderAcquireError("unreachable acquire path")

    def refresh(self) -> None:
        """Bump `renewed_at` -- call from a background task every."""
        if not self._owned:
            return
        try:
            self._write_state(self._current_state(starting=False))
        except OSError as exc:
            logger.warning("file_leader_refresh_failed: %s", exc)

    def release(self) -> None:
        """Release the lock cleanly. The next `acquire()` from any."""
        if not self._owned:
            return
        self._owned = False
        with suppress(FileNotFoundError, PermissionError, OSError):
            self._path.unlink()

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
        payload = json.dumps({
            "pid": state.pid,
            "host": state.host,
            "started_at": state.started_at,
            "renewed_at": state.renewed_at,
        }).encode("utf-8")
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
