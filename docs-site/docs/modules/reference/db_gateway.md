---

id: db_gateway
title: Database Gateway Module
sidebar_position: 13
format: md
---




# db_gateway

Semantic database access layer designed for LLM agents. No SQL required - operations use entity names and MongoDB-style filter syntax. Supports any database through pluggable adapters.

| Property | Value |
|----------|-------|
| **Module ID** | `db_gateway` |
| **Version** | `1.1.0` |
| **Type** | database |
| **Platforms** | All |
| **Dependencies** | `sqlalchemy` (for SQL adapter) |
| **Declared Permissions** | `database.read`, `database.write` |

---


## Why db_gateway?

The `database` module requires agents to write raw SQL. This works but has two problems:

1. **LLMs make SQL mistakes** - wrong table names, incorrect JOIN syntax, SQL dialect differences
2. **SQL is not semantic** - the agent needs to know the schema intimately

`db_gateway` provides an abstraction layer where:
- The agent says "find users where age > 25" using a structured filter
- The adapter translates to the appropriate backend query
- Schema introspection is built-in, so the agent can discover tables and columns

---


## Actions (12)

### Connection

| Action | Description | Key Parameters |
|--------|-------------|----------------|
| `connect` | Open semantic connection | `connection_id`, `driver`, `url` or `host`/`port`/`database` |
| `disconnect` | Close connection | `connection_id` |
| `introspect` | Get schema (tables, columns, types) | `connection_id`, `table` (optional) |

### Read Operations

| Action | Description | Key Parameters |
|--------|-------------|----------------|
| `find` | Query with MongoDB-style filters | `connection_id`, `entity`, `filter`, `fields`, `sort`, `limit`, `offset` |
| `find_one` | Query single record | `connection_id`, `entity`, `filter` |
| `count` | Count matching records | `connection_id`, `entity`, `filter` |
| `search` | Full-text search | `connection_id`, `entity`, `query`, `fields` |
| `aggregate` | Aggregation pipeline | `connection_id`, `entity`, `pipeline` |

### Write Operations

| Action | Description | Key Parameters |
|--------|-------------|----------------|
| `create` | Insert single record | `connection_id`, `entity`, `data` |
| `create_many` | Insert multiple records | `connection_id`, `entity`, `data` (array) |
| `update` | Update matching records | `connection_id`, `entity`, `filter`, `data` |
| `delete` | Delete matching records | `connection_id`, `entity`, `filter` |

**Security for delete**:
- `@requires_permission(Permission.DATABASE_WRITE)`
- `@sensitive_action(RiskLevel.MEDIUM)`
- `@audit_trail("detailed")`

---


## Filter Syntax

Filters use a MongoDB-inspired syntax:

```json
{
  "age": {"$gt": 25},
  "status": "active",
  "name": {"$like": "%john%"},
  "role": {"$in": ["admin", "moderator"]}
}
```

Supported operators:

| Operator | Description | Example |
|----------|-------------|---------|
| `$eq` | Equals (default) | `{"status": "active"}` |
| `$ne` | Not equals | `{"status": {"$ne": "deleted"}}` |
| `$gt` / `$gte` | Greater than (or equal) | `{"age": {"$gt": 25}}` |
| `$lt` / `$lte` | Less than (or equal) | `{"price": {"$lt": 100}}` |
| `$in` | In array | `{"role": {"$in": ["admin", "mod"]}}` |
| `$nin` | Not in array | `{"status": {"$nin": ["deleted"]}}` |
| `$like` | SQL LIKE pattern | `{"name": {"$like": "%john%"}}` |
| `$regex` | Regular expression | `{"email": {"$regex": ".*@gmail.com"}}` |
| `$exists` | Field exists | `{"avatar": {"$exists": true}}` |
| `$and` / `$or` | Logical operators | `{"$or": [{"age": {"$gt": 25}}, {"role": "admin"}]}` |

---


## Adapter Architecture

```
db_gateway action
    |
    v
Adapter ABC (base_adapter.py)
    |
    +--→ SQLAlchemy adapter (sql_adapter.py) - PostgreSQL, MySQL, SQLite, etc.
    |
    +--→ Custom adapter (community plugin via entry points)
```

The SQLAlchemy adapter is built-in and handles all SQL databases. Custom adapters for MongoDB, Redis, or other backends can be installed as pip packages.

---


## Implementation Notes

- SQL dialect auto-detection from connection URL
- Connection pooling via SQLAlchemy engine
- Schema caching with configurable TTL (`db_gateway.schema_cache_ttl`)
- Auto-introspect on first connection (configurable)
- Row limit enforcement (`db_gateway.default_row_limit`)
