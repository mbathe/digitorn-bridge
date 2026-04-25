"""Text2SQLStrategy — NL to SQL via database module + ServiceBus.

Flow: question → schema retrieval → SQL generation → validation → execution → formatting.
"""

from __future__ import annotations

import logging
from typing import Any

from ..citations import Citation, RetrievalResult
from .base import RetrievalStrategy

logger = logging.getLogger(__name__)

_SQL_GEN_PROMPT = (
    "Given the following database schema:\n{schema}\n\n"
    "Generate a SQL query to answer this question: {query}\n\n"
    "Rules:\n"
    "- Use only tables and columns from the schema above\n"
    "- Return ONLY the SQL query, no explanation\n"
    "- Use standard SQL syntax\n"
    "- Add LIMIT 100 if no explicit limit\n"
    "- Never use DELETE, UPDATE, INSERT, DROP, ALTER, or TRUNCATE"
)

_UNSAFE_PATTERNS = ("delete ", "update ", "insert ", "drop ", "alter ", "truncate ", "create ", "grant ")


class Text2SQLStrategy(RetrievalStrategy):

    def __init__(
        self,
        service_bus: Any,
        connection_id: str,
        schema_text: str,
        llm_caller: Any,
    ) -> None:
        self._bus = service_bus
        self._conn_id = connection_id
        self._schema = schema_text
        self._llm = llm_caller

    async def retrieve(
        self,
        query: str,
        *,
        top_k: int = 5,
        min_score: float = 0.0,
        metadata_filter: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> list[RetrievalResult]:
        sql = await self._generate_sql(query)
        if not sql:
            return []

        if not self._validate_sql(sql):
            logger.warning("Generated SQL rejected by safety check: %s", sql[:200])
            return []

        rows = await self._execute_sql(sql)
        if not rows:
            return []

        text = self._format_rows(rows, sql)

        return [RetrievalResult(
            text=text,
            score=0.95,
            doc_id=f"sql:{self._conn_id}:{hash(sql) % 10**8}",
            citation=Citation(
                source_type="database",
                source_id=self._conn_id,
                location=f"query: {sql[:100]}",
                confidence=0.95,
            ),
        )]

    async def _generate_sql(self, query: str) -> str:
        try:
            prompt = _SQL_GEN_PROMPT.format(schema=self._schema, query=query)
            result = await self._llm(prompt)
            if not result:
                return ""

            sql = result.strip() if isinstance(result, str) else str(result).strip()
            sql = sql.strip("`").strip()
            if sql.lower().startswith("sql"):
                sql = sql[3:].strip()

            return sql
        except Exception as exc:
            logger.warning("SQL generation failed: %s", exc)
            return ""

    @staticmethod
    def _validate_sql(sql: str) -> bool:
        lower = sql.lower().strip()
        if not lower.startswith("select"):
            return False
        for pattern in _UNSAFE_PATTERNS:
            if pattern in lower:
                return False
        return True

    async def _execute_sql(self, sql: str) -> list[dict[str, Any]]:
        try:
            result = await self._bus.call("database", "fetch_results", {
                "connection_id": self._conn_id,
                "query": sql,
            })
            if not result or not result.success or not result.data:
                return []
            return result.data.get("rows", [])
        except Exception as exc:
            logger.warning("SQL execution failed: %s", exc)
            return []

    @staticmethod
    def _format_rows(rows: list[dict[str, Any]], sql: str) -> str:
        if not rows:
            return ""

        headers = list(rows[0].keys())
        lines = [" | ".join(headers)]
        lines.append(" | ".join("---" for _ in headers))
        for row in rows[:50]:
            lines.append(" | ".join(str(row.get(h, "")) for h in headers))

        table = "\n".join(lines)
        return f"SQL: `{sql}`\n\n{table}\n\n({len(rows)} rows)"
