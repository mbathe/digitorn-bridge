---
id: vector
title: vector Module
sidebar_label: vector
sidebar_position: 14
description: Vector collections — FastEmbed embeddings, Qdrant storage, 4 chunking strategies, hybrid search.
---

# vector

Lightweight vector module for direct collection management.
Agents create named collections, index documents with
automatic chunking, and search semantically or with hybrid
keyword + vector. Shares the FastEmbed singleton with
`context_builder` and `rag` — zero extra memory cost when all
three are loaded.

| Property | Value | Source |
|----------|-------|--------|
| Module id | `vector` | `module.py` |
| Version | `1.1.0` | `module.py` |
| Type | user | |
| Pip deps | `fastembed`, `qdrant-client` | |

> **Use `vector` for raw vector ops; use `rag` for full
> RAG pipelines** (knowledge bases, hybrid retrieval, RRF
> fusion, citations, semantic cache, Text2SQL).
> [rag reference →](rag.md)

## The 14 actions

`module.py`. Mostly `risk_level: low` (reads), `medium` for
inserts, `high` for `delete_collection`.

| Tool | Source | Purpose |
|------|--------|---------|
| `vector.create_collection` | `:265` | Create a named collection. |
| `vector.delete_collection` | `:290` | Delete a collection + all its docs. |
| `vector.list_collections` | `:312` | List user collections + counts. |
| `vector.add` | `:324` | Add raw text documents (chunks + embeds). |
| `vector.add_file` | `:374` | Read + chunk + embed + add a file. |
| `vector.search` | `:471` | Semantic search. |
| `vector.hybrid_search` | `:522` | Semantic + keyword fusion. |
| `vector.get` | `:585` | Retrieve docs by id. |
| `vector.delete` | `:617` | Delete docs by id (or filter). |
| `vector.update_metadata` | `:652` | Patch metadata on existing docs. |
| `vector.count` | `:681` | Count docs (with optional filter). |
| `vector.collection_stats` | `:701` | Doc count, dimensions, storage info. |
| `vector.add_directory` | `:736` | Walk a directory tree, chunk + embed each file. Skips unchanged files (content-hash dedup). |
| `vector.search_multi` | `:851` | Fan-out semantic search across many collections, merged + re-ranked. |

## Chunking strategies

| Strategy | Splits on | Default size | Best for |
|----------|-----------|--------------|----------|
| `fixed` | N characters + overlap | 500 chars | Structured data, code. |
| `sentence` | Sentence boundaries (`. ! ?`) | 500 chars | Natural prose. |
| `paragraph` | Double newlines (`\n\n`) | 1000 chars | Articles, docs. |
| `recursive` *(default)* | `\n\n` → `\n` → `. ` → ` ` → char | 500 chars | Universal. |

Each chunk carries `{text, index, start_char, end_char,
metadata}`.

## Embedding model

| Property | Value |
|----------|-------|
| Default model | `sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2` (`minilm-l12`). |
| Dimensions | 384. |
| Languages | ~50. |
| Source | FastEmbed (ONNX, CPU). |
| Sharing | Singleton with `context_builder` + `rag`. |

Override via `config.embedding_model` (any FastEmbed-supported
HuggingFace id).

## Collection isolation

Naming convention: user collections become
`user_{app_id}_{name}` under the hood — separate from the
system `tools` collection used by tool discovery. Two apps
with the same nominal collection name don't clash.

## Configuration

```yaml
tools:
  modules:
    vector:
      config:
        embedding_model: null            # null = use context_builder's singleton
        default_chunk_size: 500
        default_overlap: 50
        persistence_dir: null            # null = in-memory; otherwise Qdrant on-disk path
```

## Aliases (FR / EN)

`vector` ships extensive aliases for international agents.
A few examples:

| Action | Aliases |
|--------|---------|
| `vector.create_collection` | `creer_collection`, `new_collection` |
| `vector.add` | `ajouter`, `indexer`, `embed`, `insert` |
| `vector.search` | `rechercher`, `chercher`, `query`, `find_similar` |
| `vector.hybrid_search` | `recherche_hybride` |
| `vector.delete` | `supprimer_documents`, `remove` |

Full list per action via `@action(aliases=[...])` in
`module.py`.

## Cross-references

- App-config block reference (`tools.modules.vector`):
  [App Configuration → tools.modules](../../language/02-app-config.md#toolsmodules--module-config)
- Full RAG pipeline (knowledge bases, RRF, citations, cache,
  Text2SQL): [rag reference](rag.md)
- index module (system-level workspace index):
  [index reference](index_module.md)
