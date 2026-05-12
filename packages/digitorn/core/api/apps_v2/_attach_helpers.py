"""Shared helpers for the chat attachments pipeline.

Single source of truth for two things that used to be duplicated across
``messages.py`` (ingest side) and ``_dispatch.py`` (retrieval side):

  1. The knowledge-base name for a chat session. If the format ever
     drifts between the two sites, ingestion lands in KB-A while
     retrieval queries KB-B and the LLM silently sees no excerpts.
     Funnel both through :func:`kb_name_for_session`.

  2. Format detection by magic bytes. Extension-based dispatch breaks
     the moment a client uploads ``report`` (no suffix, mime
     ``application/pdf``) - the file ends up parsed as UTF-8 garbage
     and the canary never reaches the index. :func:`sniff_format`
     reads the first 16 bytes and returns a canonical suffix
     (``.pdf`` / ``.docx`` / ``.xlsx`` / ``.png`` / ...). Falls back
     to the on-disk extension when bytes are ambiguous.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Final

logger = logging.getLogger(__name__)


_KB_PREFIX: Final[str] = "chat-session-"


def kb_name_for_session(session_id: str) -> str:
    """Return the canonical RAG knowledge-base name for a chat session.

    Used by both the ingest path (``POST /messages``) and the retrieval
    path (pre-turn context injection). NEVER inline the format string
    elsewhere - the two sites would drift and ingestion + query would
    silently target different KBs.
    """
    return f"{_KB_PREFIX}{session_id}"


# ── Format detection ──────────────────────────────────────────────────


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
    """Return a canonical lowercase suffix (``.pdf``, ``.txt``, ...).

    Reads up to 16 bytes from disk to identify the format. Pure magic-
    byte detection is the fast path; if it returns ``.zip`` we look at
    the filename hint (or the on-disk extension) to refine to
    ``.docx`` / ``.xlsx`` / ``.pptx`` / ... When nothing matches we
    fall back to the extension verbatim, or ``.txt`` when the file
    carries no extension at all.

    Defensive: any OS error during the small read collapses to
    extension-only detection. We never raise from this helper - the
    caller's whole pipeline (file_store → rag.ingest_file) depends on
    it being infallible.
    """
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
    """Extract a lowercase ``.ext`` from a free-form filename string.

    Returns ``""`` when the hint carries no suffix or is empty.
    """
    if not filename_hint:
        return ""
    return Path(filename_hint).suffix.lower()
