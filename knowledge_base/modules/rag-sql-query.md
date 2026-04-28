---
id: rag-sql-query
title: "rag.sql_query (RagSqlQuery)"
type: module-action
module: rag
action: sql_query
fqn: rag.sql_query
short_name: RagSqlQuery
keywords: [rag, sql_query, ragsqlquery, search, database, text2sql]
permissions: []
risk_level: medium
irreversible: false
require_approval: false
---

# rag.sql_query (RagSqlQuery)

## Description
Answer a natural language question by generating and executing SQL.

## Parameters
| Name | Type | Required | Default | Description |
|------|------|:--------:|---------|-------------|
| `query` | string | ✓ | - | Natural language question. |
| `connection_id` | string | ✓ | - | Database connection ID. |
| `knowledge_base` | string |  | `` | KB with schema info. Empty = auto-detect. |
| `top_k` | integer |  | `5` | Max results. |

## Capability grant (in app YAML)
```yaml
capabilities:
  grant:
    - module: rag
      actions: [sql_query]
```

## Safety
- Risk level: **medium**
