"""Okapi BM25 sparse index — pure Python, zero dependencies."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any


def _tokenize(text: str) -> list[str]:
    try:
        from digitorn.modules.context_builder.scoring import tokenize
        return tokenize(text)
    except ImportError:
        import re
        return [t.lower() for t in re.split(r"\W+", text) if len(t) >= 2]


@dataclass
class _DocEntry:
    doc_id: str
    term_freqs: dict[str, int]
    doc_len: int


class BM25Index:
    def __init__(self, k1: float = 1.5, b: float = 0.75) -> None:
        self.k1 = k1
        self.b = b
        self._docs: dict[str, _DocEntry] = {}
        self._df: dict[str, int] = {}
        self._total_doc_len: int = 0

    def add_documents(self, doc_ids: list[str], texts: list[str]) -> None:
        for doc_id, text in zip(doc_ids, texts):
            if doc_id in self._docs:
                self.remove_documents([doc_id])

            tokens = _tokenize(text)
            tf: dict[str, int] = {}
            for t in tokens:
                tf[t] = tf.get(t, 0) + 1

            entry = _DocEntry(doc_id=doc_id, term_freqs=tf, doc_len=len(tokens))
            self._docs[doc_id] = entry
            self._total_doc_len += entry.doc_len
            for term in tf:
                self._df[term] = self._df.get(term, 0) + 1

    def remove_documents(self, doc_ids: list[str]) -> int:
        removed = 0
        for doc_id in doc_ids:
            entry = self._docs.pop(doc_id, None)
            if entry is None:
                continue
            removed += 1
            self._total_doc_len -= entry.doc_len
            for term in entry.term_freqs:
                self._df[term] -= 1
                if self._df[term] <= 0:
                    del self._df[term]
        return removed

    def search(self, query: str, top_k: int = 20) -> list[tuple[str, float]]:
        if not self._docs:
            return []

        query_tokens = _tokenize(query)
        if not query_tokens:
            return []

        n = len(self._docs)
        avgdl = self._total_doc_len / n if n else 1.0
        scores: dict[str, float] = {}

        for term in query_tokens:
            df = self._df.get(term, 0)
            if df == 0:
                continue
            idf = math.log((n - df + 0.5) / (df + 0.5) + 1.0)
            for doc_id, entry in self._docs.items():
                tf = entry.term_freqs.get(term, 0)
                if tf == 0:
                    continue
                num = tf * (self.k1 + 1)
                den = tf + self.k1 * (1 - self.b + self.b * entry.doc_len / avgdl)
                scores[doc_id] = scores.get(doc_id, 0.0) + idf * num / den

        return sorted(scores.items(), key=lambda x: x[1], reverse=True)[:top_k]

    @property
    def doc_count(self) -> int:
        return len(self._docs)

    @property
    def term_count(self) -> int:
        return len(self._df)

    def contains(self, doc_id: str) -> bool:
        return doc_id in self._docs

    def to_dict(self) -> dict[str, Any]:
        return {
            "k1": self.k1,
            "b": self.b,
            "docs": {
                did: {"tf": e.term_freqs, "dl": e.doc_len}
                for did, e in self._docs.items()
            },
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> BM25Index:
        idx = cls(k1=data.get("k1", 1.5), b=data.get("b", 0.75))
        for doc_id, d in data.get("docs", {}).items():
            entry = _DocEntry(doc_id=doc_id, term_freqs=d["tf"], doc_len=d["dl"])
            idx._docs[doc_id] = entry
            idx._total_doc_len += entry.doc_len
            for term in entry.term_freqs:
                idx._df[term] = idx._df.get(term, 0) + 1
        return idx
