"""Index module - in-memory store with inverted index."""

from __future__ import annotations

import re
import time
from typing import Any

from .types import IndexEntry, Relation, Source

_SPLIT_RE = re.compile(r"[^a-zA-Z0-9_]+")
_MIN_TOKEN_LEN = 2

def _tokenize(text: str) -> set[str]:
    tokens = set()
    for part in _SPLIT_RE.split(text.lower()):
        if len(part) >= _MIN_TOKEN_LEN:
            tokens.add(part)
            for sub in re.findall(r"[a-z]+|[A-Z][a-z]*|[0-9]+", part):
                if len(sub) >= _MIN_TOKEN_LEN:
                    tokens.add(sub.lower())
    return tokens

class IndexStore:
    """In-memory index store - fast lookups, inverted index for full-text."""

    def __init__(self) -> None:
        self._sources: dict[str, Source] = {}

        self._entries: dict[str, IndexEntry] = {}

        self._by_path: dict[str, set[str]] = {}
        self._by_kind: dict[str, set[str]] = {}
        self._by_source: dict[str, set[str]] = {}
        self._by_name: dict[str, set[str]] = {}

        self._fts: dict[str, set[str]] = {}

        self._relations_out: dict[str, list[Relation]] = {}
        self._relations_in: dict[str, list[Relation]] = {}

    def add_source(self, source: Source) -> None:
        self._sources[source.source_id] = source

    def get_source(self, source_id: str) -> Source | None:
        return self._sources.get(source_id)

    def list_sources(self) -> list[Source]:
        return list(self._sources.values())

    def remove_source(self, source_id: str) -> int:
        """Remove a source and all its entries. Returns count of removed entries."""
        self._sources.pop(source_id, None)
        entry_ids = list(self._by_source.get(source_id, []))
        for eid in entry_ids:
            self._remove_entry(eid)
        self._by_source.pop(source_id, None)
        return len(entry_ids)

    def upsert(self, entry: IndexEntry) -> bool:
        """Insert or update an entry. Returns True if content changed."""
        existing = self._entries.get(entry.entry_id)
        if existing and existing.content_hash == entry.content_hash:
            return False

        if existing:
            self._unindex_entry(existing)

        entry.updated_at = time.time()
        self._entries[entry.entry_id] = entry
        self._index_entry(entry)
        return True

    def get(self, entry_id: str) -> IndexEntry | None:
        return self._entries.get(entry_id)

    def get_by_path(self, path: str) -> list[IndexEntry]:
        ids = self._by_path.get(path, set())
        return [self._entries[eid] for eid in ids if eid in self._entries]

    def get_by_name(self, name: str) -> list[IndexEntry]:
        ids = self._by_name.get(name.lower(), set())
        return [self._entries[eid] for eid in ids if eid in self._entries]

    def _remove_entry(self, entry_id: str) -> None:
        entry = self._entries.pop(entry_id, None)
        if entry:
            self._unindex_entry(entry)
        for rel in self._relations_out.pop(entry_id, []):
            in_list = self._relations_in.get(rel.to_id, [])
            self._relations_in[rel.to_id] = [
                r for r in in_list if r.from_id != entry_id
            ]
        for rel in self._relations_in.pop(entry_id, []):
            out_list = self._relations_out.get(rel.from_id, [])
            self._relations_out[rel.from_id] = [
                r for r in out_list if r.to_id != entry_id
            ]

    def _index_entry(self, entry: IndexEntry) -> None:
        eid = entry.entry_id
        self._by_path.setdefault(entry.path, set()).add(eid)
        self._by_kind.setdefault(entry.kind, set()).add(eid)
        self._by_source.setdefault(entry.source_id, set()).add(eid)
        self._by_name.setdefault(entry.name.lower(), set()).add(eid)

        tokens = _tokenize(f"{entry.name} {entry.signature} {entry.summary}")
        for token in tokens:
            self._fts.setdefault(token, set()).add(eid)

    def _unindex_entry(self, entry: IndexEntry) -> None:
        eid = entry.entry_id
        self._by_path.get(entry.path, set()).discard(eid)
        self._by_kind.get(entry.kind, set()).discard(eid)
        self._by_source.get(entry.source_id, set()).discard(eid)
        self._by_name.get(entry.name.lower(), set()).discard(eid)

        tokens = _tokenize(f"{entry.name} {entry.signature} {entry.summary}")
        for token in tokens:
            self._fts.get(token, set()).discard(eid)

    def add_relation(self, relation: Relation) -> None:
        out_list = self._relations_out.setdefault(relation.from_id, [])
        for existing in out_list:
            if existing.to_id == relation.to_id and existing.kind == relation.kind:
                return
        out_list.append(relation)
        self._relations_in.setdefault(relation.to_id, []).append(relation)

    def get_relations(
        self,
        entry_id: str,
        direction: str = "both",
        kind: str | None = None,
        depth: int = 1,
    ) -> tuple[list[IndexEntry], list[Relation]]:
        """Traverse the relation graph from an entry."""
        visited_entries: dict[str, IndexEntry] = {}
        collected_relations: list[Relation] = []
        frontier = {entry_id}

        for _ in range(depth):
            next_frontier: set[str] = set()
            for eid in frontier:
                if direction in ("out", "both"):
                    for rel in self._relations_out.get(eid, []):
                        if kind and rel.kind != kind:
                            continue
                        collected_relations.append(rel)
                        if rel.to_id not in visited_entries and rel.to_id != entry_id:
                            entry = self._entries.get(rel.to_id)
                            if entry:
                                visited_entries[rel.to_id] = entry
                                next_frontier.add(rel.to_id)

                if direction in ("in", "both"):
                    for rel in self._relations_in.get(eid, []):
                        if kind and rel.kind != kind:
                            continue
                        collected_relations.append(rel)
                        if rel.from_id not in visited_entries and rel.from_id != entry_id:
                            entry = self._entries.get(rel.from_id)
                            if entry:
                                visited_entries[rel.from_id] = entry
                                next_frontier.add(rel.from_id)

            frontier = next_frontier
            if not frontier:
                break

        return list(visited_entries.values()), collected_relations

    def search(
        self,
        query: str,
        kind: str | None = None,
        source_id: str | None = None,
        limit: int = 20,
    ) -> list[tuple[IndexEntry, float]]:
        """Search the index. Returns (entry, score) sorted by relevance."""
        tokens = _tokenize(query)
        if not tokens:
            return []

        scores: dict[str, float] = {}
        for token in tokens:
            matching = self._fts.get(token, set())
            for eid in matching:
                scores[eid] = scores.get(eid, 0) + 1.0

        query_lower = query.lower().strip()
        for eid in self._by_name.get(query_lower, set()):
            scores[eid] = scores.get(eid, 0) + 5.0

        results: list[tuple[IndexEntry, float]] = []
        for eid, score in scores.items():
            entry = self._entries.get(eid)
            if not entry:
                continue
            if kind and entry.kind != kind:
                continue
            if source_id and entry.source_id != source_id:
                continue
            results.append((entry, score / len(tokens)))

        results.sort(key=lambda x: -x[1])
        return results[:limit]

    def invalidate_by_path(self, path: str) -> int:
        """Remove all entries for a given path. Returns count removed."""
        entry_ids = list(self._by_path.get(path, []))
        for eid in entry_ids:
            self._remove_entry(eid)
        return len(entry_ids)

    def invalidate_by_source(self, source_id: str) -> int:
        """Remove all entries for a source. Returns count removed."""
        entry_ids = list(self._by_source.get(source_id, []))
        for eid in entry_ids:
            self._remove_entry(eid)
        return len(entry_ids)

    def stats(self) -> dict[str, Any]:
        return {
            "sources": len(self._sources),
            "entries": len(self._entries),
            "relations": sum(len(v) for v in self._relations_out.values()),
            "fts_tokens": len(self._fts),
            "by_kind": {k: len(v) for k, v in self._by_kind.items() if v},
        }

    def snapshot(self) -> dict[str, Any]:
        """Serialize the entire store for persistence."""
        return {
            "sources": {
                sid: {
                    "source_id": s.source_id,
                    "module_id": s.module_id,
                    "root": s.root,
                    "extractor": s.extractor,
                    "scan_pattern": s.scan_pattern,
                    "metadata": s.metadata,
                    "watch": s.watch,
                    "watch_mode": s.watch_mode,
                    "app_id": s.app_id,
                    "last_scan": s.last_scan,
                    "entry_count": s.entry_count,
                }
                for sid, s in self._sources.items()
            },
            "entries": {
                eid: {
                    "entry_id": e.entry_id,
                    "source_id": e.source_id,
                    "path": e.path,
                    "kind": e.kind,
                    "name": e.name,
                    "signature": e.signature,
                    "summary": e.summary,
                    "line_start": e.line_start,
                    "line_end": e.line_end,
                    "content_hash": e.content_hash,
                    "metadata": e.metadata,
                    "updated_at": e.updated_at,
                }
                for eid, e in self._entries.items()
            },
            "relations": [
                rel.to_dict()
                for rels in self._relations_out.values()
                for rel in rels
            ],
        }

    def restore(self, data: dict[str, Any]) -> None:
        """Restore from a snapshot. Clears existing data first."""
        self.__init__()  # type: ignore[misc]

        for sd in data.get("sources", {}).values():
            self.add_source(Source(**sd))

        for ed in data.get("entries", {}).values():
            entry = IndexEntry(**ed)
            self._entries[entry.entry_id] = entry
            self._index_entry(entry)

        for rd in data.get("relations", []):
            self.add_relation(Relation(**rd))

        for sid, ids in self._by_source.items():
            src = self._sources.get(sid)
            if src:
                src.entry_count = len(ids)
