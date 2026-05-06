---
id: web
title: web Module
sidebar_label: web
sidebar_position: 11
description: Web search, fetch, extract, download — multi-backend, DuckDuckGo free default, SSRF-guarded.
---

# web

Web search, fetch, content extraction, and downloads. Five
backends with automatic fallback. DuckDuckGo is the free
default — no API key required.

| Property | Value | Source |
|----------|-------|--------|
| Module id | `web` | `module.py:96` |
| Version | `1.0.0` | `module.py:97` |
| Type | user | |
| Config model | `WebConfig` | `module.py:79-90` |
| Pip deps | `aiohttp`, `beautifulsoup4`, `html2text` | |

## Design notes

- **Free by default** — DuckDuckGo works out of the box.
- **Clean content** — HTML → markdown-like text via `html2text`;
  scripts, ads, navigation, cookie banners stripped.
- **Cached fetches** — 15 min default TTL, 100 URL capacity
  (LRU). Same URL twice = one HTTP request.
- **Fallback resilience** — if `search_backend` fails, the
  module retries with `search_fallback` and tags the result
  with a `note: "Primary backend ... failed, used fallback"`.
- **SSRF-guarded** — outbound requests go through
  `modules/http/security.py` (private-network blocklist + DNS
  pinning, see [Production Deployment → SSRF](../../app-language/36-production.md#ssrf-protection)).

## Search backends

| Backend | API key | Cost | Best for |
|---------|---------|------|----------|
| `duckduckgo` *(default)* | no | free | dev / testing. |
| `brave` | yes | ~$0.01/q | production, affordable. |
| `tavily` | yes | ~$0.01/q | AI-agent-shaped structured results. |
| `searxng` | no (self-host) | free | meta-search across many engines. |
| `google` | yes + CX | 100 free / day | highest quality. |

## Configuration

`WebConfig` (`module.py:72-90`, `extra: forbid`):

```yaml
tools:
  modules:
    web:
      config:
        search_backend: duckduckgo     # duckduckgo | brave | tavily | searxng | google
        search_fallback: brave         # used if search_backend fails
        max_content_length: 50000      # 1000..1_000_000
        cache_ttl: 900                  # seconds (default 15 min)
        fetch_timeout: 30               # 1..300 seconds
        user_agent: "MyBot/1.0"         # optional override
```

API keys for `brave`, `tavily`, `google`, etc. are **not** in
the YAML — store them in the credentials vault and reference
via `credential:` (or fall back to `{{secret.X}}` /
`{{env.X}}`). Outbound allowlist / blocklist live under
`constraints:` (not `config.egress`); see below.

## The 4 actions

`module.py`. All `risk_level: low` except `download`
(`risk_level: medium`).

| Tool | Source | Purpose |
|------|--------|---------|
| `web.search` | `:208` | Search the web. Returns `{title, url, snippet}` per result + a `sources: [url, ...]` field for easy citation. |
| `web.fetch` | `:313` | Fetch a page → clean readable text. HTTP→HTTPS auto-upgrade. Cross-host redirect detected (returns redirect URL, doesn't silently follow). Binary content (PDF / image) → suggests `download` + then read. |
| `web.extract` | `:511` | Extract content using CSS selectors. *Internal* — prefer `fetch(extract=true)`. |
| `web.download` | `:547` | Download a file to a local path (per-app workspace). |

### `web.search` — params

| Param | Default | Notes |
|-------|---------|-------|
| `query` | required | Text query. |
| `limit` | `10` | Max results. |
| `allowed_domains` | `null` | Per-call domain allowlist. |
| `blocked_domains` | `null` | Per-call domain blocklist. |

`allowed_domains` and `blocked_domains` are **mutually
exclusive** per call (`module.py:244-248`). Combine module-level
`egress.allowed_domains` with per-call to layer enforcement.

### `web.fetch` — params

| Param | Default | Notes |
|-------|---------|-------|
| `url` | required | Auto-upgraded to HTTPS. |
| `max_length` | config | Caps text returned. |
| `extract` | `false` | Main-content extraction (article body, strips nav / footer). Delegates to `extract`. |
| `prompt` | `""` | Hint to focus extraction on a specific section. |
| `raw` | `false` | Return raw HTML instead of converted text. |

## Constraints

`module.py:100-103`. Two universal constraints (apply across
every action):

| Constraint | Type | Description |
|------------|------|-------------|
| `allowed_domains` | `string_list` | Restrict every web call to these domains. |
| `blocked_domains` | `string_list` | Block these domains from every call. |

```yaml
tools:
  modules:
    web:
      constraints:
        allowed_domains: [docs.python.org, stackoverflow.com]
        blocked_domains: [malware.example.com]
      config:
        search_backend: duckduckgo
```

## Cross-references

- App-config block reference (`tools.modules.web`):
  [App Configuration → tools.modules](../../app-language/02-app-config.md#toolsmodules--module-config)
- HTTP module (lower-level GET / POST / WebSocket primitives):
  [http reference](http.md)
- SSRF + DNS pinning:
  [Production Deployment](../../app-language/36-production.md#ssrf-protection)
- Credentials vault (`{{credential.X}}`):
  [credentials.md](../../credentials.md)
