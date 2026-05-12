"""File Store - disk-backed storage for arbitrary chat attachments.

The image-only ``image_store.py`` covers vision payloads. ``file_store``
is its multi-MIME sibling: PDFs, DOCX, spreadsheets, archives, code,
plain text — anything an end-user can drag into the composer.

Design intent:

  * One blob = one ``FileRef`` (id, on-disk path, mime, size, hash,
    original name, optional message_id). The reference is what the
    daemon hands around; the raw bytes stay on disk so the message
    envelope stays light.
  * Storage layout (when ``app_id`` is provided, recommended):
    ``~/.digitorn/workspaces/{app_id}/{session_id}/attachments/
    {file_id}{ext}``. Co-located with the workspace module's own
    per-session daemon-private directory so a session's content
    lives in one place. Legacy layout ``{base}/files/{session_id}/``
    stays supported for callers that don't pass ``app_id``.
  * Session isolation is mandatory — files never leak across
    conversations on the same daemon.
  * ``store()`` does **not** dedupe automatically across sessions
    (each session keeps its own copy so audit + retention stay
    predictable). The SHA-256 is recorded on the ref so an upstream
    RAG ingestion can short-circuit on unchanged content.
  * **Persistence policy**: blobs uploaded into a session are kept
    on disk forever. ``cleanup_session(sid)`` only releases the
    in-memory ref cache; it does NOT delete files. A future hard
    delete requires an explicit admin action (``rm -rf`` on the
    session's workspace dir, or a dedicated retention job).

Usage::

    store = get_file_store()
    ref = await store.store_base64(
        b64_data="...", mime="application/pdf",
        session_id=sid, original_name="report.pdf",
        message_id="msg_42",
    )
    # Hand ``ref.path`` to the rag ingestion or any text extractor.
"""

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

# Coarse MIME → file extension. Covers the catalogue we accept in the
# composer's ``+`` menu (image / document / audio / video). Unknown
# MIMEs fall back to ``.bin`` so the on-disk file still has a sane
# extension for OS tooling.
_MIME_TO_EXT: dict[str, str] = {
    # Documents
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
    # Text / code (only the most common — `mime_for_path` falls back
    # to the on-disk extension for the long tail of code formats).
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
    # Audio
    "audio/mpeg": ".mp3",
    "audio/mp3": ".mp3",
    "audio/wav": ".wav",
    "audio/x-wav": ".wav",
    "audio/ogg": ".ogg",
    "audio/m4a": ".m4a",
    "audio/x-m4a": ".m4a",
    "audio/flac": ".flac",
    "audio/aac": ".aac",
    # Video
    "video/mp4": ".mp4",
    "video/quicktime": ".mov",
    "video/webm": ".webm",
    "video/x-matroska": ".mkv",
    # Archives (kept for completeness — RAG will likely refuse to
    # ingest these directly, but the store accepts them).
    "application/zip": ".zip",
    "application/x-tar": ".tar",
    "application/gzip": ".gz",
    "application/x-7z-compressed": ".7z",
}


def ext_for_mime(mime: str) -> str:
    """MIME → file extension, with `.bin` fallback."""
    return _MIME_TO_EXT.get((mime or "").lower(), ".bin")


