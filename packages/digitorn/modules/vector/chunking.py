"""Text chunking strategies for vector indexing.

Four strategies from simple to intelligent:
- fixed: character count with overlap
- sentence: split on sentence boundaries
- paragraph: split on double newlines
- recursive: smart recursive split (LangChain-style)
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any


@dataclass
class Chunk:
    """A text chunk with position metadata."""

    text: str
    index: int
    start_char: int
    end_char: int
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "index": self.index,
            "start_char": self.start_char,
            "end_char": self.end_char,
            **self.metadata,
        }


def fixed_chunks(text: str, size: int = 500, overlap: int = 50) -> list[Chunk]:
    """Split text into fixed-size character chunks with overlap."""
    if not text:
        return []
    chunks = []
    start = 0
    idx = 0
    while start < len(text):
        end = min(start + size, len(text))
        chunk_text = text[start:end]
        if chunk_text.strip():
            chunks.append(Chunk(text=chunk_text, index=idx, start_char=start, end_char=end))
            idx += 1
        start += size - overlap
        if start >= len(text):
            break
    return chunks


def sentence_chunks(text: str, size: int = 500, overlap: int = 1) -> list[Chunk]:
    """Split on sentence boundaries, grouping sentences up to `size` chars.

    ``overlap`` is in number of sentences (not characters).
    """
    if not text:
        return []
    # Split on sentence-ending punctuation followed by whitespace
    sentences = re.split(r'(?<=[.!?])\s+', text)
    sentences = [s.strip() for s in sentences if s.strip()]
    if not sentences:
        return [Chunk(text=text, index=0, start_char=0, end_char=len(text))]

    chunks = []
    idx = 0
    i = 0
    while i < len(sentences):
        group = []
        total = 0
        j = i
        while j < len(sentences) and (total + len(sentences[j]) + 1 <= size or not group):
            group.append(sentences[j])
            total += len(sentences[j]) + 1
            j += 1
        chunk_text = " ".join(group)
        # Calculate char positions (approximate)
        start_char = text.find(group[0]) if group else 0
        end_char = start_char + len(chunk_text)
        chunks.append(Chunk(text=chunk_text, index=idx, start_char=start_char, end_char=end_char))
        idx += 1
        # Advance with sentence overlap
        i = max(j - overlap, i + 1)
    return chunks


def paragraph_chunks(text: str, size: int = 1000, overlap: int = 0) -> list[Chunk]:
    """Split on double newlines (paragraphs), merging small ones up to `size`."""
    if not text:
        return []
    paragraphs = re.split(r'\n\s*\n', text)
    paragraphs = [p.strip() for p in paragraphs if p.strip()]
    if not paragraphs:
        return [Chunk(text=text, index=0, start_char=0, end_char=len(text))]

    chunks = []
    idx = 0
    current_parts: list[str] = []
    current_len = 0
    char_pos = 0

    for para in paragraphs:
        if current_len + len(para) + 2 > size and current_parts:
            chunk_text = "\n\n".join(current_parts)
            chunks.append(Chunk(text=chunk_text, index=idx, start_char=char_pos, end_char=char_pos + len(chunk_text)))
            idx += 1
            char_pos += len(chunk_text) + 2
            current_parts = []
            current_len = 0
        current_parts.append(para)
        current_len += len(para) + 2

    if current_parts:
        chunk_text = "\n\n".join(current_parts)
        chunks.append(Chunk(text=chunk_text, index=idx, start_char=char_pos, end_char=char_pos + len(chunk_text)))

    return chunks


def recursive_chunks(text: str, size: int = 500, overlap: int = 50) -> list[Chunk]:
    """Smart recursive text splitter — tries natural boundaries first.

    Splits by: ``\\n\\n`` → ``\\n`` → ``. `` → `` `` → character.
    Same algorithm as LangChain's RecursiveCharacterTextSplitter.
    """
    if not text:
        return []

    separators = ["\n\n", "\n", ". ", " ", ""]

    def _split_recursive(t: str, seps: list[str]) -> list[str]:
        if len(t) <= size:
            return [t] if t.strip() else []

        sep = seps[0] if seps else ""
        remaining_seps = seps[1:] if len(seps) > 1 else [""]

        if not sep:
            # Character-level split
            pieces = []
            start = 0
            while start < len(t):
                end = min(start + size, len(t))
                pieces.append(t[start:end])
                start += size - overlap
            return pieces

        parts = t.split(sep)
        result = []
        current = ""
        for part in parts:
            candidate = current + sep + part if current else part
            if len(candidate) <= size:
                current = candidate
            else:
                if current:
                    if len(current) <= size:
                        result.append(current)
                    else:
                        result.extend(_split_recursive(current, remaining_seps))
                current = part
        if current:
            if len(current) <= size:
                result.append(current)
            else:
                result.extend(_split_recursive(current, remaining_seps))
        return result

    raw_chunks = _split_recursive(text, separators)

    # Add overlap between chunks
    chunks = []
    char_pos = 0
    for i, chunk_text in enumerate(raw_chunks):
        if not chunk_text.strip():
            continue
        start = text.find(chunk_text, max(0, char_pos - overlap))
        if start == -1:
            start = char_pos
        end = start + len(chunk_text)
        chunks.append(Chunk(text=chunk_text, index=i, start_char=start, end_char=end))
        char_pos = end

    # Re-index
    for i, c in enumerate(chunks):
        c.index = i

    return chunks


STRATEGIES = {
    "fixed": fixed_chunks,
    "sentence": sentence_chunks,
    "paragraph": paragraph_chunks,
    "recursive": recursive_chunks,
}


def chunk_text(
    text: str,
    strategy: str = "recursive",
    size: int = 500,
    overlap: int = 50,
) -> list[Chunk]:
    """Chunk text using the named strategy."""
    func = STRATEGIES.get(strategy)
    if func is None:
        raise ValueError(f"Unknown chunking strategy: {strategy}. Available: {list(STRATEGIES.keys())}")
    return func(text, size=size, overlap=overlap)
