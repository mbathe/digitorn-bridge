"""Citation and provenance tracking for RAG results."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

@dataclass(slots=True)
class Citation:
    source_type: str   # file | database | web | api | manual
    source_id: str     # docs/policy.pdf | crm:users | https://...
    location: str = ""
    content_hash: str = ""
    timestamp: str = ""
    confidence: float = 0.0
    metadata: dict[str, Any] = field(default_factory=dict)

    def format_header(self, index: int) -> str:
        parts = [f"[{index}] (source: {self.source_id}"]
        if self.location:
            parts[0] += f", {self.location}"
        if self.confidence > 0:
            parts[0] += f", confidence: {self.confidence:.2f}"
        parts[0] += ")"
        return parts[0]

@dataclass(slots=True)
class RetrievalResult:
    text: str
    score: float
    doc_id: str = ""
    citation: Citation = field(
        default_factory=lambda: Citation(source_type="manual", source_id="unknown"),
    )

_CITATION_INSTRUCTION = (
    "When answering based on retrieved context, ALWAYS cite sources using [N] notation.\n"
    "- Cite each claim: \"Employees get 25 days [1].\"\n"
    "- Multiple sources: \"Average is 22.3 [2], per policy [3].\"\n"
    "- If sources conflict, mention both with their numbers.\n"
    "- If no source supports a claim, say so explicitly.\n"
    "- Never fabricate source numbers."
)

def get_citation_instruction() -> str:
    return _CITATION_INSTRUCTION

def format_context_block(
    results: list[RetrievalResult],
    max_chars: int = 8000,
) -> str:
    if not results:
        return ""

    header = "## Retrieved context - cite sources using [1], [2], etc.\n"
    lines = [header]
    total = len(header)

    for i, result in enumerate(results, 1):
        hdr = result.citation.format_header(i)
        block = f"{hdr}\n{result.text}\n"

        if total + len(block) > max_chars:
            remaining = max_chars - total - len(hdr) - 20
            if remaining > 50:
                lines.append(f"{hdr}\n{result.text[:remaining]}... [truncated]\n")
            break

        lines.append(block)
        total += len(block)

    return "\n".join(lines)

def verify_citations(response: str, num_sources: int) -> list[dict[str, Any]]:
    cited = {int(m) for m in re.findall(r"\[(\d+)\]", response)}
    valid = set(range(1, num_sources + 1))
    return [
        {
            "type": "invalid_citation",
            "citation": n,
            "message": f"[{n}] references non-existent source (only {num_sources} provided).",
        }
        for n in sorted(cited - valid)
    ]
