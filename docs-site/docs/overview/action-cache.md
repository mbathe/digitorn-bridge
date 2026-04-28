---

id: action-cache
title: Action Cache
sidebar_position: 10
format: md
---



# Action Cache

The LLMOS Bridge action cache eliminates redundant, identical tool calls using a **two-level architecture**:

| Level | Name | Backend | Latency | Scope |
|-------|------|---------|---------|-------|
| **L1** | `ActionSessionCache` | Python `dict` | ~100 ns | Per `AgentRuntime` session |
| **L2** | `CacheClient` | Redis / fakeredis | ~1–5 µs | Cross-session, cross-instance |

Both levels work together automatically. Module authors opt into L2 via the `@cacheable` decorator.

---


## The Problem

LLMs sometimes re-read the same file, re-query the same database, or re-fetch the same system info multiple times within a single task - even when nothing has changed:

```
Turn 1:
  filesystem.list_directory(path=.)         → [file1.py, file2.py]
  filesystem.read_file(path=main.py)        → "def main(): ..."

Turn 2 (user asks to add a function):
  filesystem.list_directory(path=.)         → REDUNDANT (same result)
  filesystem.read_file(path=main.py)        → REDUNDANT (nothing changed)
```

Each duplicate call adds latency, wastes LLM context tokens, and burns API credits.

At scale (10 000 apps, 1 M actions/min), different agent sessions reading the same shared files also produce redundant work - this is the gap L1 alone cannot fill.

---


## Architecture

```
LLM tool call
     │
     ▼
AgentRuntime._execute_tool_call()
     │
     ├── L1: ActionSessionCache.get()          ~100 ns, in-memory dict
     │         HIT  → return immediately (no module call)
     │         MISS ↓
     │
     ▼
BaseModule.execute()
     │
     ├── L2 invalidation (writes only)         delete Redis keys before write
     │
     ├── L2: CacheClient.get()                 ~1–5 µs, Redis/fakeredis
     │         HIT  → return immediately (no handler call)
     │         MISS ↓
     │
     ▼
_action_<name>(params)                         actual execution (ms–s)
     │
     ├── L2: CacheClient.set(result, ttl=...)  store for cross-session reuse
     │
     └── L1: ActionSessionCache.put(result)    store for intra-session dedup
```

---


## L1 - ActionSessionCache

### What it does

`ActionSessionCache` intercepts tool calls **before** they reach the module executor inside `AgentRuntime._execute_tool_call()`. If the exact same call has already been made in the current session - and no write has since touched the relevant resource - the cached result is returned in ~100 ns.

### Architecture

```python
class ActionSessionCache:
    _cache: dict[CacheKey, CacheEntry]     # key → (result, timestamp)
    _path_index: dict[str, set[CacheKey]]  # path → keys (fast write invalidation)
```

### Cache key

`(module_id, action_name, normalized_params)` - path parameters are resolved to absolute paths so `path="."` and `path="/home/user/project"` are treated identically.

### Which actions are cached (L1)

Hardcoded read-only actions (idempotent, no side effects):

| Module | Cached actions |
|--------|---------------|
| `filesystem` | `list_directory`, `read_file`, `get_file_info`, `search_files`, `compute_checksum` |
| `os_exec` | `get_system_info`, `get_env_var`, `list_processes` |
| `database` | `list_tables`, `get_table_schema`, `fetch_results` |
| `db_gateway` | `introspect`, `count`, `find`, `find_one` |

### Write-based invalidation (L1)

When a write executes, the cache automatically removes all read entries whose paths **overlap** with the write target (parent, child, or exact match):

```
filesystem.write_file(path=/project/src/main.py)
     │
     └── Invalidates:
           list_directory(path=/project)        ← parent
           list_directory(path=/project/src)    ← parent
           read_file(path=/project/src/main.py) ← exact
```

### CLI feedback

```
  filesystem.list_directory(path=/project, recursive=True)
  file1.py, file2.py, src/  (cached)   ← L1 hit (cyan)

  filesystem.read_file(path=main.py)
  def main(): ...  142ms               ← real execution
```

---


## L2 - CacheClient (Redis / fakeredis)

### Backend auto-selection

At startup, the `CacheClient` picks the best available backend automatically:

```
REDIS_URL set?
    YES → real Redis (production, cross-process, clustering)
    NO  → embedded fakeredis (zero config, pure Python, in-process)
```

No code changes needed when switching from embedded to production Redis.

```bash
# Default: embedded fakeredis, nothing to configure
llmos-bridge daemon start

# Production: point to a real Redis instance
REDIS_URL=redis://redis-host:6379/0 llmos-bridge daemon start
```

### Cache key format

```
llmos:cache:{module_id}:{action_name}:{sha256_prefix_16}
```

Example: `llmos:cache:filesystem:read_file:a3f9d2c1b4e8f7a0`

The SHA-256 hash is computed over the normalised parameters (subset defined by `key_params`, with path values resolved to absolute paths).

### Invalidation strategy

When `@invalidates_cache("action_a", "action_b")` runs, the L2 cache deletes **all** entries matching:

```
llmos:cache:{module_id}:action_a:*
llmos:cache:{module_id}:action_b:*
```

Use `"*"` to invalidate all cached actions of the module:

```python
@invalidates_cache("*")
async def _action_reset(self, params): ...
# deletes: llmos:cache:{module_id}:*
```

---


## Module Author Guide - `@cacheable` and `@invalidates_cache`

### Import

```python
# Option A - from the cache package directly
from llmos_bridge.cache import cacheable, invalidates_cache

# Option B - from the modules package (re-exported for convenience)
from llmos_bridge.modules import cacheable, invalidates_cache
```

### `@cacheable`