def _resolve_extension(mime: str, original_name: str) -> str:
    """Prefer the original filename's extension when the MIME table
    doesn't recognise the type (e.g. ``application/octet-stream`` for
    a ``.py`` file from a browser that didn't sniff it). Falls back to
    ``.bin`` only when both are absent."""
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
    # Anchor back to the user message that carried the upload. Useful
    # when a file is referenced months later and we want to surface
    # which conversation turn introduced it.
    message_id: str = ""
    created_at: float = field(default_factory=time.time)
    # ── Indexing status (RAG pipeline) ──────────────────────────────
    # Canonical suffix sniffed from the bytes (``.pdf``, ``.docx``,
    # ...). Trusted by downstream ingestors over the on-disk extension
    # so a file uploaded without a suffix still hits the right parser.
    format: str = ""
    # ``pending`` (uploaded, indexing in flight), ``indexed`` (added to
    # the session RAG KB, retrievable), ``failed`` (parser / ingestor
    # raised - see ``index_error``), ``not_indexable`` (app didn't load
    # the rag module, file kept on disk for manifest only),
    # ``empty`` (parser succeeded but the document carried no text).
    index_status: str = "pending"
    index_chunks: int = 0
    index_error: str = ""
    # Cached extracted text for the small-doc full-inject path in
    # ``_dispatch._maybe_inject_rag_context``. Empty for big docs (the
    # daemon enforces a cap before storing so RAM stays bounded) or
    # for files where extraction failed.
    extracted_text: str = ""
    # Tracks whether the dispatch layer has already told the LLM
    # about this file (either via the tool-mode manifest or the
    # direct-mode full-inject block). Re-injection on every turn
    # makes the agent re-read the same attachment after each user
    # message, even when the new message contains no new files.
    # Set by ``_dispatch._maybe_inject_rag_context`` after the first
    # successful announcement; cleared on daemon restart (in-memory).
    announced: bool = False

    def to_dict(self) -> dict[str, Any]:
        # Note: ``extracted_text`` is deliberately omitted from the
        # serialised form. It's a daemon-internal cache (potentially
        # multi-KB of contract / paper text) - we don't want it in
        # every HTTP response that echoes the file ref.
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
    """Disk-backed file storage with session isolation.

    Mirrors the surface of ``ImageStore``: store / get / get_ref /
    list_refs / cleanup_session. Async write/read for parity with the
    image side; everything heavy stays on disk.
    """

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
        # Workspace-aligned root: every app-scoped attachment lands
        # under ``~/.digitorn/workspaces/{app_id}/{session_id}/
        # attachments/`` so the on-disk layout matches the agent's
        # WsRead view AND ``rm -rf .../workspaces/{app}/{sid}/`` wipes
        # the whole session (workspace state + attachments) in one
        # shot. The legacy ``~/.digitorn/files/{sid}/`` layout is
        # still honoured for callers that don't pass ``app_id``
        # (background tasks, tests, pre-refactor sessions).
        self._workspaces_root = Path.home() / ".digitorn" / "workspaces"
        # session_id → {file_id → ref}
        self._refs: dict[str, dict[str, FileRef]] = {}

    def _resolve_session_dir(self, session_id: str, app_id: str) -> Path:
        """Return the directory that owns this session's attachment
        blobs. Workspace-aligned when ``app_id`` is provided, legacy
        ``files/<sid>/`` otherwise."""
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
        """Persist ``data`` for ``session_id`` and return a ``FileRef``.

        Dedupes within a single session: if the same SHA-256 was
        already stored for this session, the existing ref is returned
        and no new bytes are written.

        Path layout when ``app_id`` is provided (recommended): the
        blob lands under ``~/.digitorn/workspaces/{app_id}/
        {session_id}/attachments/<file_id>.<ext>``. This co-locates
        attachments with the workspace's own session-private dir so a
        single recursive delete cleans everything up. When ``app_id``
        is empty (legacy callers, tests), falls back to
        ``~/.digitorn/files/{session_id}/<file_id>.<ext>``.
        """
        sha256 = hashlib.sha256(data).hexdigest()

        # In-session dedup. Cross-session dedup is intentionally
        # absent so ``cleanup_session`` can drop files without
        # bookkeeping.
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
        """Decode the base64 payload (with or without ``data:`` URI
        prefix) and hand off to :meth:`store`."""
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
        """Patch the RAG pipeline status fields on an existing ref.

        Returns the updated ref, or ``None`` when the file_id is
        unknown for this session. ``status`` is the canonical state
        (``indexed`` / ``failed`` / ``not_indexable`` / ``empty``);
        ``chunks`` and ``error`` are written verbatim when supplied.
        ``format`` is the sniffed canonical suffix - persisted on the
        ref so a later debug session knows exactly what the ingestor
        thought the file was. ``extracted_text`` caches the full
        document text so the dispatch layer can inject it whole for
        small docs instead of relying on top-k retrieval.
        """
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
        """Flag refs as already-announced to the LLM. Subsequent turns
        skip re-injecting the same attachment manifest."""
        bucket = self._refs.get(session_id, {})
        for fid in file_ids:
            ref = bucket.get(fid)
            if ref is not None:
                ref.announced = True

    def cleanup_session(self, session_id: str) -> int:
        """Drop the in-memory refs for ``session_id``.

        Persistence rule: anything the user uploaded in a session
        stays on disk forever. We only release the RAM cache. The
        next ``get_ref()`` for any of these files will rehydrate
        the ref from disk via the fallback scan in :meth:`_get_ref`.

        Returns the number of refs released from memory (purely
        informational - callers should treat the call as fire-and-
        forget).
        """
        refs = self._refs.pop(session_id, {})
        return len(refs)

    def _get_ref(self, file_id: str, session_id: str) -> FileRef | None:
        refs = self._refs.get(session_id, {})
        ref = refs.get(file_id)
        if ref is not None:
            return ref
        # Fallback: scan the candidate session directories so a daemon
        # restart doesn't lose track of on-disk files. We try the
        # workspace-aligned layout (every app variant of this session)
        # first, then the legacy flat layout. Rehydrated refs carry
        # the absolute path so subsequent reads bypass this scan.
        candidates: list[Path] = []
        if self._workspaces_root.exists():
            # Wildcard the app_id: we don't know which app this
            # session belongs to from inside the store.
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


# ── Singleton ─────────────────────────────────────────────────────

_store: FileStore | None = None


def get_file_store() -> FileStore:
    """Return the daemon-wide file store singleton."""
    global _store
    if _store is None:
        _store = FileStore()
    return _store
