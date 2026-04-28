"""ContextualRetrieval - Anthropic pattern for chunk enrichment.

Before embedding, each chunk gets a LLM-generated context prefix that
situates it within the full document. Improves retrieval by 49% (Anthropic benchmark).

This strategy wraps the ingestion phase, not the retrieval phase.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

logger = logging.getLogger(__name__)

_CONTEXT_PROMPT = (
    "Here is a document:\n<document>\n{document}\n</document>\n\n"
    "Here is a chunk from that document:\n<chunk>\n{chunk}\n</chunk>\n\n"
    "Give a short (2-3 sentence) context that situates this chunk within "
    "the overall document. Focus on what makes this chunk unique and searchable. "
    "Respond with ONLY the context, no preamble."
)


class ContextualEnricher:
    """Generates context prefixes for chunks before embedding."""

    def __init__(
        self,
        llm_caller: Any,
        *,
        concurrency: int = 5,
        prompt_template: str = "",
    ) -> None:
        self._llm = llm_caller
        self._semaphore = asyncio.Semaphore(concurrency)
        self._template = prompt_template or _CONTEXT_PROMPT

    async def enrich_chunks(
        self,
        document: str,
        chunks: list[str],
    ) -> list[str]:
        doc_preview = document[:3000] if len(document) > 3000 else document

        tasks = [
            self._enrich_one(doc_preview, chunk)
            for chunk in chunks
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        enriched = []
        for chunk, result in zip(chunks, results):
            if isinstance(result, Exception):
                logger.debug("Contextual enrichment failed for chunk: %s", result)
                enriched.append(chunk)
            elif result:
                enriched.append(f"{result}\n\n{chunk}")
            else:
                enriched.append(chunk)

        return enriched

    async def _enrich_one(self, document: str, chunk: str) -> str:
        async with self._semaphore:
            prompt = self._template.format(document=document, chunk=chunk)
            try:
                result = await self._llm(prompt)
                if isinstance(result, str):
                    return result.strip()
                return str(result).strip() if result else ""
            except Exception as exc:
                logger.debug("Enrichment call failed: %s", exc)
                return ""
