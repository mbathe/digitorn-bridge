"""Credential audit log.

Append-only ledger of every operation performed on a credential.
Chained by SHA-256 of the previous row so any tampering breaks the
chain and is detectable.

Public surface:

    AuditLog                    interface
    SqlAuditLog                 DB-backed implementation (HistoryLog table extension)
    AuditAction                 enum of recordable actions
    AuditOutcome                enum of outcomes
    LogScrubber                 redacts secret patterns from log lines
"""

from __future__ import annotations

from digitorn.core.credentials.audit.log import (
    AuditAction,
    AuditLog,
    AuditOutcome,
    AuditRecord,
    InMemoryAuditLog,
)
from digitorn.core.credentials.audit.scrubber import (
    LogScrubber,
    install_global_scrubber,
)
from digitorn.core.credentials.audit.store import SqlAuditLog

__all__ = [
    "AuditAction",
    "AuditLog",
    "AuditOutcome",
    "AuditRecord",
    "InMemoryAuditLog",
    "SqlAuditLog",
    "LogScrubber",
    "install_global_scrubber",
]
