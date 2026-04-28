---
id: web
title: Web Module
sidebar_label: web
sidebar_position: 11
description: Web search, fetch, and content extraction with multi-backend search and DuckDuckGo free default.
---

# web

Web search, fetch, and content extraction. Supports multiple search backends with automatic fallback. DuckDuckGo is the free default - no API key required.

| Property | Value |
|----------|-------|
| **Module ID** | `web` |
| **Version** | `1.0.0` |
| **Type** | user |
| **Dependencies** | `aiohttp`, `beautifulsoup4`, `html2text` |

---

## Design Philosophy

- **Free by default** - DuckDuckGo works out of the box with no API key. Upgrade to Brave/Tavily/Google when needed.
- **Clean content** - HTML is converted to readable markdown-like text. Scripts, ads, navigation, and cookie banners are stripped.
- **Cached fetches** - pages are cached for 15 minutes (100 URL capacity). Same URL fetched twice costs one HTTP request, not two.
- **Fallback resilience** - if the primary search backend fails, automatically retries with the configured fallback.

---

## Configuration

```yaml
modules:
  web:
    config:
      search:
        primary: duckduckgo
        fallback: brave
        api_keys:
          brave: "{{env.BRAVE_API_KEY}}"
          tavily: "{{env.TAVILY_API_KEY}}"
      max_content_length: 50000
      cache_ttl: 900
```
### Search Backends

| Backend | API Key Required | Cost | Best For |
|---------|-----------------|------|----------|
| `duckduckgo` | No | Free | Development, testing |
| `brave` | Yes | ~$0.01/query | Production, affordable |
| `tavily` | Yes | ~$0.01/query | AI agents (structured results) |
| `searxng` | No (self-hosted) | Free | Meta-search (aggregates engines) |
| `google` | Yes + CX | 100 free/day | Highest quality results |

---

## Actions (4)

### search
Search the web. Returns title, URL, snippet for each result. Parameters: `query`, `limit`. **Risk: low**

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `allowed_domains` | list[string] | no | `null` | Only return results from these domains |
| `blocked_domains` | list[string] | no | `null` | Exclude results from these domains |

**New in v1.1:** Per-query domain filtering. Response includes `sources` field with URLs for easy citation.

### fetch
Fetch a page and convert HTML to clean readable text. Parameters: `url`, `max_length`, `raw`. **Risk: low**

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `prompt` | string | no | `""` | Describe what to extract - content is filtered to relevant sections |

**New in v1.1:** HTTP auto-upgrade to HTTPS. Cross-host redirect detection (returns redirect URL instead of silently following). Binary content detection (PDF, images → suggests `download()` + `read()`). Prompt-based content filtering. Cache increased to 15 minutes / 100 URLs.

### extract
Extract content using CSS selectors. Parameters: `url`, `selector`, `max_length`. **Risk: low**

### download
Download a file to a local path. Parameters: `url`, `path`. **Risk: medium**

---

## Constraints

| Constraint | Type | Description |
|------------|------|-------------|
| `allowed_domains` | string_list | Restrict web search and fetch to these domains only. |
| `blocked_domains` | string_list | Block these domains from search and fetch. |

### Example App YAML

```yaml
modules:
  - module: web
    constraints:
      allowed_domains: [docs.python.org, stackoverflow.com]
      blocked_domains: [malware.example.com]
```