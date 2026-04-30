"""SQL-backed audit log with hash chain integrity.

Persists `AuditRecord` instances to the `credential_audit` table. Each
row carries `prev_hash` and `this_hash` columns; the chain genesis
uses `prev_hash = "0" * 64`. A periodic verifier job (or the
`/api/admin/credentials/audit/verify` endpoint) re-hashes from genesis
and reports any breakage.

Concurrency: the chain head is fetched with `SELECT ... ORDER BY id
DESC LIMIT 1 FOR UPDATE` (row-lock on the latest entry) before writing
the new row. Postgres serialises concurrent writers cleanly. SQLite
falls back to whole-table locking which is acceptable for the typical
audit volume (a few hundred rows/day per session).

Schema is defined in `digitorn.core.models.CredentialAudit` and
created by Alembic migration. This file ONLY does the read/write
logic; schema lives with the rest of the data model.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import desc, select

from digitorn.core.credentials.audit.log import (
    GENESIS_HASH,
    AuditAction,
    AuditOutcome,
    AuditRecord,
)

logger = logging.getLogger(__name__)


class SqlAuditLog:
    """Implements `AuditLog` via the `credential_audit` SQL table."""

    def __init__(self, session_factory: Any) -> None:
        self._session_factory = session_factory

    async def record(self, rec: AuditRecord) -> None:
        """Insert a new audit row, chained from the previous head.

        On failure, the operation that triggered the audit MUST be
        aborted by the caller. Audit failure = security failure.
        """
        from digitorn.core.models import CredentialAudit  # lazy: model may not be loaded yet

        async with self._session_factory() as db:
            # Lock the chain head while we compute + insert.
            async with db.begin():
                stmt = (
                    select(CredentialAudit)
                    .order_by(desc(CredentialAudit.id))
                    .limit(1)
                    .with_for_update(skip_locked=False)
                )
                head = (await db.execute(stmt)).scalar_one_or_none()
                prev_hash = head.this_hash if head else GENESIS_HASH
                this_hash = rec.chain_hash(prev_hash)

                row = CredentialAudit(
                    who=rec.who,
                    action=rec.action.value,
                    when_ts=rec.when,
                    target=rec.on,
                    outcome=rec.outcome.value,
                    reason=rec.reason,
                    where_ip=rec.where_ip,
                    where_ua=rec.where_ua,
                    app_id=rec.app_id,
                    extra=rec.extra,
                    prev_hash=prev_hash,
                    this_hash=this_hash,
                )
                db.add(row)
            # commit on context exit

    async def list_for_credential(
        self,
        credential_id: str,
        *,
        limit: int = 100,
    ) -> list[AuditRecord]:
        from digitorn.core.models import CredentialAudit

        async with self._session_factory() as db:
            stmt = (
                select(CredentialAudit)
                .where(CredentialAudit.target == credential_id)
                .order_by(desc(CredentialAudit.id))
                .limit(limit)
            )
            rows = (await db.execute(stmt)).scalars().all()
            return [self._row_to_record(r) for r in rows]

    async def list_for_user(
        self,
        user_id: str,
        *,
        limit: int = 100,
    ) -> list[AuditRecord]:
        from digitorn.core.models import CredentialAudit

        async with self._session_factory() as db:
            stmt = (
                select(CredentialAudit)
                .where(CredentialAudit.who == user_id)
                .order_by(desc(CredentialAudit.id))
                .limit(limit)
            )
            rows = (await db.execute(stmt)).scalars().all()
            return [self._row_to_record(r) for r in rows]

    async def verify_chain(self) -> tuple[bool, str | None]:
        """Walk the entire chain from genesis and verify each hash.

        Returns (True, None) on intact chain.
        Returns (False, "row id=N") at the first inconsistency.

        WARNING: this is O(n) on the audit log size. For large
        deployments, schedule it nightly rather than per-request.
        """
        from digitorn.core.models import CredentialAudit

        async with self._session_factory() as db:
            stmt = select(CredentialAudit).order_by(CredentialAudit.id.asc())
            result = await db.stream(stmt)
            prev_hash = GENESIS_HASH
            async for row_obj in result:
                row = row_obj[0] if isinstance(row_obj, tuple) else row_obj
                rec = self._row_to_record(row)
                expected = rec.chain_hash(prev_hash)
                if row.prev_hash != prev_hash:
                    return False, f"row id={row.id} has prev_hash mismatch"
                if row.this_hash != expected:
                    return False, f"row id={row.id} has this_hash mismatch"
                prev_hash = row.this_hash
        return True, None

    @staticmethod
    def _row_to_record(row: Any) -> AuditRecord:
        return AuditRecord(
            who=row.who,
            action=AuditAction(row.action),
            when=row.when_ts,
            on=row.target,
            outcome=AuditOutcome(row.outcome),
            reason=row.reason or "",
            where_ip=row.where_ip or "",
            where_ua=row.where_ua or "",
            app_id=row.app_id,
            extra=dict(row.extra or {}),
        )
