"""Index module - unified knowledge index for all Digitorn modules."""

from __future__ import annotations

import asyncio
import logging
import os
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

from digitorn.modules.base import ActionResult, BaseModule, Platform
from digitorn.modules.decorators import action
from digitorn.modules.manifest import ConstraintSpec, ModuleManifest

from .extractor import ExtractorRegistry

#
# `Extractor.extract` runs pure-Python CPU work: `content.encode()`
# on huge strings (UTF-8 conversion holds the GIL fully on big inputs),
# `hashlib.sha256(bytes)` (releases the GIL only inside the C
# digest call, not during the encode), and for PythonExtractor an
# `ast.parse` + `ast.walk` which is ALL pure Python and holds the
# GIL for the entire duration.
#
# `asyncio.to_thread` does NOT help here: a thread doing pure-Python
# work holds the GIL, which means the daemon's main asyncio loop
# cannot run Python callbacks in parallel -- visible as a 5-40s main
# loop stall every time a large source file lands in the indexer.
#
# A `ProcessPoolExecutor` does help: each subprocess has its own
# Python interpreter with its own GIL, completely independent from
# the daemon's main loop. We pay an IPC cost (pickle args + result)
# but the main loop stays responsive throughout.
#
# Lazy init -- on Windows `spawn` mode costs ~1s per worker, so
# defer until the first index scan instead of taxing daemon startup.
_EXTRACT_POOL: ProcessPoolExecutor | None = None
_EXTRACT_POOL_LOCK = asyncio.Lock()

def _get_extract_pool() -> ProcessPoolExecutor:
    global _EXTRACT_POOL
    if _EXTRACT_POOL is None:
        max_workers = max(1, (os.cpu_count() or 4) - 2)
        _EXTRACT_POOL = ProcessPoolExecutor(max_workers=max_workers)
        logger.info(
            "index_extract_pool_started workers=%d", max_workers,
        )
    return _EXTRACT_POOL

def _shutdown_extract_pool() -> None:
    global _EXTRACT_POOL
    if _EXTRACT_POOL is not None:
        _EXTRACT_POOL.shutdown(wait=False, cancel_futures=True)
        _EXTRACT_POOL = None

def _extract_in_subprocess(
    extractor_module: str,
    extractor_class: str,
    source_id: str,
    file_path: str,
    content: str,
    metadata: dict,
) -> tuple[list, list]:
    import importlib
    mod = importlib.import_module(extractor_module)
    cls = getattr(mod, extractor_class)
    inst = cls()
    return inst.extract(source_id, file_path, content, metadata)

class IndexConfig(BaseModel):
    """Pydantic config for the index module (validated at compile time)."""

    model_config = {"extra": "forbid"}

    workspace: str = Field(default="", description="Auto-injected by the daemon.")
from .params import (
    ContextParams,
    InvalidateParams,
    QueryParams,
    RegisterExtractorParams,
    RegisterSourceParams,
    RelationsParams,
    ScanParams,
)
from .store import IndexStore
from .types import Source

_CHARS_PER_TOKEN = 3.5

