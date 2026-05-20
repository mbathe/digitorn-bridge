"""Shared helpers for the chat attachments pipeline."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Final

logger = logging.getLogger(__name__)


_KB_PREFIX: Final[str] = "chat-session-"


def kb_name_for_session(session_id: str) -> str:
    """Return the canonical RAG knowledge-base name for a chat session."""
    return f"{_KB_PREFIX}{session_id}"


_MAGIC_BYTES: Final[tuple[tuple[bytes, str], ...]] = (
    (b"%PDF", ".pdf"),
    (b"PK\x03\x04", ".zip"),  # refined below for docx/xlsx by extension
    (b"{\\rtf", ".rtf"),
    (b"\x89PNG\r\n\x1a\n", ".png"),
    (b"\xff\xd8\xff", ".jpg"),
    (b"GIF87a", ".gif"),
    (b"GIF89a", ".gif"),
    (b"RIFF", ".webp"),  # need 4 more bytes; refined below
    (b"BM", ".bmp"),
    (b"<?xml", ".xml"),
    (b"<!DOCTYPE", ".html"),
    (b"<html", ".html"),
    (b"\x1f\x8b", ".gz"),
)

# ZIP-based Office formats share PK\x03\x04 - disambiguate by extension
# when the user-supplied filename carries one.
_ZIP_OFFICE: Final[frozenset[str]] = frozenset(
    {".docx", ".xlsx", ".pptx", ".odt", ".ods", ".odp", ".epub"},
)


def sniff_format(path: Path | str, *, filename_hint: str = "") -> str:
    """Return a canonical lowercase suffix (`.pdf`, `.txt`, ...)."""
    p = Path(path)
    try:
        with p.open("rb") as fh:
            header = fh.read(16)
    except OSError as exc:
        logger.debug("sniff_format: cannot read header for %s: %s", p, exc)
        return p.suffix.lower() or _hint_suffix(filename_hint) or ".txt"

    for magic, fmt in _MAGIC_BYTES:
        if header.startswith(magic):
            if fmt == ".zip":
                hint = _hint_suffix(filename_hint) or p.suffix.lower()
                if hint in _ZIP_OFFICE:
                    return hint
                return ".zip"
            if fmt == ".webp" and len(header) >= 12 and header[8:12] != b"WEBP":
                # RIFF without WEBP marker - probably WAV or AVI. Fall
                # through to extension.
                break
            return fmt

    # Magic bytes inconclusive - trust the filename / on-disk suffix.
    return p.suffix.lower() or _hint_suffix(filename_hint) or ".txt"


def _hint_suffix(filename_hint: str) -> str:
    """Extract a lowercase `.ext` from a free-form filename string."""
    if not filename_hint:
        return ""
    return Path(filename_hint).suffix.lower()
