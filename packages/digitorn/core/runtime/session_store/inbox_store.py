"""File-backed inbox store: per-user notifications on local disk."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class InboxItem:
    """One inbox notification. Mirrors the legacy DB row shape."""

    id: str
    user_id: str
    kind: str
    title: str
    subtitle: str = ""
    app_id: str | None = None
    session_id: str | None = None
    activation_id: str | None = None
    credential_provider: str | None = None
    item_metadata: dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=_utc_iso)
    read_at: str | None = None
    archived_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "InboxItem":
        return cls(**d)


class FileInboxStore:
    """Per-user inbox persistence on local disk."""

    def __init__(self, *, root: Path) -> None:
        self._root = Path(root)
        self._root.mkdir(parents=True, exist_ok=True)

    @property
    def root(self) -> Path:
        return self._root

    def _user_dir(self, user_id: str) -> Path:
        if not user_id or "/" in user_id or ".." in user_id:
            raise ValueError(f"invalid user_id: {user_id!r}")
        return self._root / user_id

    def _path_for(self, user_id: str, item_id: str) -> Path:
        if not item_id or "/" in item_id or ".." in item_id:
            raise ValueError(f"invalid item_id: {item_id!r}")
        return self._user_dir(user_id) / f"{item_id}.json"

    async def add(
        self,
        *,
        user_id: str,
        kind: str,
        title: str,
        subtitle: str = "",
        app_id: str | None = None,
        session_id: str | None = None,
        activation_id: str | None = None,
        credential_provider: str | None = None,
        item_metadata: dict[str, Any] | None = None,
        item_id: str | None = None,
    ) -> InboxItem:
        """Persist a new item. Returns the stored InboxItem."""
        item = InboxItem(
            id=item_id or uuid.uuid4().hex,
            user_id=user_id,
            kind=kind,
            title=title,
            subtitle=subtitle,
            app_id=app_id,
            session_id=session_id,
            activation_id=activation_id,
            credential_provider=credential_provider,
            item_metadata=dict(item_metadata or {}),
        )
        await asyncio.to_thread(self._write_item, item)
        return item

    async def get(
        self, *, user_id: str, item_id: str,
    ) -> InboxItem | None:
        return await asyncio.to_thread(self._read_item, user_id, item_id)

    async def list(
        self,
        *,
        user_id: str,
        unread_only: bool = False,
        include_archived: bool = False,
        limit: int = 100,
    ) -> list[InboxItem]:
        return await asyncio.to_thread(
            self._list_sync,
            user_id, unread_only, include_archived, limit,
        )

    async def mark_read(
        self, *, user_id: str, item_id: str,
    ) -> bool:
        return await asyncio.to_thread(
            self._mutate, user_id, item_id, "read",
        )

    async def mark_unread(
        self, *, user_id: str, item_id: str,
    ) -> bool:
        return await asyncio.to_thread(
            self._mutate, user_id, item_id, "unread",
        )

    async def archive(
        self, *, user_id: str, item_id: str,
    ) -> bool:
        return await asyncio.to_thread(
            self._mutate, user_id, item_id, "archive",
        )

    async def unarchive(
        self, *, user_id: str, item_id: str,
    ) -> bool:
        return await asyncio.to_thread(
            self._mutate, user_id, item_id, "unarchive",
        )

    async def delete(
        self, *, user_id: str, item_id: str,
    ) -> bool:
        return await asyncio.to_thread(self._delete_sync, user_id, item_id)

    async def count_unread(self, *, user_id: str) -> int:
        return await asyncio.to_thread(self._count_unread_sync, user_id)

    def _read_item(
        self, user_id: str, item_id: str,
    ) -> InboxItem | None:
        try:
            path = self._path_for(user_id, item_id)
        except ValueError:
            return None
        if not path.exists():
            return None
        try:
            return InboxItem.from_dict(
                json.loads(path.read_text(encoding="utf-8"))
            )
        except (json.JSONDecodeError, OSError, TypeError, KeyError) as exc:
            logger.warning(
                "inbox_item_corrupt user=%s id=%s err=%s",
                user_id, item_id, exc,
            )
            return None

    def _list_sync(
        self, user_id: str,
        unread_only: bool, include_archived: bool, limit: int,
    ) -> list[InboxItem]:
        try:
            user_dir = self._user_dir(user_id)
        except ValueError:
            return []
        if not user_dir.exists():
            return []
        items: list[InboxItem] = []
        for entry in user_dir.iterdir():
            if not entry.is_file() or entry.suffix != ".json":
                continue
            try:
                item = InboxItem.from_dict(
                    json.loads(entry.read_text(encoding="utf-8"))
                )
            except (json.JSONDecodeError, OSError, TypeError, KeyError) as exc:
                logger.warning(
                    "inbox_list_skip_bad path=%s err=%s", entry, exc,
                )
                continue
            if unread_only and item.read_at is not None:
                continue
            if not include_archived and item.archived_at is not None:
                continue
            items.append(item)
        items.sort(key=lambda i: i.created_at, reverse=True)
        return items[:limit]

    def _mutate(self, user_id: str, item_id: str, op: str) -> bool:
        item = self._read_item(user_id, item_id)
        if item is None:
            return False
        now = _utc_iso()
        if op == "read":
            item.read_at = now
        elif op == "unread":
            item.read_at = None
        elif op == "archive":
            item.archived_at = now
            if item.read_at is None:
                item.read_at = now
        elif op == "unarchive":
            item.archived_at = None
        else:
            return False
        self._write_item(item)
        return True

    def _delete_sync(self, user_id: str, item_id: str) -> bool:
        try:
            path = self._path_for(user_id, item_id)
        except ValueError:
            return False
        if not path.exists():
            return False
        try:
            path.unlink()
            return True
        except OSError as exc:
            logger.warning(
                "inbox_delete_failed path=%s err=%s", path, exc,
            )
            return False

    def _count_unread_sync(self, user_id: str) -> int:
        try:
            user_dir = self._user_dir(user_id)
        except ValueError:
            return 0
        if not user_dir.exists():
            return 0
        count = 0
        for entry in user_dir.iterdir():
            if not entry.is_file() or entry.suffix != ".json":
                continue
            try:
                d = json.loads(entry.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            if d.get("read_at") is None and d.get("archived_at") is None:
                count += 1
        return count

    def _write_item(self, item: InboxItem) -> None:
        path = self._path_for(item.user_id, item.id)
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(
            prefix=".inbox_", suffix=".tmp", dir=str(path.parent),
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(item.to_dict(), f, ensure_ascii=False)
                f.flush()
                try:
                    os.fsync(f.fileno())
                except OSError:
                    pass
            os.replace(tmp, path)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