class IndexModule(BaseModule):
    MODULE_ID = "index"
    VERSION = "1.0.0"
    SUPPORTED_PLATFORMS = [Platform.ALL]
    MODULE_TYPE = "system"
    CONFIG_MODEL = IndexConfig

    def __init__(self) -> None:
        super().__init__()
        self.store = IndexStore()
        self.extractors = ExtractorRegistry()
        import asyncio as _asyncio
        self._write_lock: _asyncio.Lock = _asyncio.Lock()

    async def on_start(self) -> None:
        pass

    async def on_event(self, topic: str, event: dict[str, Any]) -> None:
        """React to events from other modules and the watcher system."""
        data = event.get("data", {})

        if topic.startswith("digitorn.watcher."):
            change_type = data.get("change_type", "")
            path = data.get("path")
            if not path:
                return

            async with self._write_lock:
                if change_type in ("created", "modified"):
                    self.store.invalidate_by_path(path)
                    await self._auto_reindex_path(path)
                elif change_type == "deleted":
                    self.store.invalidate_by_path(path)
                elif change_type == "moved":
                    self.store.invalidate_by_path(path)
                    old_path = data.get("old_path")
                    if old_path:
                        self.store.invalidate_by_path(old_path)
            return

        action_name = data.get("action", "")
        success = data.get("success", False)
        params = data.get("params", {})

        if not success:
            return

        reindex_actions = {"write", "edit"}
        remove_actions = {"rm", "mv"}

        path = params.get("path")
        if not path:
            return

        async with self._write_lock:
            if action_name in reindex_actions:
                self.store.invalidate_by_path(path)
                await self._auto_reindex_path(path)

            elif action_name in remove_actions:
                count = self.store.invalidate_by_path(path)
                dest = params.get("destination") or params.get("dest")
                if dest:
                    self.store.invalidate_by_path(dest)

    async def _auto_reindex_path(self, path: str) -> None:
        for source in self.store.list_sources():
            if not path.startswith(source.root):
                continue

            if source.module_id == "filesystem":
                await self._reindex_filesystem_path(source, path)
            else:
                self.store.invalidate_by_path(path)

    async def _reindex_filesystem_path(self, source: "Source", path: str) -> None:
        file_path = Path(path)
        if not file_path.exists() or not file_path.is_file():
            return

        try:
            content = file_path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            return

        extractor = self.extractors.resolve(
            source.extractor, path, source.metadata,
        )
        if not extractor:
            return

        entries, relations = extractor.extract(
            source.source_id, path, content, source.metadata,
        )

        for entry in entries:
            self.store.upsert(entry)
        for rel in relations:
            self.store.add_relation(rel)

        source.entry_count = len(
            self.store._by_source.get(source.source_id, set())
        )

    def _get_watcher_service(self) -> Any | None:
        ctx = getattr(self, "_context", None)
        if ctx:
            ws = getattr(ctx, "watcher_service", None)
            if ws:
                return ws
        return getattr(self, "_watcher_service", None)

    async def _start_watch(self, source: "Source") -> str:
        from digitorn.core.watcher.types import WatchConfig, WatchMode

        watcher = self._get_watcher_service()
        if not watcher:
            return "no_watcher_service"

        if source.module_id == "filesystem":
            backend = "filesystem"
        elif source.module_id in ("polling",):
            backend = "polling"
        else:
            backend = "service_bus"

        config = WatchConfig(
            source_id=source.source_id,
            backend=backend,
            root=source.root,
            patterns=[source.scan_pattern] if source.scan_pattern != "*" else ["**/*"],
            mode=WatchMode(source.watch_mode),
            app_id=source.app_id,
            metadata=source.metadata,
        )

        watch_kwargs: dict[str, Any] = {}
        if backend == "service_bus":
            ctx = getattr(self, "_context", None)
            service_bus = getattr(ctx, "service_bus", None) if ctx else None
            if service_bus:
                watch_kwargs["service_bus"] = service_bus
                watch_kwargs["module_id"] = source.module_id

        try:
            await watcher.watch(config, **watch_kwargs)
            return "active"
        except Exception:
            return "error"

    async def _stop_watch(self, source_id: str) -> None:
        watcher = self._get_watcher_service()
        if watcher:
            await watcher.unwatch(source_id)

    def state_snapshot(self) -> dict[str, Any]:
        """Persist the entire index + persistent watch configs for daemon restart."""
        snapshot = self.store.snapshot()

        persistent_watches = []
        for source in self.store.list_sources():
            if source.watch and source.watch_mode == "persistent":
                persistent_watches.append({
                    "source_id": source.source_id,
                    "module_id": source.module_id,
                    "root": source.root,
                    "scan_pattern": source.scan_pattern,
                    "metadata": source.metadata,
                })
        snapshot["persistent_watches"] = persistent_watches

        return snapshot

    async def restore_state(self, state: dict[str, Any]) -> None:
        """Restore index from a previous snapshot and restart persistent watches."""
        self.store.restore(state)

        persistent_watches = state.get("persistent_watches", [])
        for watch_info in persistent_watches:
            source = self.store.get_source(watch_info["source_id"])
            if source and source.watch:
                await self._start_watch(source)

    @action(
        description=(
            "Register a data source to be indexed. "
            "Sources are owned by a specific module (filesystem, database, etc.). "
            "After registration, call 'scan' to populate the index."
        ),
        params_model=RegisterSourceParams,
        permissions=["index:admin"],
        risk_level="low",
        tags=["config"],
    )
    async def register_source(self, params: RegisterSourceParams) -> ActionResult:
        source = Source(
            source_id=params.source_id,
            module_id=params.module_id,
            root=params.root,
            extractor=params.extractor,
            scan_pattern=params.scan_pattern,
            metadata=params.metadata,
            watch=params.watch,
            watch_mode=params.watch_mode,
        )
        self.store.add_source(source)

        watch_status = "disabled"
        if params.watch:
            watch_status = await self._start_watch(source)

        return ActionResult(
            success=True,
            data={
                "source_id": source.source_id,
                "module_id": source.module_id,
                "root": source.root,
                "extractor": source.extractor,
                "watch": params.watch,
                "watch_mode": params.watch_mode,
                "watch_status": watch_status,
                "message": f"Source '{source.source_id}' registered. Call scan to index it.",
            },
        )

    @action(
        description=(
            "Register a custom extractor provided by another module. "
            "The extractor will be called via the service bus during scan."
        ),
        params_model=RegisterExtractorParams,
        permissions=["index:admin"],
        risk_level="low",
        tags=["config"],
    )
    async def register_extractor(self, params: RegisterExtractorParams) -> ActionResult:
        self.extractors.register_remote(
            name=params.name,
            module_id=params.module_id,
            action=params.extract_action,
        )
        return ActionResult(
            success=True,
            data={
                "name": params.name,
                "module_id": params.module_id,
                "action": params.extract_action,
                "extractors_available": self.extractors.list_extractors(),
            },
        )

    @action(
        description=(
            "Scan a registered source and update the index. "
            "Incremental by default - only processes changed content. "
            "Use force=true for a full rescan."
        ),
        params_model=ScanParams,
        permissions=["index:write"],
        risk_level="low",
        streams_progress=True,
        tags=["index", "scan"],
    )
    async def scan(self, params: ScanParams) -> ActionResult:
        source = self.store.get_source(params.source_id)
        if not source:
            return ActionResult(
                success=False,
                error=f"Source '{params.source_id}' not found. Register it first.",
            )

        remote = self.extractors.get_remote(source.extractor)
        if remote:
            return await self._scan_remote(source, remote, params.force)

        return await self._scan_local(source, params.force)

    async def _scan_local(self, source: Source, force: bool) -> ActionResult:
        extractor = self.extractors.resolve(source.extractor, source.root, source.metadata)
        if not extractor:
            return ActionResult(
                success=False,
                error=f"No extractor found for '{source.extractor}'.",
            )

        ctx = getattr(self, "_context", None)
        service_bus = getattr(ctx, "service_bus", None) if ctx else None

        if source.module_id == "filesystem" and service_bus:
            try:
                files = await self._list_files_via_bus(service_bus, source)
            except Exception as exc:
                logger.debug(
                    "index_bus_fallback source=%s error=%s",
                    source.source_id, exc,
                )
                # rglob is sync IO -- off-load to a thread so the
                # asyncio loop never stalls during a fallback scan.
                files = await asyncio.to_thread(
                    self._list_files_direct, source,
                )
        else:
            files = await asyncio.to_thread(
                self._list_files_direct, source,
            )

        if not files:
            return ActionResult(
                success=True,
                data={"source_id": source.source_id, "scanned": 0, "message": "No files found."},
            )

        added = 0
        updated = 0
        unchanged = 0
        errors = 0

        for i, file_path in enumerate(files):
            try:
                if service_bus and source.module_id == "filesystem":
                    result = await service_bus.call(
                        "filesystem", "read", {"path": file_path},
                    )
                    if hasattr(result, "success") and not result.success:
                        errors += 1
                        continue
                    data = result.data if hasattr(result, "data") else result
                    content = data.get("content", "") if isinstance(data, dict) else str(data)
                    content = _strip_line_numbers(content)
                else:
                    # Off-load `read_text` so thousands of files at
                    # daemon startup don't stall the loop.
                    content = await asyncio.to_thread(
                        Path(file_path).read_text,
                        encoding="utf-8",
                        errors="replace",
                    )

                file_extractor = self.extractors.resolve(
                    source.extractor, file_path, source.metadata,
                )
                if not file_extractor:
                    file_extractor = extractor

                # Route CPU-bound parsing to a ProcessPoolExecutor.
                loop = asyncio.get_running_loop()
                _ex_mod = type(file_extractor).__module__
                _ex_cls = type(file_extractor).__name__
                entries, relations = await loop.run_in_executor(
                    _get_extract_pool(),
                    _extract_in_subprocess,
                    _ex_mod, _ex_cls,
                    source.source_id, file_path, content, source.metadata,
                )

                for entry in entries:
                    changed = self.store.upsert(entry)
                    if changed:
                        if entry.content_hash:
                            updated += 1
                        else:
                            added += 1
                    else:
                        unchanged += 1

                for rel in relations:
                    self.store.add_relation(rel)

            except Exception:
                errors += 1

            if self.stream and (i + 1) % 10 == 0:
                await self.stream.progress(i + 1, len(files), f"Scanned {i + 1}/{len(files)}")

        source.last_scan = time.time()
        source.entry_count = len(self.store._by_source.get(source.source_id, set()))

        return ActionResult(
            success=True,
            data={
                "source_id": source.source_id,
                "files_scanned": len(files),
                "added": added,
                "updated": updated,
                "unchanged": unchanged,
                "errors": errors,
                "total_entries": source.entry_count,
            },
        )

    async def _scan_remote(
        self, source: Source, remote: dict[str, str], force: bool,
    ) -> ActionResult:
        ctx = getattr(self, "_context", None)
        service_bus = getattr(ctx, "service_bus", None) if ctx else None
        if not service_bus:
            return ActionResult(
                success=False,
                error="Service bus not available. Cannot call remote extractor.",
            )

        result = await service_bus.call(
            remote["module_id"],
            remote["action"],
            {"source_id": source.source_id, "root": source.root, "force": force},
        )

        data = result.data if hasattr(result, "data") else result
        if not isinstance(data, dict):
            return ActionResult(success=False, error="Remote extractor returned invalid data.")

        from .types import IndexEntry, Relation

        added = 0
        for entry_dict in data.get("entries", []):
            entry = IndexEntry(**entry_dict)
            if self.store.upsert(entry):
                added += 1

        for rel_dict in data.get("relations", []):
            self.store.add_relation(Relation(**rel_dict))

        source.last_scan = time.time()
        source.entry_count = len(self.store._by_source.get(source.source_id, set()))

        return ActionResult(
            success=True,
            data={
                "source_id": source.source_id,
                "entries_added": added,
                "total_entries": source.entry_count,
            },
        )

    async def _list_files_via_bus(
        self, service_bus: Any, source: Source,
    ) -> list[str]:
        result = await service_bus.call(
            "filesystem", "find",
            {"path": source.root, "pattern": source.scan_pattern, "max_results": 5000},
        )
        data = result.data if hasattr(result, "data") else result
        if isinstance(data, dict):
            return data.get("files", [])
        return []

    def _list_files_direct(self, source: Source) -> list[str]:
        root = Path(source.root)
        if not root.exists():
            return []
        files = []
        for p in root.rglob(source.scan_pattern):
            if p.is_file() and not any(
                part.startswith(".") for part in p.relative_to(root).parts
            ):
                files.append(str(p))
                if len(files) >= 5000:
                    break
        return files

    @action(
        description=(
            "Search the index for entries matching a query. "
            "Searches across names, signatures, and summaries. "
            "Returns entries sorted by relevance."
        ),
        params_model=QueryParams,
        permissions=["index:read"],
        risk_level="low",
        tags=["search"],
    )
    async def query(self, params: QueryParams) -> ActionResult:
        results = self.store.search(
            query=params.q,
            kind=params.kind,
            source_id=params.source_id,
            limit=params.limit,
        )
        return ActionResult(
            success=True,
            data={
                "results": [
                    {**entry.to_dict(), "score": round(score, 2)}
                    for entry, score in results
                ],
                "count": len(results),
                "query": params.q,
            },
        )

    @action(
        description=(
            "Explore the relation graph from an entry. "
            "Shows what an entry imports/calls/references and what references it."
        ),
        params_model=RelationsParams,
        permissions=["index:read"],
        risk_level="low",
        tags=["graph"],
    )
    async def relations(self, params: RelationsParams) -> ActionResult:
        entry = self.store.get(params.entry_id)
        if not entry:
            return ActionResult(
                success=False,
                error=f"Entry '{params.entry_id}' not found.",
            )

        entries, rels = self.store.get_relations(
            entry_id=params.entry_id,
            direction=params.direction,
            kind=params.kind,
            depth=params.depth,
        )

        return ActionResult(
            success=True,
            data={
                "target": entry.to_dict(),
                "related_entries": [e.to_dict() for e in entries],
                "relations": [r.to_dict() for r in rels],
                "count": len(entries),
            },
        )

    @action(
        description=(
            "Get optimal context for an LLM to work on a target. "
            "Returns the target's signature, location, and related entries "
            "(dependencies, callers), all trimmed to fit the token budget. "
            "This is the primary action for LLM agents - call this FIRST "
            "before reading or editing files."
        ),
        params_model=ContextParams,
        permissions=["index:read"],
        risk_level="low",
        tags=["context", "llm"],
    )
    async def context(self, params: ContextParams) -> ActionResult:
        target_entry = self._resolve_target(params.target)
        if not target_entry:
            return ActionResult(
                success=False,
                error=f"Target '{params.target}' not found in the index. Try scanning first.",
            )

        budget = params.token_budget
        result: dict[str, Any] = {
            "target": target_entry.to_dict(),
            "content": None,
            "dependencies": [],
            "callers": [],
            "tokens_used": 0,
            "token_budget": budget,
        }

        tokens_used = _estimate_tokens(str(target_entry.to_dict()))

        content = await self._fetch_content(target_entry)
        if content:
            content_tokens = _estimate_tokens(content)
            if tokens_used + content_tokens <= budget:
                result["content"] = content
                tokens_used += content_tokens

        if params.include_relations and tokens_used < budget * 0.8:
            entries, rels = self.store.get_relations(
                entry_id=target_entry.entry_id,
                direction="both",
                depth=params.depth,
            )

            deps = []
            callers = []
            for rel in rels:
                if rel.from_id == target_entry.entry_id:
                    related = self.store.get(rel.to_id)
                    if related:
                        deps.append((related, rel))
                else:
                    related = self.store.get(rel.from_id)
                    if related:
                        callers.append((related, rel))

            for related, rel in deps:
                entry_data = related.to_dict()
                entry_data["relation"] = rel.kind
                entry_tokens = _estimate_tokens(str(entry_data))
                if tokens_used + entry_tokens > budget:
                    break
                result["dependencies"].append(entry_data)
                tokens_used += entry_tokens

            for related, rel in callers:
                entry_data = related.to_dict()
                entry_data["relation"] = rel.kind
                entry_tokens = _estimate_tokens(str(entry_data))
                if tokens_used + entry_tokens > budget:
                    break
                result["callers"].append(entry_data)
                tokens_used += entry_tokens

        result["tokens_used"] = tokens_used
        result["entries_included"] = (
            1 + len(result["dependencies"]) + len(result["callers"])
        )

        return ActionResult(success=True, data=result)

    def _resolve_target(self, target: str) -> Any | None:
        entry = self.store.get(target)
        if entry:
            return entry

        entries = self.store.get_by_path(target)
        if entries:
            for e in entries:
                if e.kind != "import":
                    return e
            return entries[0]

        entries = self.store.get_by_name(target)
        if entries:
            for e in entries:
                if e.kind != "import":
                    return e
            return entries[0]

        results = self.store.search(target, limit=1)
        if results:
            return results[0][0]

        return None

    async def _fetch_content(self, entry: Any) -> str | None:
        ctx = getattr(self, "_context", None)
        service_bus = getattr(ctx, "service_bus", None) if ctx else None
        source = self.store.get_source(entry.source_id)

        if not source or not service_bus:
            try:
                path = Path(entry.path)
                if path.exists() and path.is_file():
                    content = path.read_text(encoding="utf-8", errors="replace")
                    if entry.line_start and entry.line_end:
                        lines = content.splitlines()
                        content = "\n".join(
                            lines[entry.line_start - 1: entry.line_end]
                        )
                    return content
            except Exception as exc:
                logger.debug("module best-effort block failed: %s", exc)
            return None

        if source.module_id == "filesystem":
            try:
                read_params: dict[str, Any] = {"path": entry.path}
                if entry.line_start and entry.line_end:
                    read_params["start_line"] = entry.line_start
                    read_params["end_line"] = entry.line_end
                result = await service_bus.call("filesystem", "read", read_params)
                data = result.data if hasattr(result, "data") else result
                if isinstance(data, dict):
                    return data.get("content", "")
            except Exception as exc:
                logger.debug("module best-effort block failed: %s", exc)

        return None

    @action(
        description=(
            "Invalidate (remove) entries from the index. "
            "Use source_id to clear an entire source, or path for a specific file."
        ),
        params_model=InvalidateParams,
        permissions=["index:write"],
        risk_level="low",
        tags=["index", "invalidate"],
    )
    async def invalidate(self, params: InvalidateParams) -> ActionResult:
        removed = 0
        if params.source_id:
            removed += self.store.invalidate_by_source(params.source_id)
        if params.path:
            removed += self.store.invalidate_by_path(params.path)

        if not params.source_id and not params.path:
            return ActionResult(
                success=False,
                error="Provide either source_id or path to invalidate.",
            )

        return ActionResult(
            success=True,
            data={"entries_removed": removed},
        )

    CONSTRAINTS = [
        ConstraintSpec(
            name="allowed_sources",
            type="string_list",
            description="Source IDs this application can access.",
            scope="universal",
        ),
        ConstraintSpec(
            name="max_entries",
            type="integer",
            description="Maximum number of entries per source.",
            scope="module",
            default="50000",
        ),
    ]

    def get_manifest(self) -> ModuleManifest:
        return ModuleManifest.from_module(self).model_copy(update={
            "description": (
                "Unified knowledge index - stores, searches, and links entries "
                "from all data sources (filesystem, database, storage). "
                "Call 'context' to get optimal LLM context in one shot."
            ),
            "author": "Digitorn Team",
            "tags": ["core", "index", "search", "context"],
            "declared_permissions": ["index:read", "index:write", "index:admin"],
            "supported_constraints": self.CONSTRAINTS,
            "provides_services": ["index"],
            "subscribes_events": [
                "digitorn.module.filesystem.action_completed",
                "digitorn.module.database.action_completed",
                "digitorn.module.storage.action_completed",
                "digitorn.watcher.*.file_created",
                "digitorn.watcher.*.file_modified",
                "digitorn.watcher.*.file_deleted",
                "digitorn.watcher.*.file_moved",
            ],
        })

    async def health_check(self) -> dict[str, Any]:
        stats = self.store.stats()
        return {
            "status": "healthy",
            "module_id": self.MODULE_ID,
            "stats": stats,
            "extractors": self.extractors.list_extractors(),
        }

    def metrics(self) -> dict[str, Any]:
        return self.store.stats()

def _estimate_tokens(text: str) -> int:
    return max(1, int(len(text) / _CHARS_PER_TOKEN))

def _strip_line_numbers(content: str) -> str:
    import re
    lines = content.split("\n")
    stripped = []
    for line in lines:
        cleaned = re.sub(r"^\s*\d+│", "", line)
        stripped.append(cleaned)
    return "\n".join(stripped)
