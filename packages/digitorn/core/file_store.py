"""File Store: disk-backed storage for arbitrary chat attachments."""

from __future__ import annotations

import base64
import hashlib
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import uuid4

logger = logging.getLogger(__name__)

_MIME_TO_EXT: dict[str, str] = {
    "application/pdf": ".pdf",
    "application/msword": ".doc",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    "application/vnd.ms-excel": ".xls",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
    "application/vnd.ms-powerpoint": ".ppt",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": ".pptx",
    "application/vnd.oasis.opendocument.text": ".odt",
    "application/vnd.oasis.opendocument.spreadsheet": ".ods",
    "application/vnd.oasis.opendocument.presentation": ".odp",
    "application/rtf": ".rtf",
    "text/rtf": ".rtf",
    "text/plain": ".txt",
    "text/markdown": ".md",
    "text/csv": ".csv",
    "text/tab-separated-values": ".tsv",
    "text/html": ".html",
    "text/css": ".css",
    "application/json": ".json",
    "application/xml": ".xml",
    "application/x-yaml": ".yaml",
    "application/toml": ".toml",
    "audio/mpeg": ".mp3",
    "audio/mp3": ".mp3",
    "audio/wav": ".wav",
    "audio/x-wav": ".wav",
    "audio/ogg": ".ogg",
    "audio/m4a": ".m4a",
    "audio/x-m4a": ".m4a",
    "audio/flac": ".flac",
    "audio/aac": ".aac",
    "video/mp4": ".mp4",
    "video/quicktime": ".mov",
    "video/webm": ".webm",
    "video/x-matroska": ".mkv",
    "application/zip": ".zip",
    "application/x-tar": ".tar",
    "application/gzip": ".gz",
    "application/x-7z-compressed": ".7z",
}


def ext_for_mime(mime: str) -> str:
    """MIME → file extension, with `.bin` fallback."""
    return _MIME_TO_EXT.get((mime or "").lower(), ".bin")


def _resolve_extension(mime: str, original_name: str) -> str:
    mime_ext = _MIME_TO_EXT.get((mime or "").lower())
    if mime_ext:
        return mime_ext
    suffix = Path(original_name or "").suffix
    if suffix:
        return suffix.lower()
    return ".bin"


@dataclass
class FileRef:
    """Lightweight reference to a stored attachment."""

    file_id: str
    path: str
    mime: str
    size: int
    sha256: str
    original_name: str = ""
    message_id: str = ""
    created_at: float = field(default_factory=time.time)
    format: str = ""
    index_status: str = "pending"
    index_chunks: int = 0
    index_error: str = ""
    extracted_text: str = ""
    announced: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "file_id": self.file_id,
            "mime": self.mime,
            "size": self.size,
            "sha256": self.sha256,
            "original_name": self.original_name,
            "message_id": self.message_id,
            "created_at": self.created_at,
            "format": self.format,
            "index_status": self.index_status,
            "index_chunks": self.index_chunks,
            "index_error": self.index_error,
        }


