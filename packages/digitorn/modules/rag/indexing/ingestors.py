"""Format-specific document ingestors.

Each ingestor reads a source format and produces normalized documents
ready for chunking and embedding. Auto-detected by file extension.
"""

from __future__ import annotations

import csv
import io
import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class IngestDocument:
    text: str
    doc_id: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


class Ingestor(Protocol):
    def ingest(self, path: Path, **kwargs: Any) -> list[IngestDocument]: ...


class PlainTextIngestor:
    def ingest(self, path: Path, **kwargs: Any) -> list[IngestDocument]:
        text = path.read_text(encoding="utf-8", errors="replace")
        if not text.strip():
            return []
        return [IngestDocument(
            text=text,
            doc_id=str(path),
            metadata={"source_type": "file", "source_id": str(path), "format": "text"},
        )]


class MarkdownIngestor:
    """Splits markdown by headers, preserving section hierarchy."""

    _HEADER_RE = re.compile(r"^(#{1,6})\s+(.+)$", re.MULTILINE)

    def ingest(self, path: Path, **kwargs: Any) -> list[IngestDocument]:
        text = path.read_text(encoding="utf-8", errors="replace")
        if not text.strip():
            return []

        sections = self._split_by_headers(text)
        if not sections:
            return [IngestDocument(
                text=text, doc_id=str(path),
                metadata={"source_type": "file", "source_id": str(path), "format": "markdown"},
            )]

        docs = []
        for i, (header, body) in enumerate(sections):
            combined = f"{header}\n{body}" if header else body
            if not combined.strip():
                continue
            docs.append(IngestDocument(
                text=combined,
                doc_id=f"{path}:section:{i}",
                metadata={
                    "source_type": "file", "source_id": str(path),
                    "format": "markdown", "section": header.strip("# ").strip(),
                    "section_index": i,
                },
            ))
        return docs

    def _split_by_headers(self, text: str) -> list[tuple[str, str]]:
        matches = list(self._HEADER_RE.finditer(text))
        if not matches:
            return [("", text)]

        sections = []
        if matches[0].start() > 0:
            sections.append(("", text[:matches[0].start()]))

        for i, m in enumerate(matches):
            end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
            sections.append((m.group(0), text[m.end():end]))

        return sections


class CodeIngestor:
    """Treats code files as single documents with language metadata."""

    _LANG_MAP = {
        ".py": "python", ".js": "javascript", ".ts": "typescript",
        ".go": "go", ".rs": "rust", ".java": "java", ".rb": "ruby",
        ".php": "php", ".c": "c", ".cpp": "cpp", ".cs": "csharp",
        ".swift": "swift", ".kt": "kotlin", ".sh": "bash",
    }

    def ingest(self, path: Path, **kwargs: Any) -> list[IngestDocument]:
        text = path.read_text(encoding="utf-8", errors="replace")
        if not text.strip():
            return []
        lang = self._LANG_MAP.get(path.suffix.lower(), "unknown")
        return [IngestDocument(
            text=text,
            doc_id=str(path),
            metadata={
                "source_type": "file", "source_id": str(path),
                "format": "code", "language": lang,
            },
        )]


class CSVIngestor:
    """Each row becomes a document, or groups of rows if large."""

    def ingest(
        self, path: Path, *, max_rows: int = 10000, **kwargs: Any,
    ) -> list[IngestDocument]:
        text = path.read_text(encoding="utf-8", errors="replace")
        reader = csv.DictReader(io.StringIO(text))
        docs = []
        for i, row in enumerate(reader):
            if i >= max_rows:
                break
            row_text = " | ".join(f"{k}: {v}" for k, v in row.items() if v)
            if not row_text.strip():
                continue
            docs.append(IngestDocument(
                text=row_text,
                doc_id=f"{path}:row:{i}",
                metadata={
                    "source_type": "file", "source_id": str(path),
                    "format": "csv", "row_index": i,
                },
            ))
        return docs