```python
@cacheable(
    ttl=300,              # seconds until the cache entry expires (0 = no expiry)
    key_params=None,      # params included in the key (None = all params)
    shared=True,          # True = L2 Redis; False = L1 only
    invalidated_by=None,  # informational: which write actions invalidate this
)
```

**Parameters:**

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `ttl` | `int` | `300` | Seconds until the cache entry expires. `0` = no expiry (lives until daemon restart or explicit flush). |
| `key_params` | `list[str] \| None` | `None` | Param names used in the cache key. Narrowing the key increases hit rate. `None` = all params. |
| `shared` | `bool` | `True` | `True` = L2 Redis/fakeredis (cross-session). `False` = skip L2, use L1 only. |
| `invalidated_by` | `list[str] \| None` | `None` | Informational: which write actions invalidate this. Actual invalidation is declared on the write side. |

**Example:**

```python
from llmos_bridge.modules import BaseModule, cacheable, invalidates_cache
from llmos_bridge.security.decorators import requires_permission
from llmos_bridge.security.models import Permission


class WeatherModule(BaseModule):
    MODULE_ID = "weather"

    @requires_permission(Permission.NETWORK_REQUEST)
    @cacheable(ttl=600, key_params=["city", "units"])
    async def _action_get_current(self, params: dict) -> dict:
        """Fetch current weather - expensive HTTP call, cache it."""
        city = params["city"]
        # ... HTTP call ...
        return {"city": city, "temp": 22, "condition": "sunny"}

    @requires_permission(Permission.NETWORK_REQUEST)
    @cacheable(ttl=3600, key_params=["city"])
    async def _action_get_forecast(self, params: dict) -> dict:
        city = params["city"]
        # ... HTTP call ...
        return {"city": city, "forecast": [...]}
```

### `@invalidates_cache`

```python
@invalidates_cache("action_name_1", "action_name_2", ...)
```

Place this on write/mutating actions. When the decorated action executes successfully, all L2 cache entries for the listed action names in **this module** are deleted.

**Example:**

```python
class DatabaseModule(BaseModule):
    MODULE_ID = "database"

    @cacheable(ttl=300, key_params=["table", "where"])
    async def _action_fetch_results(self, params: dict) -> dict:
        # cached SELECT query
        ...

    @cacheable(ttl=600)
    async def _action_list_tables(self, params: dict) -> dict:
        # cached SHOW TABLES
        ...

    @invalidates_cache("fetch_results", "list_tables")
    async def _action_execute(self, params: dict) -> dict:
        # INSERT / UPDATE / DELETE - clears read caches
        ...
```

### Decorator stacking order

Always place `@cacheable` / `@invalidates_cache` **inside** (closer to the function than) `@requires_permission`. The metadata propagates outward automatically:

```python
# CORRECT
@requires_permission(Permission.FILESYSTEM_READ)
@cacheable(ttl=60, key_params=["path"])
async def _action_read_file(self, params: dict) -> dict:
    ...

# CORRECT - multiple decorators stacked
@requires_permission(Permission.FILESYSTEM_WRITE)
@invalidates_cache("read_file", "list_directory")
@rate_limited(calls_per_minute=60)
@audit_trail("standard")
async def _action_write_file(self, params: dict) -> dict:
    ...
```

---


## Output types

The cache stores whatever the action returns - any JSON-serialisable Python value:

| Return type | Cached as | Notes |
|-------------|-----------|-------|
| `dict` | JSON object | Most common |
| `list` | JSON array | Batch results |
| `str` | JSON string | |
| `int` / `float` | JSON number | |
| `bool` | JSON boolean | |
| Non-serialisable | `str` fallback | `datetime`, `Path`, etc. via `default=str` |

Binary data (bytes, images) is **not** supported - convert to base64 string before returning.

---


## TTL guidelines

| Data type | Recommended TTL | Rationale |
|-----------|----------------|-----------|
| Static config files | 300–600 s | Changes rarely |
| Directory listings | 60 s | Invalidated by writes anyway |
| File content | 60 s | Invalidated by writes anyway |
| File checksum | 120 s | Expensive to compute |
| System info (CPU/disk) | 10 s | Changes constantly |
| Process list | 5 s | Highly dynamic |
| API responses | 300–3600 s | Depends on API |
| Database schema | 600 s | Changes rarely |
| Database query results | 30–300 s | Depends on write frequency |

---


## Disabling the cache

### Disable L1

```python
self._action_cache = ActionSessionCache(enabled=False)
```

### Disable L2 for a specific action

```python
@cacheable(ttl=60, shared=False)  # shared=False → L1 only, skip L2
async def _action_read_private(self, params: dict) -> dict:
    ...
```

### Flush all L2 entries

```python
from llmos_bridge.cache import get_cache_client

cache = await get_cache_client()
await cache.flush()  # deletes all llmos:cache:* keys
```

---


## Stats

### L1

```python
agent._action_cache.stats()
# {"cached": 3, "hits": 5, "misses": 8, "invalidations": 2}
```

### L2

```python
from llmos_bridge.cache import get_cache_client

cache = await get_cache_client()
await cache.stats()
# {"backend": "fakeredis", "enabled": True, "keyspace_hits": 142, "keyspace_misses": 38}
```

---


## Scope and limitations

| Property | L1 | L2 |
|----------|----|----|
| Scope | Per `AgentRuntime` instance | Cross-session, cross-instance |
| Persistence | Lost on session end | In-memory (fakeredis) or persistent (Redis) |
| TTL | None - full session | Configurable per action |
| External changes | Not detected | Not detected (rely on TTL) |
| Scale | 1 session | 10 000+ apps sharing 1 Redis |
| Latency | ~100 ns | ~1–5 µs (fakeredis) / ~300 µs (Redis) |
