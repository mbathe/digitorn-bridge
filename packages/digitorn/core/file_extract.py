"""Module-agnostic text extraction for chat attachments.

The chat attachments pipeline needs to pull TEXT out of binary files
(PDF, DOCX, PPTX, ODT, ODS, RTF, XLSX) without depending on the
``rag`` module being loaded by the app. Pure-chat apps don't need
retrieval - they just need to put the document's text in front of
the agent (direct mode) or under ``attachments/`` for WsRead (tool
mode).

This helper reuses the same per-format ingestors that ``rag``
already implements, but exposes them through a single async
function that:

  * dispatches by canonical extension (sniffed magic bytes preferred)
  * off-loads the parse to a worker thread so the event loop stays
    responsive
  * returns the combined text + a uniform error path

No embedding, no chunking, no indexing. Just extraction.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


async def extract_text(
    path: str | Path,
    *,
    format_hint: str = "",
) -> str:
    """Return the plain-text content of ``path``.

    ``format_hint`` is the canonical suffix (``".pdf"``, ``".docx"``,
    …) typically obtained from :func:`sniff_format` at upload time.
    When empty, the file's on-disk extension is used. Unknown
    extensions fall through to a UTF-8-with-replace read so even
    unknown text formats yield something usable.

    Returns an empty string when the file can't be opened or the
    parser produced no text. Never raises - callers should treat
    empty as "no extractable content" and surface that to the user
    via the status flow.
    """
    fp = Path(path)
    if not fp.exists() or not fp.is_file():
        return ""

    ext = (format_hint or fp.suffix or ".txt").lower()

    # Imports lazy + local so this helper stays available even when
    # the rag module isn't installed. The ingestors themselves are
    # pure (no rag dependency beyond the file location).
    from digitorn.modules.rag.indexing.ingestors import (
        IngestDocument as _IngDoc,
        PDFIngestor,
        SpreadsheetIngestor,
        get_ingestor,
        is_async_format,
    )

    try:
        if is_async_format(ext):
            if ext == ".pdf":
                docs = await PDFIngestor().ingest_async(fp)
            elif ext in (".xlsx", ".xls"):
                docs = await SpreadsheetIngestor().ingest_async(fp)
            else:
                docs = []
        else:
            ingestor = get_ingestor(ext)
            if ingestor is not None:
                docs = await asyncio.to_thread(ingestor.ingest, fp)
            else:
                text = await asyncio.to_thread(
                    fp.read_text, encoding="utf-8", errors="replace",
                )
                docs = [_IngDoc(
                    text=text, doc_id=str(fp),
                    metadata={"source_type": "file"},
                )]
    except Exception as exc:
        logger.warning(
            "file_extract_failed path=%s ext=%s err=%s",
            fp.name, ext, exc,
        )
        return ""

    parts = [d.text for d in docs if getattr(d, "text", "")]
    combined = "\n\n".join(p for p in parts if p.strip())
    return combined