class JSONIngestor:
    """Flattens JSON objects into searchable text documents."""

    def ingest(self, path: Path, **kwargs: Any) -> list[IngestDocument]:
        text = path.read_text(encoding="utf-8", errors="replace")
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            return [IngestDocument(
                text=text, doc_id=str(path),
                metadata={"source_type": "file", "source_id": str(path), "format": "json"},
            )]

        if isinstance(data, list):
            return self._ingest_array(path, data)
        return [IngestDocument(
            text=json.dumps(data, indent=2, ensure_ascii=False),
            doc_id=str(path),
            metadata={"source_type": "file", "source_id": str(path), "format": "json"},
        )]

    def _ingest_array(self, path: Path, items: list) -> list[IngestDocument]:
        docs = []
        for i, item in enumerate(items[:10000]):
            text = json.dumps(item, indent=2, ensure_ascii=False) if isinstance(item, dict) else str(item)
            docs.append(IngestDocument(
                text=text,
                doc_id=f"{path}:item:{i}",
                metadata={
                    "source_type": "file", "source_id": str(path),
                    "format": "json", "item_index": i,
                },
            ))
        return docs


class JSONLIngestor:
    def ingest(self, path: Path, **kwargs: Any) -> list[IngestDocument]:
        docs = []
        with open(path, encoding="utf-8", errors="replace") as f:
            for i, line in enumerate(f):
                line = line.strip()
                if not line:
                    continue
                if i >= 10000:
                    break
                docs.append(IngestDocument(
                    text=line,
                    doc_id=f"{path}:line:{i}",
                    metadata={
                        "source_type": "file", "source_id": str(path),
                        "format": "jsonl", "line_index": i,
                    },
                ))
        return docs


class HTMLIngestor:
    """Extracts text from HTML, strips tags."""

    _TAG_RE = re.compile(r"<[^>]+>")
    _WS_RE = re.compile(r"\s+")

    def ingest(self, path: Path, **kwargs: Any) -> list[IngestDocument]:
        raw = path.read_text(encoding="utf-8", errors="replace")
        text = self._TAG_RE.sub(" ", raw)
        text = self._WS_RE.sub(" ", text).strip()
        if not text:
            return []
        return [IngestDocument(
            text=text,
            doc_id=str(path),
            metadata={"source_type": "file", "source_id": str(path), "format": "html"},
        )]


def _extract_pdf_pages(path: Path) -> list[tuple[int, str]]:
    """Extract per-page text from a PDF using pymupdf.

    Returns a list of (page_number_1based, text) tuples. Empty list on
    failure or when pymupdf is not installed. The pdf module used to
    own this code, but it was the only consumer and the wrapper added
    no value, so it was inlined here when the pdf module was removed.
    """
    try:
        import pymupdf
    except ImportError:
        logger.warning(
            "pymupdf not installed — PDF ingestion disabled. "
            "Install with: pip install pymupdf",
        )
        return []
    try:
        doc = pymupdf.open(str(path))
        pages = [(idx + 1, doc[idx].get_text("text")) for idx in range(len(doc))]
        doc.close()
        return pages
    except Exception as exc:
        logger.warning("PDF read failed for %s: %s", path, exc)
        return []


class PDFIngestor:
    """Read a PDF file and produce one IngestDocument per page."""

    def ingest(self, path: Path, **kwargs: Any) -> list[IngestDocument]:
        # Sync entry point — used by the synchronous IndexingEngine path.
        # The async path runs the same extraction in a thread.
        return self._build_docs(path, _extract_pdf_pages(path))

    async def ingest_async(self, path: Path, bus: Any = None) -> list[IngestDocument]:
        # ``bus`` is accepted for API compatibility with sibling ingestors
        # that still delegate over ServiceBus; PDFIngestor reads directly.
        import asyncio
        pages = await asyncio.to_thread(_extract_pdf_pages, path)
        return self._build_docs(path, pages)

    def _build_docs(
        self, path: Path, pages: list[tuple[int, str]],
    ) -> list[IngestDocument]:
        docs: list[IngestDocument] = []
        for page_num, text in pages:
            text = text.strip()
            if not text:
                continue
            docs.append(IngestDocument(
                text=text,
                doc_id=f"{path}:page:{page_num}",
                metadata={
                    "source_type": "file",
                    "source_id": str(path),
                    "format": "pdf",
                    "page": page_num,
                },
            ))
        return docs


