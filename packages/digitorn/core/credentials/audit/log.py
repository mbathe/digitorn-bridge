"""AuditLog interface + in-memory implementation."""

from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Protocol, runtime_checkable

logger = logging.getLogger(__name__)


GENESIS_HASH = "0" * 64


class AuditAction(str, Enum):
    """Operations on credentials that produce an audit row."""

    CREATE = "create"           # new credential created
    READ = "read"               # full fields decrypted (NOT just metadata)
    READ_METADATA = "read_metadata"  # status / mask only, no decrypt
    UPDATE_FIELDS = "update_fields"  # secret fields rotated
    UPDATE_METADATA = "update_metadata"  # label, status, expiry changed
    DELETE = "delete"           # hard-delete
    TEST_CONNECTION = "test_connection"  # handler.test_live_connection
    REFRESH = "refresh"         # OAuth refresh, etc.
    REVOKE = "revoke"           # admin force-revoke
    INJECT = "inject"           # runtime injection into a module
    SCOPE_CHANGE = "scope_change"  # scope mutation (admin)
    PERMISSION_DENIED = "permission_denied"  # rejected access attempt


class AuditOutcome(str, Enum):
    """Result of the audited operation."""

    SUCCESS = "success"
    FAILURE = "failure"
    DENIED = "denied"  # RBAC rejection


@dataclass(frozen=True)
class AuditRecord:
    """One audit row. Immutable by construction."""

    who: str
    action: AuditAction
    when: float
    on: str
    outcome: AuditOutcome
    reason: str = ""
    where_ip: str = ""
    where_ua: str = ""
    app_id: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def to_canonical_json(self) -> str:
        """Stable JSON representation for hashing. Sorted keys, no"""
        d = asdict(self)
        d["action"] = self.action.value
        d["outcome"] = self.outcome.value
        return json.dumps(d, sort_keys=True, separators=(",", ":"))

    def chain_hash(self, prev_hash: str) -> str:
        """Compute this row's hash given the predecessor's hash."""
        h = hashlib.sha256()
        h.update(prev_hash.encode("ascii"))
        h.update(self.to_canonical_json().encode("utf-8"))
        return h.hexdigest()


@runtime_checkable
class AuditLog(Protocol):
    """Backend-agnostic audit log."""

    async def record(self, rec: AuditRecord) -> None:
        """Persist one audit row. Implementations append to the chain"""
        ...

    async def list_for_credential(
        self,
        credential_id: str,
        *,
        limit: int = 100,
    ) -> list[AuditRecord]:
        """Fetch the most recent rows for a credential, newest first."""
        ...

    async def list_for_user(
        self,
        user_id: str,
        *,
        limit: int = 100,
    ) -> list[AuditRecord]:
        """Fetch the most recent rows by actor, newest first."""
        ...

    async def verify_chain(self) -> tuple[bool, str | None]:
        """Re-hash the chain and verify integrity. Returns"""
        ...


class InMemoryAuditLog:
    """Implementation of AuditLog for tests and ephemeral processes."""

    def __init__(self) -> None:
        self._rows: list[tuple[AuditRecord, str]] = []  # (record, this_hash)
        self._prev_hash = GENESIS_HASH

    async def record(self, rec: AuditRecord) -> None:
        h = rec.chain_hash(self._prev_hash)
        self._rows.append((rec, h))
        self._prev_hash = h

    async def list_for_credential(
        self, credential_id: str, *, limit: int = 100,
    ) -> list[AuditRecord]:
        rows = [r for r, _ in self._rows if r.on == credential_id]
        return list(reversed(rows[-limit:]))

    async def list_for_user(
        self, user_id: str, *, limit: int = 100,
    ) -> list[AuditRecord]:
        rows = [r for r, _ in self._rows if r.who == user_id]
        return list(reversed(rows[-limit:]))

    async def verify_chain(self) -> tuple[bool, str | None]:
        prev = GENESIS_HASH
        for idx, (rec, persisted) in enumerate(self._rows):
            expected = rec.chain_hash(prev)
            if expected != persisted:
                return False, f"row index {idx}"
            prev = expected
        return True, None

    def __len__(self) -> int:
        return len(self._rows)


def make_record(
    who: str,
    action: AuditAction,
    on: str,
    *,
    outcome: AuditOutcome = AuditOutcome.SUCCESS,
    reason: str = "",
    where_ip: str = "",
    where_ua: str = "",
    app_id: str | None = None,
    extra: dict[str, Any] | None = None,
) -> AuditRecord:
    """Builder that timestamps the record at call time."""
    return AuditRecord(
        who=who,
        action=action,
        when=time.time(),
        on=on,
        outcome=outcome,
        reason=reason,
        where_ip=where_ip,
        where_ua=where_ua,
        app_id=app_id,
        extra=dict(extra or {}),
    )
