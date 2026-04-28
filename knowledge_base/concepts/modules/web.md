---
id: module-concept-web
title: "web module - overview"
type: module-concept
module: web
isolation: shared
keywords: [web, web-module, search, fetch, extract, download]
version: 1.0.0
---

# `web` module

- **Isolation**: `shared` (one instance shared across apps)
- **Version**: `1.0.0`
- **Actions**: 4 visible, 0 internal

## Description (from class docstring)

Web module - fast search, fetch, and parse web content.

Supports multiple search backends (DuckDuckGo free default, Brave, Tavily, SearXNG).
Uses aiohttp for async HTTP + html2text/bs4 for content parsing.
Includes response caching for repeated fetches.

> Class-level summary: Web search, fetch, and content extraction.

## Configuration

Set under `modules.web.config` in `app.yaml`. All fields derive from the module's Pydantic config model.

| Field | Type | Required | Default | Description |
|-------|------|:--------:|---------|-------------|
| `workspace` | str |  | `''` | Auto-injected by the daemon at module init time. Do NOT set manually in YAML - the daemon resolves it from the app's workspace/workspace_mode config. |
| `search_backend` | str |  | `'duckduckgo'` | Primary search backend (duckduckgo, brave, tavily, searxng, google) |
| `search_fallback` | str \| None |  | `None` | Fallback search backend |
| `user_agent` | str \| None |  | `None` | Custom User-Agent |
| `cache_ttl` | float |  | `900.0` | Fetch cache TTL in seconds (default 15 minutes) |
| `max_content_length` | int |  | `50000` | Max content length from fetched pages |
| `fetch_timeout` | float |  | `30.0` | HTTP fetch timeout in seconds |

## Actions

| Action | Short name | Internal | Risk | One-liner |
|--------|-----------|:--------:|------|-----------|
| `search` | `WebSearch` |  | low | Search the web for information. |
| `fetch` | `WebFetch` |  | low | Fetch a web page and return its content as text. |
| `extract` | `WebExtract` |  | low | Extract content from a web page using CSS selectors. Internal - use Fetch(extract=true) instead. |
| `download` | `WebDownload` |  | medium | Download a file from a URL to a local path. Supports large files with streaming. Returns the file size in bytes. The ... |

## Grant (in `capabilities.grant`)

Full-app grant (every visible action):

```yaml
capabilities:
  grant:
    - module: web
      actions: [search, fetch, extract, download]
```

Per-specialist grant (under `agents[].modules`):

```yaml
agents:
  - id: my-agent
    modules:
      - {web: [search, fetch, extract, download]}
```

## Per-action cards

For the full parameter spec of each action, see the auto-generated cards in `knowledge_base/modules/web-*.md`.
