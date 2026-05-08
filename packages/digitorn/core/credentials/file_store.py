"""File-backed credential store: encrypted blobs on local disk.

Same crypto guarantees as the legacy DB-backed store -- AES-GCM via
``Cipher`` from ``digitorn.core.credentials.cipher`` -- but rows live
on the filesystem instead of Postgres. Used by the local-mode runtime
(no DB) and as the default for single-machine deployments.

Layout::

    ~/.digitorn/credentials/
    └── <user_id>/
        ├── <credential_id>.json    (encrypted payload + metadata)
        └── ...

Each ``<credential_id>.json`` is a small JSON envelope::

    {
      "credential_id": "...",
      "user_id": "...",
      "provider": "github_copilot",
      "label": "Production",
      "scope": "per_user",
      "created_at": "2026-...",
      "updated_at": "2026-...",
      "payload_b64": "...",       (encrypted blob)
      "nonce_b64": "..."           (AES-GCM nonce)
    }

Atomicity: writes go via tmp + ``os.replace`` so a crash mid-write
never leaves a partial file. Reads tolerate missing/corrupt files
gracefully (return None, log at warning).

Concurrency: single-writer-per-credential. Two processes touching the
same credential are NOT supported (and would NOT be supported in DB
mode either without row locks).
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import tempfile
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from digitorn.core.credentials.cipher import CipherError, VersionedCipher as Cipher

logger = logging.getLogger(__name__)


CREDENTIALS_FILENAME_EXT = ".json"


def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class CredentialSummary:
    """Lightweight metadata returned by ``list_user_credentials``.
    The encrypted payload is NOT included -- use ``get_credential``
    to load + decrypt."""

    credential_id: str
    user_id: str
    provider: str
    label: str
    scope: str
    created_at: str
    updated_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "credential_id": self.credential_id,
            "user_id": self.user_id,
            "provider": self.provider,
            "label": self.label,
            "scope": self.scope,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


class FileCredentialStore:
    """Per-user encrypted credential store on local disk.

    Construct with a Cipher instance (same one the rest of the daemon
    uses). The store does NOT manage the master key; that's the
    cipher's provider responsibility."""

    def __init__(self, *, root: Path, cipher: Cipher) -> None:
        self._root = Path(root)
        self._cipher = cipher
        self._root.mkdir(parents=True, exist_ok=True)

    @property
    def root(self) -> Path:
        return self._root

    def _user_dir(self, user_id: str) -> Path:
        if not user_id or "/" in user_id or ".." in user_id:
            raise ValueError(f"invalid user_id: {user_id!r}")
        return self._root / user_id

    def _path_for(self, user_id: str, credential_id: str) -> Path:
        if not credential_id or "/" in credential_id or ".." in credential_id:
            raise ValueError(f"invalid credential_id: {credential_id!r}")
        return self._user_dir(user_id) / f"{credential_id}{CREDENTIALS_FILENAME_EXT}"

    async def put_credential(
        self,
        *,
        user_id: str,
        provider: str,
        label: str,
        fields: dict[str, Any],
        scope: str = "per_user",
        credential_id: str | None = None,
    ) -> CredentialSummary:
        """Encrypt + persist. Returns the summary metadata.

        ``credential_id`` is generated if not supplied (UUID4 hex).
        Existing files are overwritten atomically.
        """
        cid = credential_id or uuid.uuid4().hex
        payload, nonce = await self._cipher.encrypt(fields)
        now = _utc_iso()
        envelope = {
            "credential_id": cid,
            "user_id": user_id,
            "provider": provider,
            "label": label,
            "scope": scope,
            "created_at": now,
            "updated_at": now,
            "payload_b64": base64.b64encode(payload).decode("ascii"),
            "nonce_b64": base64.b64encode(nonce).decode("ascii"),
        }
        path = self._path_for(user_id, cid)
        await asyncio.to_thread(self._atomic_write, path, envelope)
        return CredentialSummary(
            credential_id=cid, user_id=user_id, provider=provider,
            label=label, scope=scope,
            created_at=now, updated_at=now,
        )

    async def get_credential(
        self, *, user_id: str, credential_id: str,
    ) -> dict[str, Any] | None:
        """Decrypt + return the original ``fields`` dict, or ``None``
        when the credential doesn't exist or is corrupt."""
        envelope = await asyncio.to_thread(
            self._read_envelope, user_id, credential_id,
        )
        if envelope is None:
            return None
        try:
            payload = base64.b64decode(envelope["payload_b64"])
            nonce = base64.b64decode(envelope["nonce_b64"])
            return await self._cipher.decrypt(payload, nonce)
        except (CipherError, KeyError, ValueError) as exc:
            logger.warning(
                "credential_decrypt_failed user=%s cid=%s err=%s",
                user_id, credential_id, exc,
            )
            return None

    async def get_summary(
        self, *, user_id: str, credential_id: str,
    ) -> CredentialSummary | None:
        """Read the metadata WITHOUT decrypting. Cheap probe call."""
        envelope = await asyncio.to_thread(
            self._read_envelope, user_id, credential_id,
        )
        if envelope is None:
            return None
        return CredentialSummary(
            credential_id=envelope["credential_id"],
            user_id=envelope["user_id"],
            provider=envelope["provider"],
            label=envelope["label"],
            scope=envelope.get("scope", "per_user"),
            created_at=envelope["created_at"],
            updated_at=envelope.get("updated_at", envelope["created_at"]),
        )

    async def list_user_credentials(
        self, *, user_id: str,
    ) -> list[CredentialSummary]:
        """List all credentials owned by ``user_id``. Empty list when
        the user dir doesn't exist."""
        return await asyncio.to_thread(self._list_sync, user_id)

    async def delete_credential(
        self, *, user_id: str, credential_id: str,
    ) -> bool:
        """Delete a credential file. Returns True when removed,
        False when the file didn't exist."""
        return await asyncio.to_thread(
            self._delete_sync, user_id, credential_id,
        )

    async def update_label(
        self, *, user_id: str, credential_id: str, new_label: str,
    ) -> bool:
        """Rename a credential. Decrypt + re-encrypt to refresh the
        ``updated_at`` field; the ciphertext changes (new nonce) but
        the ``credential_id`` stays the same."""
        envelope = await asyncio.to_thread(
            self._read_envelope, user_id, credential_id,
        )
        if envelope is None:
            return False
        envelope["label"] = new_label
        envelope["updated_at"] = _utc_iso()
        path = self._path_for(user_id, credential_id)
        await asyncio.to_thread(self._atomic_write, path, envelope)
        return True

    def _read_envelope(
        self, user_id: str, credential_id: str,
    ) -> dict[str, Any] | None:
        path = self._path_for(user_id, credential_id)
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning(
                "credential_envelope_corrupt user=%s cid=%s err=%s",
                user_id, credential_id, exc,
            )
            return None

    def _list_sync(self, user_id: str) -> list[CredentialSummary]:
        try:
            user_dir = self._user_dir(user_id)
        except ValueError:
            return []
        if not user_dir.exists():
            return []
        out: list[CredentialSummary] = []
        for entry in user_dir.iterdir():
            if not entry.is_file() or entry.suffix != CREDENTIALS_FILENAME_EXT:
                continue
            try:
                env = json.loads(entry.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError) as exc:
                logger.warning(
                    "credential_list_skip_bad_file path=%s err=%s",
                    entry, exc,
                )
                continue
            try:
                out.append(CredentialSummary(
                    credential_id=env["credential_id"],
                    user_id=env["user_id"],
                    provider=env["provider"],
                    label=env["label"],
                    scope=env.get("scope", "per_user"),
                    created_at=env["created_at"],
                    updated_at=env.get("updated_at", env["created_at"]),
                ))
            except KeyError as exc:
                logger.warning(
                    "credential_list_skip_missing_field path=%s err=%s",
                    entry, exc,
                )
        out.sort(key=lambda s: s.created_at)
        return out

    def _delete_sync(self, user_id: str, credential_id: str) -> bool:
        try:
            path = self._path_for(user_id, credential_id)
        except ValueError:
            return False
        if not path.exists():
            return False
        try:
            path.unlink()
            return True
        except OSError as exc:
            logger.warning(
                "credential_delete_failed path=%s err=%s", path, exc,
            )
            return False

    @staticmethod
    def _atomic_write(path: Path, envelope: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(
            prefix=".cred_", suffix=".tmp", dir=str(path.parent),
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(envelope, f, ensure_ascii=False)
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
