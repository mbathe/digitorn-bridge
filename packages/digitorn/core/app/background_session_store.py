"""Background Session Store - manages background sessions per user."""

from __future__ import annotations

import logging
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import delete, select, update, func

logger = logging.getLogger(__name__)


def _payload_files_dir(app_id: str, session_id: str) -> Path:
    """Resolve the on-disk directory holding a session's uploaded files."""
    # Safety: reject path traversal at the segment level.
    for name in (app_id, session_id):
        if not name or "/" in name or "\\" in name or ".." in name:
            raise ValueError(f"unsafe id segment: {name!r}")
    return Path.home() / ".digitorn" / "apps" / app_id / "sessions" / session_id / "payload"


class BackgroundSessionStore:
    """Database-backed background session store."""

    def __init__(self, session_factory: Any) -> None:
        self._session_factory = session_factory

    async def create(
        self,
        app_id: str,
        user_id: str,
        *,
        name: str = "",
        params: dict | None = None,
        routing_keys: dict | None = None,
        workspace: str = "",
    ) -> dict[str, Any]:
        """Create a new background session. Returns the session dict."""
        from digitorn.core.models import BackgroundSession

        session = BackgroundSession(
            app_id=app_id,
            user_id=user_id,
            name=name,
            params=params or {},
            routing_keys=routing_keys or {},
            workspace=workspace,
            status="active",
        )

        async with self._session_factory() as db:
            db.add(session)
            await db.commit()
            await db.refresh(session)
            return self._row_to_dict(session)

    async def get(self, session_id: str) -> dict[str, Any] | None:
        """Get a single background session."""
        from digitorn.core.models import BackgroundSession

        async with self._session_factory() as db:
            result = await db.execute(
                select(BackgroundSession).where(BackgroundSession.id == session_id)
            )
            row = result.scalar_one_or_none()
            return self._row_to_dict(row) if row else None

    async def get_or_create_mono(
        self,
        app_id: str,
        user_id: str,
    ) -> dict[str, Any]:
        """Get the mono session for a user, or create it if missing."""
        from digitorn.core.models import BackgroundSession

        async with self._session_factory() as db:
            result = await db.execute(
                select(BackgroundSession).where(
                    BackgroundSession.app_id == app_id,
                    BackgroundSession.user_id == user_id,
                )
            )
            row = result.scalar_one_or_none()
            if row is not None:
                return self._row_to_dict(row)

        # Create new mono session
        return await self.create(
            app_id=app_id,
            user_id=user_id,
            name="default",
        )

    async def list_for_app(
        self,
        app_id: str,
        user_id: str | None = None,
        status: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """List background sessions for an app, optionally filtered by user."""
        from digitorn.core.models import BackgroundSession

        async with self._session_factory() as db:
            stmt = (
                select(BackgroundSession)
                .where(BackgroundSession.app_id == app_id)
                .order_by(BackgroundSession.created_at.desc())
            )
            if user_id:
                stmt = stmt.where(BackgroundSession.user_id == user_id)
            if status:
                stmt = stmt.where(BackgroundSession.status == status)
            stmt = stmt.offset(offset).limit(limit)

            result = await db.execute(stmt)
            return [self._row_to_dict(row) for row in result.scalars().all()]

    async def count_for_user(self, app_id: str, user_id: str) -> int:
        """Count active sessions for a user (for max_sessions_per_user check)."""
        from digitorn.core.models import BackgroundSession

        async with self._session_factory() as db:
            result = await db.execute(
                select(func.count(BackgroundSession.id)).where(
                    BackgroundSession.app_id == app_id,
                    BackgroundSession.user_id == user_id,
                    BackgroundSession.status == "active",
                )
            )
            return result.scalar() or 0

    async def update_status(self, session_id: str, status: str) -> bool:
        """Update session status (active, paused, stopped)."""
        from digitorn.core.models import BackgroundSession

        async with self._session_factory() as db:
            result = await db.execute(
                update(BackgroundSession)
                .where(BackgroundSession.id == session_id)
                .values(status=status)
            )
            await db.commit()
            return (result.rowcount or 0) > 0

    async def touch(self, session_id: str) -> None:
        """Update last_active_at and increment activation_count."""
        from digitorn.core.models import BackgroundSession

        async with self._session_factory() as db:
            await db.execute(
                update(BackgroundSession)
                .where(BackgroundSession.id == session_id)
                .values(
                    last_active_at=datetime.now(timezone.utc),
                    activation_count=BackgroundSession.activation_count + 1,
                )
            )
            await db.commit()

    async def delete(self, session_id: str) -> bool:
        """Delete a background session."""
        from digitorn.core.models import BackgroundSession

        async with self._session_factory() as db:
            result = await db.execute(
                delete(BackgroundSession).where(BackgroundSession.id == session_id)
            )
            await db.commit()
            return (result.rowcount or 0) > 0

    async def delete_for_app(self, app_id: str) -> int:
        """Delete all background sessions for an app."""
        from digitorn.core.models import BackgroundSession

        async with self._session_factory() as db:
            result = await db.execute(
                delete(BackgroundSession).where(BackgroundSession.app_id == app_id)
            )
            await db.commit()
            return result.rowcount or 0


    async def resolve_routing(
        self,
        app_id: str,
        trigger_routing: str,
        routing_key_value: str,
    ) -> list[dict[str, Any]]:
        """Resolve a routing key to target sessions."""
        from digitorn.core.models import BackgroundSession

        async with self._session_factory() as db:
            if trigger_routing == "broadcast":
                # All active sessions for this app
                result = await db.execute(
                    select(BackgroundSession).where(
                        BackgroundSession.app_id == app_id,
                        BackgroundSession.status == "active",
                    )
                )
                return [self._row_to_dict(r) for r in result.scalars().all()]

            elif trigger_routing == "user":
                # All active sessions for the identified user
                result = await db.execute(
                    select(BackgroundSession).where(
                        BackgroundSession.app_id == app_id,
                        BackgroundSession.user_id == routing_key_value,
                        BackgroundSession.status == "active",
                    )
                )
                rows = result.scalars().all()
                if rows:
                    return [self._row_to_dict(r) for r in rows]

                # Also check routing_keys JSON for indirect mapping
                # e.g. routing_keys: {"telegram": "chat_12345"}
                all_active = await db.execute(
                    select(BackgroundSession).where(
                        BackgroundSession.app_id == app_id,
                        BackgroundSession.status == "active",
                    )
                )
                return [
                    self._row_to_dict(r) for r in all_active.scalars().all()
                    if routing_key_value in str(r.routing_keys.values())
                ]

            elif trigger_routing == "session":
                # Direct session lookup by routing key
                all_active = await db.execute(
                    select(BackgroundSession).where(
                        BackgroundSession.app_id == app_id,
                        BackgroundSession.status == "active",
                    )
                )
                for r in all_active.scalars().all():
                    rk = r.routing_keys or {}
                    if routing_key_value in rk.values() or r.id == routing_key_value:
                        return [self._row_to_dict(r)]
                return []

        return []


    _PAYLOAD_KEY = "_payload"

    async def set_payload(
        self,
        session_id: str,
        *,
        prompt: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Create or update the prompt + metadata part of a session payload."""
        from digitorn.core.models import BackgroundSession

        async with self._session_factory() as db:
            result = await db.execute(
                select(BackgroundSession).where(BackgroundSession.id == session_id)
            )
            row = result.scalar_one_or_none()
            if row is None:
                raise KeyError(f"Background session not found: {session_id}")

            params = dict(row.params or {})
            payload = dict(params.get(self._PAYLOAD_KEY) or {})

            if prompt is not None:
                payload["prompt"] = str(prompt)
            if metadata is not None:
                # Shallow merge - callers pass the subset they want to
                # change, existing keys not in `metadata` are kept.
                meta = dict(payload.get("metadata") or {})
                meta.update(metadata)
                payload["metadata"] = meta
            # Files list is managed by add/remove_payload_file; we
            # preserve whatever was there.
            payload.setdefault("files", [])

            params[self._PAYLOAD_KEY] = payload
            row.params = params
            # Force SQLA to see the JSON column as dirty (mutable dict edits
            # on some dialects don't trigger the change tracker otherwise).
            from sqlalchemy.orm.attributes import flag_modified
            flag_modified(row, "params")
            await db.commit()
            return payload

    async def get_payload(self, session_id: str) -> dict[str, Any]:
        """Return the session's payload dict (prompt + metadata + files)."""
        from digitorn.core.models import BackgroundSession

        async with self._session_factory() as db:
            result = await db.execute(
                select(BackgroundSession).where(BackgroundSession.id == session_id)
            )
            row = result.scalar_one_or_none()
            if row is None:
                raise KeyError(f"Background session not found: {session_id}")

            params = row.params or {}
            payload = dict(params.get(self._PAYLOAD_KEY) or {})
            return {
                "prompt": payload.get("prompt", ""),
                "metadata": payload.get("metadata") or {},
                "files": payload.get("files") or [],
            }

    async def add_payload_file(
        self,
        session_id: str,
        *,
        filename: str,
        content: bytes,
        mime_type: str | None = None,
    ) -> dict[str, Any]:
        """Persist a file on disk and record it in the session payload."""
        from digitorn.core.models import BackgroundSession

        # Fetch session row to learn its app_id (we need it for the path)
        async with self._session_factory() as db:
            result = await db.execute(
                select(BackgroundSession).where(BackgroundSession.id == session_id)
            )
            row = result.scalar_one_or_none()
            if row is None:
                raise KeyError(f"Background session not found: {session_id}")
            app_id = row.app_id

        # Resolve a safe on-disk path
        safe_name = Path(filename).name  # strip any directories
        if not safe_name or safe_name.startswith(".") or safe_name in ("..", "/"):
            raise ValueError(f"invalid filename: {filename!r}")
        files_dir = _payload_files_dir(app_id, session_id)
        files_dir.mkdir(parents=True, exist_ok=True)
        dest = files_dir / safe_name
        dest.write_bytes(content)

        file_entry = {
            "name": safe_name,
            "path": str(dest),
            "mime_type": mime_type or "",
            "size_bytes": len(content),
        }

        # Update the payload's files list (replace if same name)
        async with self._session_factory() as db:
            result = await db.execute(
                select(BackgroundSession).where(BackgroundSession.id == session_id)
            )
            row = result.scalar_one_or_none()
            if row is None:
                raise KeyError(f"Background session not found: {session_id}")

            params = dict(row.params or {})
            payload = dict(params.get(self._PAYLOAD_KEY) or {})
            files = [f for f in (payload.get("files") or []) if f.get("name") != safe_name]
            files.append(file_entry)
            payload["files"] = files
            payload.setdefault("prompt", "")
            payload.setdefault("metadata", {})
            params[self._PAYLOAD_KEY] = payload
            row.params = params
            from sqlalchemy.orm.attributes import flag_modified
            flag_modified(row, "params")
            await db.commit()

        return {
            "prompt": payload.get("prompt", ""),
            "metadata": payload.get("metadata") or {},
            "files": files,
        }

    async def remove_payload_file(
        self, session_id: str, filename: str,
    ) -> bool:
        """Delete a file from disk and remove its entry from the payload."""
        from digitorn.core.models import BackgroundSession

        async with self._session_factory() as db:
            result = await db.execute(
                select(BackgroundSession).where(BackgroundSession.id == session_id)
            )
            row = result.scalar_one_or_none()
            if row is None:
                return False
            app_id = row.app_id

        safe_name = Path(filename).name
        files_dir = _payload_files_dir(app_id, session_id)
        dest = files_dir / safe_name
        removed = False
        if dest.is_file():
            try:
                dest.unlink()
                removed = True
            except OSError:
                pass

        async with self._session_factory() as db:
            result = await db.execute(
                select(BackgroundSession).where(BackgroundSession.id == session_id)
            )
            row = result.scalar_one_or_none()
            if row is None:
                return removed
            params = dict(row.params or {})
            payload = dict(params.get(self._PAYLOAD_KEY) or {})
            files = [f for f in (payload.get("files") or []) if f.get("name") != safe_name]
            if len(files) != len(payload.get("files") or []):
                payload["files"] = files
                params[self._PAYLOAD_KEY] = payload
                row.params = params
                from sqlalchemy.orm.attributes import flag_modified
                flag_modified(row, "params")
                await db.commit()
                removed = True
        return removed

    async def clear_payload(self, session_id: str) -> bool:
        """Delete every payload file + wipe the payload from the DB row."""
        from digitorn.core.models import BackgroundSession

        async with self._session_factory() as db:
            result = await db.execute(
                select(BackgroundSession).where(BackgroundSession.id == session_id)
            )
            row = result.scalar_one_or_none()
            if row is None:
                return False
            app_id = row.app_id

            params = dict(row.params or {})
            params.pop(self._PAYLOAD_KEY, None)
            row.params = params
            from sqlalchemy.orm.attributes import flag_modified
            flag_modified(row, "params")
            await db.commit()

        try:
            shutil.rmtree(_payload_files_dir(app_id, session_id), ignore_errors=True)
        except Exception as exc:
            logger.debug("background_session_store best-effort block failed: %s", exc)
        return True

    @staticmethod
    def _row_to_dict(row: Any) -> dict[str, Any]:
        return {
            "id": row.id,
            "app_id": row.app_id,
            "user_id": row.user_id,
            "name": row.name,
            "status": row.status,
            "params": row.params or {},
            "routing_keys": row.routing_keys or {},
            "workspace": row.workspace or "",
            "created_at": str(row.created_at) if row.created_at else None,
            "last_active_at": str(row.last_active_at) if row.last_active_at else None,
            "activation_count": row.activation_count or 0,
        }