class FileStore:
    """Disk-backed file storage with session isolation."""

    def __init__(self, base_dir: str | Path | None = None) -> None:
        if base_dir:
            self._base_dir = Path(base_dir)
        else:
            try:
                from digitorn.core.config import get_settings
                cfg_dir = getattr(
                    getattr(get_settings(), "files", None),
                    "storage_dir",
                    "",
                )
                if cfg_dir:
                    self._base_dir = Path(cfg_dir)
                else:
                    self._base_dir = Path.home() / ".digitorn" / "files"
            except Exception:
                self._base_dir = Path.home() / ".digitorn" / "files"
        self._base_dir.mkdir(parents=True, exist_ok=True)
        self._workspaces_root = Path.home() / ".digitorn" / "workspaces"
        self._refs: dict[str, dict[str, FileRef]] = {}

    def _resolve_session_dir(self, session_id: str, app_id: str) -> Path:
        if app_id:
            return self._workspaces_root / app_id / session_id / "attachments"
        return self._base_dir / session_id

    async def store(
        self,
        data: bytes,
        mime: str,
        session_id: str,
        original_name: str = "",
        message_id: str = "",
        app_id: str = "",
    ) -> FileRef:
        """Persist data for session_id and return a FileRef."""
        sha256 = hashlib.sha256(data).hexdigest()

        existing = self._refs.get(session_id) or {}
        for ref in existing.values():
            if ref.sha256 == sha256:
                logger.debug(
                    "file_dedup session=%s sha=%s reused=%s",
                    session_id[:8], sha256[:8], ref.file_id,
                )
                return ref

        file_id = uuid4().hex[:12]
        ext = _resolve_extension(mime, original_name)

        session_dir = self._resolve_session_dir(session_id, app_id)
        session_dir.mkdir(parents=True, exist_ok=True)
        path = session_dir / f"{file_id}{ext}"
        path.write_bytes(data)

        ref = FileRef(
            file_id=file_id,
            path=str(path),
            mime=mime or "application/octet-stream",
            size=len(data),
            sha256=sha256,
            original_name=original_name,
            message_id=message_id,
        )
        self._refs.setdefault(session_id, {})[file_id] = ref

        logger.info(
            "file_stored id=%s session=%s size=%d mime=%s name=%r",
            file_id, session_id[:8], len(data), ref.mime, original_name,
        )
        return ref

    async def store_base64(
        self,
        b64_data: str,
        mime: str,
        session_id: str,
        original_name: str = "",
        message_id: str = "",
        app_id: str = "",
    ) -> FileRef:
        """Decode the base64 payload and hand off to store()."""
        if "," in b64_data and b64_data.lstrip().startswith("data:"):
            b64_data = b64_data.split(",", 1)[1]
        data = base64.b64decode(b64_data)
        return await self.store(
            data, mime, session_id,
            original_name=original_name,
            message_id=message_id,
            app_id=app_id,
        )

    async def get(self, file_id: str, session_id: str) -> bytes | None:
        ref = self._get_ref(file_id, session_id)
        if ref is None:
            return None
        path = Path(ref.path)
        if not path.exists():
            return None
        return path.read_bytes()

    def get_ref(self, file_id: str, session_id: str) -> FileRef | None:
        return self._get_ref(file_id, session_id)

    def list_refs(self, session_id: str) -> list[FileRef]:
        return list(self._refs.get(session_id, {}).values())

    def update_index_status(
        self,
        file_id: str,
        session_id: str,
        *,
        status: str | None = None,
        chunks: int | None = None,
        error: str | None = None,
        format: str | None = None,
        extracted_text: str | None = None,
    ) -> FileRef | None:
        """Patch the RAG pipeline status fields on an existing ref."""
        ref = self._refs.get(session_id, {}).get(file_id)
        if ref is None:
            return None
        if status is not None:
            ref.index_status = status
        if chunks is not None:
            ref.index_chunks = chunks
        if error is not None:
            ref.index_error = error
        if format is not None:
            ref.format = format
        if extracted_text is not None:
            ref.extracted_text = extracted_text
        return ref

    def mark_announced(self, file_ids: list[str], session_id: str) -> None:
        """Flag refs as already-announced to the LLM."""
        bucket = self._refs.get(session_id, {})
        for fid in file_ids:
            ref = bucket.get(fid)
            if ref is not None:
                ref.announced = True

    def cleanup_session(self, session_id: str) -> int:
        """Drop the in-memory refs for session_id."""
        refs = self._refs.pop(session_id, {})
        return len(refs)

    def _get_ref(self, file_id: str, session_id: str) -> FileRef | None:
        refs = self._refs.get(session_id, {})
        ref = refs.get(file_id)
        if ref is not None:
            return ref
        candidates: list[Path] = []
        if self._workspaces_root.exists():
            for app_dir in self._workspaces_root.iterdir():
                if not app_dir.is_dir():
                    continue
                sd = app_dir / session_id / "attachments"
                if sd.exists():
                    candidates.append(sd)
        legacy_dir = self._base_dir / session_id
        if legacy_dir.exists():
            candidates.append(legacy_dir)
        for session_dir in candidates:
            for f in session_dir.iterdir():
                if f.stem == file_id:
                    data = f.read_bytes()
                    ref = FileRef(
                        file_id=file_id,
                        path=str(f),
                        mime="application/octet-stream",
                        size=len(data),
                        sha256=hashlib.sha256(data).hexdigest(),
                    )
                    self._refs.setdefault(session_id, {})[file_id] = ref
                    return ref
        return None


_store: FileStore | None = None


def get_file_store() -> FileStore:
    """Return the daemon-wide file store singleton."""
    global _store
    if _store is None:
        _store = FileStore()
    return _store
