"""Build Draft Store - persists App Builder drafts in DB + on disk.

A *draft* is an in-progress conversation between a user and the App
Builder agent. The user describes what they want, the builder asks
clarifying questions, generates a YAML, compiles it, fixes errors,
and eventually arrives at a deploy-ready app definition.

Each draft is persisted in two places that stay in sync :

- **DB** (``BuildDraft`` row): the chat history, builder state-machine
  bookkeeping, current YAML, status. Used by every API call that
  returns or updates the draft.
- **Disk** (``~/.digitorn/drafts/<user_id>/<draft_id>/app.yaml``): just
  the current YAML bytes. Lets the user ``cat`` / download / scp the
  draft without going through the API. The store rewrites this file
  on every ``update_yaml()`` call so it never goes stale.

Each user is capped at ``MAX_DRAFTS_PER_USER`` drafts (50). When the
limit is hit, ``create()`` raises ``DraftLimitExceeded`` so the API
layer can return a clean 409 instead of growing the table forever.
"""

from __future__ import annotations

import logging
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import delete, func, select, update
from sqlalchemy.orm.attributes import flag_modified

logger = logging.getLogger(__name__)


MAX_DRAFTS_PER_USER = 50
MAX_CHAT_MESSAGES = 500  # cap on chat_history length to bound row size


class DraftLimitExceeded(Exception):
    """Raised when a user tries to create a draft past the per-user cap."""


def _drafts_dir(user_id: str, draft_id: str) -> Path:
    """Return the on-disk directory holding one draft's YAML.

    Layout::

        ~/.digitorn/drafts/<user_id>/<draft_id>/app.yaml

    Both id segments are validated to reject path traversal - the
    daemon stores user-controlled values in them so we can't trust
    them blindly.
    """
    for name in (user_id, draft_id):
        if not name or "/" in name or "\\" in name or ".." in name:
            raise ValueError(f"unsafe id segment: {name!r}")
    return Path.home() / ".digitorn" / "drafts" / user_id / draft_id