def _extract_spreadsheet_sheets(
    path: Path, max_rows: int = 10000,
) -> list[tuple[str, list[Any], list[list[Any]]]]:
    """Extract (sheet_name, headers, rows) tuples from a spreadsheet.

    Supports .csv (stdlib csv) and .xlsx (openpyxl, optional). Returns
    an empty list on unsupported formats or missing libraries. The
    spreadsheet module used to own this code, but it was the only
    consumer and was inlined when the module was removed.
    """
    suffix = path.suffix.lower()
    if suffix == ".csv":
        import csv
        try:
            with path.open(encoding="utf-8", newline="") as f:
                reader = csv.reader(f)
                rows = list(reader)
            if not rows:
                return []
            headers = rows[0]
            return [("Sheet1", headers, rows[1:max_rows + 1])]
        except Exception as exc:
            logger.warning("CSV read failed for %s: %s", path, exc)
            return []
    if suffix in (".xlsx", ".xlsm"):
        try:
            import openpyxl
        except ImportError:
            logger.warning(
                "openpyxl not installed — XLSX ingestion disabled. "
                "Install with: pip install openpyxl",
            )
            return []
        try:
            wb = openpyxl.load_workbook(str(path), data_only=True, read_only=True)
            out: list[tuple[str, list[Any], list[list[Any]]]] = []
            for sheet_name in wb.sheetnames:
                ws = wb[sheet_name]
                rows_iter = ws.iter_rows(values_only=True)
                first = next(rows_iter, None)
                if first is None:
                    continue
                headers = [c if c is not None else "" for c in first]
                rows: list[list[Any]] = []
                for i, row in enumerate(rows_iter):
                    if i >= max_rows:
                        break
                    rows.append(list(row))
                out.append((sheet_name, headers, rows))
            wb.close()
            return out
        except Exception as exc:
            logger.warning("XLSX read failed for %s: %s", path, exc)
            return []
    return []


class SpreadsheetIngestor:
    """Read a spreadsheet (.csv / .xlsx) and produce one IngestDocument per row."""

    async def ingest_async(self, path: Path, bus: Any = None) -> list[IngestDocument]:
        import asyncio
        sheets = await asyncio.to_thread(_extract_spreadsheet_sheets, path)
        docs: list[IngestDocument] = []
        for sheet_name, headers, rows in sheets:
            for i, row in enumerate(rows):
                if headers:
                    text = " | ".join(
                        f"{h}: {row[j]}"
                        for j, h in enumerate(headers)
                        if j < len(row) and row[j]
                    )
                else:
                    text = " | ".join(str(v) for v in row if v)
                if not text.strip():
                    continue
                docs.append(IngestDocument(
                    text=text,
                    doc_id=f"{path}:{sheet_name}:row:{i}",
                    metadata={
                        "source_type": "file",
                        "source_id": str(path),
                        "format": "spreadsheet",
                        "sheet": sheet_name,
                        "row_index": i,
                    },
                ))
        return docs


# ── Registry ──────────────────────────────────────────────────────────

_SYNC_INGESTORS: dict[str, Ingestor] = {
    ".md": MarkdownIngestor(),
    ".markdown": MarkdownIngestor(),
    ".txt": PlainTextIngestor(),
    ".rst": PlainTextIngestor(),
    ".log": PlainTextIngestor(),
    ".py": CodeIngestor(),
    ".js": CodeIngestor(),
    ".ts": CodeIngestor(),
    ".tsx": CodeIngestor(),
    ".jsx": CodeIngestor(),
    ".go": CodeIngestor(),
    ".rs": CodeIngestor(),
    ".java": CodeIngestor(),
    ".rb": CodeIngestor(),
    ".php": CodeIngestor(),
    ".c": CodeIngestor(),
    ".cpp": CodeIngestor(),
    ".h": CodeIngestor(),
    ".cs": CodeIngestor(),
    ".swift": CodeIngestor(),
    ".kt": CodeIngestor(),
    ".sh": CodeIngestor(),
    ".bash": CodeIngestor(),
    ".yaml": PlainTextIngestor(),
    ".yml": PlainTextIngestor(),
    ".toml": PlainTextIngestor(),
    ".ini": PlainTextIngestor(),
    ".cfg": PlainTextIngestor(),
    ".conf": PlainTextIngestor(),
    ".csv": CSVIngestor(),
    ".tsv": CSVIngestor(),
    ".json": JSONIngestor(),
    ".jsonl": JSONLIngestor(),
    ".ndjson": JSONLIngestor(),
    ".html": HTMLIngestor(),
    ".htm": HTMLIngestor(),
    ".xml": PlainTextIngestor(),
    ".sql": CodeIngestor(),
}

_ASYNC_EXTENSIONS = {".pdf", ".xlsx", ".xls"}


def get_ingestor(ext: str) -> Ingestor | None:
    return _SYNC_INGESTORS.get(ext.lower())


def is_async_format(ext: str) -> bool:
    return ext.lower() in _ASYNC_EXTENSIONS


def supported_extensions() -> set[str]:
    return set(_SYNC_INGESTORS.keys()) | _ASYNC_EXTENSIONS
