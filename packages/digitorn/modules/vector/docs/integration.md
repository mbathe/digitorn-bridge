# vector — Integration Guide

`vector` is the low-level **embeddings + nearest-neighbour search**
primitive. It's what `rag` builds on. Use `vector` directly when you
want raw semantic search over custom documents without the knowledge-
base abstraction.

## Actions

| Action | Purpose |
|---|---|
| `create_collection` | Create (or ensure) a named vector collection. |
| `delete_collection` | Remove a collection and all its vectors. |
| `list_collections` | Enumerate collections the caller can see. |
| `add_documents` | Embed and insert text documents into a collection. |
| `search` | k-NN over a collection with optional metadata filters. |
| `delete_documents` | Remove by id or by metadata filter. |
| `count` / `stats` | Collection size + index state. |

(See `docs/actions.md` for the full list and parameters.)

## Backends

`vector` defaults to an **on-disk Qdrant** store (same backend
convention as `rag`). Configuring the path:

```yaml
modules:
  vector:
    config:
      backend:
        type: qdrant
        path: "./.digitorn/vectors/.qdrant"
```

## Constraints

| Constraint | Type | Scope | Purpose |
|---|---|---|---|
| `paths` | `string_list` | universal | Restrict where `add_documents` can ingest from when given file paths. |
| `max_documents` | `integer` | module | Per-collection cap on the number of vectors. |
| `allowed_collections` | `string_list` | module | Whitelist of collection names the agent can touch. |

## Isolation

`vector` is `shared` — one instance per daemon, multiple apps share
the same store. Collections are scoped by app via the `_app_id_override`
convention the module accepts at config-update time.

## `vector` vs `rag` — when to use which

| Use case | Pick |
|---|---|
| "Chat with these docs, give me ranked snippets" | **rag** (higher-level: handles chunking + metadata + ranking) |
| "I just want k-nearest neighbours of a custom embedding" | **vector** |
| "I want to drop in my own embedding model + dedupe + custom scoring" | **vector** |
| "I ingested 10 k markdown files and want semantic search" | **rag** |

## When NOT to use

- Exact text lookup → use `filesystem.grep` / a SQL index, not vectors.
- Small collections (< 100 docs) where in-memory cosine is enough and
  no cross-session persistence is needed.

## Related

- `modules/rag` — the opinionated layer on top of `vector`
- `modules/vector/module.py` — embedding + Qdrant wiring
