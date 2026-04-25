"""Per-session audit trail — immutable security log.

Records security-relevant events for each sandboxed session:
    - Worker lifecycle transitions (warm → sandboxed → tainted)
    - Namespace creation / failures
    - Landlock / seccomp application
    - Intercepted syscalls (from seccomp-notify)
    - File access patterns
    - Session start / end

Stored as append-only JSONL files in the session state directory.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class AuditEntry:
    """A single audit log entry."""
    timestamp: float
    event: str
    app_id: str
    session_id: str
    data: dict[str, Any] = field(default_factory=dict)
    severity: str = "info"  # info | warning | alert


class SessionAuditLog:
    """Append-only audit log for a single sandboxed session."""

    def __init__(
        self,
        app_id: str,
        session_id: str,
        audit_dir: str | None = None,
    ) -> None:
        self._app_id = app_id
        self._session_id = session_id

        if audit_dir:
            self._dir = Path(audit_dir)
        else:
            home = Path.home() / ".digitorn" / "audit" / app_id
            self._dir = home

        self._dir.mkdir(parents=True, exist_ok=True)
        self._path = self._dir / f"{session_id}.jsonl"
        self._fd: int | None = None

    def open(self) -> None:
        """Open the audit log file (append-only, sync writes)."""
        self._fd = os.open(
            str(self._path),
            os.O_WRONLY | os.O_CREAT | os.O_APPEND,
            0o600,  # owner-only
        )

    def record(
        self,
        event: str,
        data: dict[str, Any] | None = None,
        severity: str = "info",
    ) -> None:
        """Append an entry to the audit log."""
        entry = AuditEntry(
            timestamp=time.time(),
            event=event,
            app_id=self._app_id,
            session_id=self._session_id,
            data=data or {},
            severity=severity,
        )

        line = json.dumps(asdict(entry), ensure_ascii=False) + "\n"

        if self._fd is not None:
            os.write(self._fd, line.encode("utf-8"))
            os.fsync(self._fd)
        else:
            # Fallback: append via pathlib
            with self._path.open("a") as f:
                f.write(line)

    def record_sandbox_applied(self, guard_data: dict[str, Any]) -> None:
        self.record("sandbox_applied", guard_data)

    def record_namespace_created(self, namespaces: list[str]) -> None:
        self.record("namespace_created", {"namespaces": namespaces})

    def record_hardening_applied(self, features: list[str]) -> None:
        self.record("hardening_applied", {"features": features})

    def record_syscall(self, syscall_name: str, pid: int, args: tuple[int, ...]) -> None:
        self.record("syscall_intercepted", {
            "syscall": syscall_name,
            "pid": pid,
            "args": list(args[:3]),
        }, severity="warning")

    def record_session_start(self, workspace: str) -> None:
        self.record("session_start", {"workspace": workspace})

    def record_session_end(self, reason: str = "normal") -> None:
        self.record("session_end", {"reason": reason})

    def close(self) -> None:
        """Close the audit log file."""
        if self._fd is not None:
            try:
                os.close(self._fd)
            except OSError:
                pass
            self._fd = None

    def __del__(self) -> None:
        self.close()


def read_audit_log(path: str | Path) -> list[AuditEntry]:
    """Read all entries from an audit log file."""
    entries: list[AuditEntry] = []
    path = Path(path)
    if not path.exists():
        return entries

    with path.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
                entries.append(AuditEntry(**obj))
            except (json.JSONDecodeError, TypeError):
                continue

    return entries