class BuildDraftStore:
    """Database + filesystem store for App Builder drafts."""

    def __init__(self, session_factory: Any) -> None:
        self._session_factory = session_factory

    # ── CRUD ─────────────────────────────────────────────────────

    async def create(
        self,
        user_id: str,
        *,
        name: str = "Untitled draft",
        initial_yaml: str = "",
        builder_state: dict | None = None,
    ) -> dict[str, Any]:
        """Create a new draft for ``user_id``.

        Raises ``DraftLimitExceeded`` if the user already has
        ``MAX_DRAFTS_PER_USER`` drafts (any status). The cap intentionally
        counts every status - abandoned drafts still take a slot, the
        user must explicitly delete them to free up space.
        """
        from digitorn.core.models import BuildDraft

        async with self._session_factory() as db:
            # Cap check
            count_result = await db.execute(
                select(func.count(BuildDraft.id)).where(BuildDraft.user_id == user_id)
            )
            count = count_result.scalar() or 0
            if count >= MAX_DRAFTS_PER_USER:
                raise DraftLimitExceeded(
                    f"user '{user_id}' already has {count} drafts "
                    f"(max {MAX_DRAFTS_PER_USER}). Delete one to free a slot."
                )

            draft = BuildDraft(
                user_id=user_id,
                name=name or "Untitled draft",
                status="in_progress",
                current_yaml=initial_yaml or "",
                chat_history=[],
                builder_state=builder_state or {},
            )
            db.add(draft)
            await db.commit()
            await db.refresh(draft)

            # Mirror the YAML to disk + persist the path back into the row.
            if initial_yaml:
                yaml_path = self._write_yaml_to_disk(user_id, draft.id, initial_yaml)
                draft.yaml_path = str(yaml_path)
                await db.commit()
                await db.refresh(draft)

            return self._row_to_dict(draft)

    async def get(self, draft_id: str, user_id: str | None = None) -> dict[str, Any] | None:
        """Fetch one draft. If ``user_id`` is provided, returns ``None`` when
        the draft belongs to someone else (cross-user isolation)."""
        from digitorn.core.models import BuildDraft

        async with self._session_factory() as db:
            stmt = select(BuildDraft).where(BuildDraft.id == draft_id)
            if user_id is not None:
                stmt = stmt.where(BuildDraft.user_id == user_id)
            result = await db.execute(stmt)
            row = result.scalar_one_or_none()
            return self._row_to_dict(row) if row else None

    async def list_for_user(
        self,
        user_id: str,
        *,
        status: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """List a user's drafts, most recently updated first."""
        from digitorn.core.models import BuildDraft

        async with self._session_factory() as db:
            stmt = (
                select(BuildDraft)
                .where(BuildDraft.user_id == user_id)
                .order_by(BuildDraft.updated_at.desc())
            )
            if status:
                stmt = stmt.where(BuildDraft.status == status)
            stmt = stmt.offset(offset).limit(min(limit, MAX_DRAFTS_PER_USER))
            result = await db.execute(stmt)
            return [self._row_to_dict(r) for r in result.scalars().all()]

    async def count_for_user(self, user_id: str) -> int:
        """Return the user's current draft count (any status)."""
        from digitorn.core.models import BuildDraft

        async with self._session_factory() as db:
            result = await db.execute(
                select(func.count(BuildDraft.id)).where(BuildDraft.user_id == user_id)
            )
            return int(result.scalar() or 0)

    async def update(
        self,
        draft_id: str,
        *,
        user_id: str | None = None,
        name: str | None = None,
        status: str | None = None,
        current_yaml: str | None = None,
        chat_history: list | None = None,
        builder_state: dict | None = None,
        deployed_app_id: str | None = None,
    ) -> dict[str, Any] | None:
        """Update one or more fields on a draft.

        Any non-``None`` argument overwrites that field. ``current_yaml``
        also rewrites the on-disk file. ``chat_history`` is capped at
        ``MAX_CHAT_MESSAGES`` to keep the row size bounded.
        """
        from digitorn.core.models import BuildDraft

        async with self._session_factory() as db:
            stmt = select(BuildDraft).where(BuildDraft.id == draft_id)
            if user_id is not None:
                stmt = stmt.where(BuildDraft.user_id == user_id)
            row = (await db.execute(stmt)).scalar_one_or_none()
            if row is None:
                return None

            changed = False
            if name is not None:
                row.name = name
                changed = True
            if status is not None:
                row.status = status
                changed = True
            if current_yaml is not None:
                row.current_yaml = current_yaml
                yaml_path = self._write_yaml_to_disk(row.user_id, row.id, current_yaml)
                row.yaml_path = str(yaml_path)
                changed = True
            if chat_history is not None:
                # Cap to avoid unbounded growth - keep the most recent N.
                if len(chat_history) > MAX_CHAT_MESSAGES:
                    chat_history = chat_history[-MAX_CHAT_MESSAGES:]
                row.chat_history = list(chat_history)
                flag_modified(row, "chat_history")
                changed = True
            if builder_state is not None:
                row.builder_state = dict(builder_state)
                flag_modified(row, "builder_state")
                changed = True
            if deployed_app_id is not None:
                row.deployed_app_id = deployed_app_id
                changed = True

            if changed:
                row.updated_at = datetime.now(timezone.utc)
                await db.commit()
                await db.refresh(row)
            return self._row_to_dict(row)

    async def append_chat_messages(
        self,
        draft_id: str,
        messages: list[dict],
        *,
        user_id: str | None = None,
    ) -> dict[str, Any] | None:
        """Append messages to ``chat_history`` without sending the full list.

        Cheaper than ``update(chat_history=...)`` when the builder agent
        wants to add a single user/assistant exchange - we read the
        existing list, append, cap, and write back.
        """
        from digitorn.core.models import BuildDraft

        async with self._session_factory() as db:
            stmt = select(BuildDraft).where(BuildDraft.id == draft_id)
            if user_id is not None:
                stmt = stmt.where(BuildDraft.user_id == user_id)
            row = (await db.execute(stmt)).scalar_one_or_none()
            if row is None:
                return None
            history = list(row.chat_history or [])
            history.extend(messages)
            if len(history) > MAX_CHAT_MESSAGES:
                history = history[-MAX_CHAT_MESSAGES:]
            row.chat_history = history
            flag_modified(row, "chat_history")
            row.updated_at = datetime.now(timezone.utc)
            await db.commit()
            await db.refresh(row)
            return self._row_to_dict(row)

    async def delete(self, draft_id: str, user_id: str | None = None) -> bool:
        """Delete a draft (DB row + on-disk YAML directory)."""
        from digitorn.core.models import BuildDraft

        async with self._session_factory() as db:
            stmt = select(BuildDraft).where(BuildDraft.id == draft_id)
            if user_id is not None:
                stmt = stmt.where(BuildDraft.user_id == user_id)
            row = (await db.execute(stmt)).scalar_one_or_none()
            if row is None:
                return False

            owner = row.user_id
            del_stmt = delete(BuildDraft).where(BuildDraft.id == draft_id)
            if user_id is not None:
                del_stmt = del_stmt.where(BuildDraft.user_id == user_id)
            result = await db.execute(del_stmt)
            await db.commit()
            deleted = (result.rowcount or 0) > 0

        # Best-effort disk cleanup - never crash on this, the row is gone.
        if deleted:
            try:
                shutil.rmtree(_drafts_dir(owner, draft_id), ignore_errors=True)
            except Exception as exc:
                logger.debug("draft disk cleanup failed: %s", exc)
        return deleted

    # ── Disk mirror ──────────────────────────────────────────────

    def _write_yaml_to_disk(self, user_id: str, draft_id: str, yaml_text: str) -> Path:
        """Write the current YAML to the draft's on-disk app.yaml.

        Idempotent - the file is overwritten on every call. Parent
        directories are created lazily on first write.
        """
        directory = _drafts_dir(user_id, draft_id)
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / "app.yaml"
        target.write_text(yaml_text, encoding="utf-8")
        return target

    # ── Row → dict ───────────────────────────────────────────────

    @staticmethod
    def _row_to_dict(row: Any) -> dict[str, Any]:
        return {
            "id": row.id,
            "user_id": row.user_id,
            "name": row.name,
            "status": row.status,
            "current_yaml": row.current_yaml or "",
            "yaml_path": row.yaml_path or "",
            "chat_history": list(row.chat_history or []),
            "builder_state": dict(row.builder_state or {}),
            "deployed_app_id": row.deployed_app_id or "",
            "created_at": row.created_at.isoformat() if row.created_at else None,
            "updated_at": row.updated_at.isoformat() if row.updated_at else None,
        }
