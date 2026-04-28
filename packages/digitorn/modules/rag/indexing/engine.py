"""IndexingEngine - orchestrates auto-indexing from files and databases."""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path
from typing import Any

from .ingestors import (
    IngestDocument,
    PDFIngestor,
    SpreadsheetIngestor,
    get_ingestor,
    is_async_format,
)
from .sync import ChangeRecord, SyncStrategy, create_sync

logger = logging.getLogger(__name__)


class IndexingEngine:
    """Manages source scanning, incremental indexing, and watcher integration."""

    def __init__(self, service_bus: Any = None) -> None:
        self._bus = service_bus
        self._content_hashes: dict[str, str] = {}
        self._db_syncs: dict[str, dict[str, SyncStrategy]] = {}
        self._pdf_ingestor = PDFIngestor()
        self._xlsx_ingestor = SpreadsheetIngestor()

    async def scan_directory(
        self,
        directory: str,
        extensions: list[str],
        *,
        recursive: bool = True,
        max_files: int = 1000,
    ) -> list[IngestDocument]:
        root = Path(directory).resolve()
        if not root.is_dir():
            logger.warning("Directory not found: %s", directory)
            return []

        pattern = "**/*" if recursive else "*"
        ext_set = {e.lower() if e.startswith(".") else f".{e.lower()}" for e in extensions}

        files: list[Path] = []
        for p in root.glob(pattern):
            if not p.is_file():
                continue
            if p.suffix.lower() not in ext_set:
                continue
            if p.name.startswith("."):
                continue
            files.append(p)
            if len(files) >= max_files:
                break

        files.sort(key=lambda p: p.name)
        all_docs: list[IngestDocument] = []

        for fp in files:
            content_hash = self._hash_file(fp)
            key = str(fp)
            if self._content_hashes.get(key) == content_hash:
                continue
            self._content_hashes[key] = content_hash

            docs = await self._ingest_file(fp)
            all_docs.extend(docs)

        logger.info(
            "Scanned %s: %d files, %d documents (incremental)",
            directory, len(files), len(all_docs),
        )
        return all_docs

    async def _ingest_file(self, path: Path) -> list[IngestDocument]:
        ext = path.suffix.lower()

        if is_async_format(ext) and self._bus:
            if ext == ".pdf":
                return await self._pdf_ingestor.ingest_async(path, self._bus)
            if ext in (".xlsx", ".xls"):
                return await self._xlsx_ingestor.ingest_async(path, self._bus)

        ingestor = get_ingestor(ext)
        if ingestor is None:
            logger.debug("No ingestor for extension %s, skipping %s", ext, path)
            return []

        try:
            return ingestor.ingest(path)
        except Exception as exc:
            logger.warning("Ingestor failed for %s: %s", path, exc)
            return []

    async def ingest_single_file(self, path: str | Path) -> list[IngestDocument]:
        fp = Path(path).resolve()
        if not fp.is_file():
            return []
        return await self._ingest_file(fp)

    # ── Database indexing ─────────────────────────────────────────────

    async def index_database_schema(
        self,
        connection_id: str,
        tables: dict[str, Any],
    ) -> list[IngestDocument]:
        if not self._bus:
            return []

        docs: list[IngestDocument] = []
        for table_name, table_cfg in tables.items():
            try:
                result = await self._bus.call("database", "describe", {
                    "connection_id": connection_id,
                    "table": table_name,
                    "sample_limit": 5,
                })
                if not result or not result.success or not result.data:
                    continue

                data = result.data
                text = self._format_table_description(table_name, data)
                docs.append(IngestDocument(
                    text=text,
                    doc_id=f"db:{connection_id}:{table_name}:schema",
                    metadata={
                        "source_type": "database_schema",
                        "source_id": f"{connection_id}:{table_name}",
                        "connection_id": connection_id,
                        "table": table_name,
                        "row_count": data.get("row_count", 0),
                    },
                ))
            except Exception as exc:
                logger.warning("Schema index failed for %s.%s: %s", connection_id, table_name, exc)

        return docs

    async def index_database_rows(
        self,
        connection_id: str,
        table_name: str,
        columns: list[str],
        *,
        template: str = "",
        max_rows: int = 50000,
    ) -> list[IngestDocument]:
        if not self._bus or not columns:
            return []

        col_list = ", ".join(columns)
        query = f"SELECT {col_list} FROM {table_name} LIMIT {max_rows}"

        try:
            result = await self._bus.call("database", "fetch_results", {
                "connection_id": connection_id,
                "query": query,
            })
        except Exception as exc:
            logger.warning("Row index failed for %s.%s: %s", connection_id, table_name, exc)
            return []

        if not result or not result.success or not result.data:
            return []

        rows = result.data.get("rows", [])
        docs: list[IngestDocument] = []

        for i, row in enumerate(rows):
            if template:
                try:
                    text = template.format(**row)
                except (KeyError, IndexError):
                    text = " | ".join(f"{k}: {v}" for k, v in row.items() if v)
            else:
                text = " | ".join(f"{k}: {v}" for k, v in row.items() if v)

            if not text.strip():
                continue

            row_id = str(row.get("id", row.get(columns[0], i)))
            docs.append(IngestDocument(
                text=text,
                doc_id=f"db:{connection_id}:{table_name}:row:{row_id}",
                metadata={
                    "source_type": "database",
                    "source_id": f"{connection_id}:{table_name}",
                    "connection_id": connection_id,
                    "table": table_name,
                    "row_id": row_id,
                },
            ))

        logger.info(
            "Indexed %d rows from %s.%s", len(docs), connection_id, table_name,
        )
        return docs

    # ── DB sync polling ───────────────────────────────────────────────

    async def setup_db_sync(
        self,
        connection_id: str,
        table_name: str,
        strategy: str,
        *,
        auto_triggers: bool = True,
    ) -> SyncStrategy:
        key = f"{connection_id}:{table_name}"
        if connection_id not in self._db_syncs:
            self._db_syncs[connection_id] = {}

        sync = create_sync(strategy)

        if strategy == "changelog" and auto_triggers and self._bus:
            from .sync import ChangelogSync
            if isinstance(sync, ChangelogSync):
                await sync.setup_triggers(self._bus, connection_id, [table_name])

        self._db_syncs[connection_id][table_name] = sync
        return sync

    async def poll_db_changes(
        self,
        connection_id: str,
        table_name: str,
        columns: list[str],
    ) -> list[ChangeRecord]:
        syncs = self._db_syncs.get(connection_id, {})
        sync = syncs.get(table_name)
        if sync is None or self._bus is None:
            return []
        return await sync.poll(self._bus, connection_id, table_name, columns)

    # ── Watcher event handling ────────────────────────────────────────

    async def on_file_changed(
        self, path: str, change_type: str,
    ) -> list[IngestDocument]:
        fp = Path(path)

        if change_type in ("deleted", "moved"):
            self._content_hashes.pop(str(fp), None)
            return []

        if not fp.is_file():
            return []

        new_hash = self._hash_file(fp)
        if self._content_hashes.get(str(fp)) == new_hash:
            return []

        self._content_hashes[str(fp)] = new_hash
        return await self._ingest_file(fp)

    # ── State ─────────────────────────────────────────────────────────

    def state_snapshot(self) -> dict[str, Any]:
        db_syncs = {}
        for conn_id, tables in self._db_syncs.items():
            db_syncs[conn_id] = {t: s.to_dict() for t, s in tables.items()}
        return {
            "content_hashes": dict(self._content_hashes),
            "db_syncs": db_syncs,
        }

    def restore_state(self, state: dict[str, Any]) -> None:
        self._content_hashes = state.get("content_hashes", {})
        for conn_id, tables in state.get("db_syncs", {}).items():
            self._db_syncs[conn_id] = {}
            for table, sync_data in tables.items():
                sync = create_sync(sync_data.get("strategy", "updated_at"))
                sync.restore(sync_data)
                self._db_syncs[conn_id][table] = sync

    # ── Internals ─────────────────────────────────────────────────────

    @staticmethod
    def _hash_file(path: Path) -> str:
        try:
            content = path.read_bytes()
            return hashlib.md5(content).hexdigest()[:16]
        except Exception:
            return ""

    @staticmethod
    def _format_table_description(name: str, data: dict[str, Any]) -> str:
        lines = [f"Table: {name}"]

        desc = data.get("description") or data.get("comment", "")
        if desc:
            lines.append(f"Description: {desc}")

        row_count = data.get("row_count", 0)
        if row_count:
            lines.append(f"Row count: {row_count}")

        columns = data.get("columns", [])
        if columns:
            col_lines = []
            for col in columns:
                col_name = col.get("name", "?")
                col_type = col.get("type", "?")
                nullable = "NULL" if col.get("nullable") else "NOT NULL"
                col_desc = col.get("description", "")
                entry = f"  - {col_name} ({col_type}, {nullable})"
                if col_desc:
                    entry += f" - {col_desc}"
                col_lines.append(entry)
            lines.append("Columns:\n" + "\n".join(col_lines))

        fks = data.get("foreign_keys", [])
        if fks:
            fk_lines = []
            for fk in fks:
                fk_lines.append(
                    f"  - {fk.get('column', '?')} -> {fk.get('referred_table', '?')}.{fk.get('referred_column', '?')}"
                )
            lines.append("Foreign keys:\n" + "\n".join(fk_lines))

        samples = data.get("sample_rows", data.get("samples", []))
        if samples:
            lines.append(f"Sample rows ({len(samples)}):")
            for row in samples[:3]:
                lines.append(f"  {row}")

        annotations = data.get("annotations", {})
        if annotations:
            ann_desc = annotations.get("_description", "")
            if ann_desc:
                lines.append(f"Business context: {ann_desc}")
            tags = annotations.get("tags", [])
            if tags:
                lines.append(f"Tags: {', '.join(tags)}")

        return "\n".join(lines)
