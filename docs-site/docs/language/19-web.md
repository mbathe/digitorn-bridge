---
id: web
---

# Web Module

 `MODULE_ID = "web"`.
Provides web search, page fetch, content extraction, and file
download. Default search backend is DuckDuckGo - no API key
needed. Five backends are supported, with optional fallback chain.

Every action and field on this page maps to real code; entries
are cited with file + line.

## Module declaration

```yaml
tools:
  modules:
    web:
      config:
        backend: duckduckgo            # default; see below for the five options
        fallback: brave                # optional secondary if backend fails
        timeout: 30                    # seconds (default 30)
        api_keys:
          brave:    "{{secret.BRAVE_API_KEY}}"
          tavily:   "{{secret.TAVILY_API_KEY}}"
          google:   "{{secret.GOOGLE_API_KEY}}"
          searxng:  "https://my-searxng.example.com"   # base URL, no key
        allowed_domains: []             # global default whitelist
        blocked_domains: []             # global default blacklist
```

The full `ModuleBlock` shape (`config`, `setup`, `constraints`,
`middleware`, `credential`) is in
[App Configuration → tools.modules](02-app-config.md#toolsmodules---module-configuration).

## The 4 LLM-exposed actions

| Action | Short alias | Purpose |
|--------|-------------|---------|
| `web.search` | `WebSearch` | Web search via the configured backend. |
| `web.fetch` | `WebFetch` | Fetch a web page and return clean readable text. |
| `web.extract` | - | Extract structured data (links, headings, tables, ...) from HTML. |
| `web.download` | - | Download a file to disk. |

Short aliases come from ### `web.search`

Params (`SearchParams`):

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `query` | string | *required* | Search query. |
| `limit` | int | `10` | Max number of results. |
| `allowed_domains` | list[string] | `[]` | Per-call domain whitelist (overrides app-level). |
| `blocked_domains` | list[string] | `[]` | Per-call domain blacklist. Mutually exclusive with `allowed_domains`. |

Returns `{ query, results: [{title, url, snippet}, ...], count,
backend, sources: [url, ...] }`. When the primary backend fails,
the runtime falls back to the secondary backend (`config.fallback`)
if configured; the response includes a
`note: "Primary backend X failed, used fallback"`.

```json
{"name": "WebSearch",
 "arguments": {"query": "python asyncio timeout handling",
               "limit": 5}}
```

### `web.fetch`

Fetches a URL and returns the body as **clean readable text**
(strips boilerplate via the `readability` heuristic). Returns
`{ url, text, title, content_type, status }`.

Common params: `url` (required), `format` (`text` / `html` /
`markdown`), `max_length` (truncate response).

```json
{"name": "WebFetch",
 "arguments": {"url": "https://docs.python.org/3/library/asyncio.html",
               "format": "markdown"}}
```

### `web.extract`

Extracts structured data from a page or raw HTML - links,
headings, tables, meta tags. Useful when `fetch` returns too much
prose and the agent only needs the navigation tree or a specific
table.

### `web.download`

Downloads a file to disk inside the workspace. Returns the
absolute path to the downloaded file plus content metadata
(MIME, size).

## Search backends

`_search_*` functions. Five backends are wired
into the module:

| Backend (config value) | Source method | API key | Notes |
|-----------------------|---------------|---------|-------|
| `duckduckgo` (default) | `_search_duckduckgo` | None | Free, no rate limit, basic results. |
| `brave` | `_search_brave` | `BRAVE_API_KEY` | Better quality than DDG. Free tier available. |
| `tavily` | `_search_tavily` | `TAVILY_API_KEY` | LLM-optimized; returns rich snippets. |
| `searxng` | `_search_searxng` | None (just a base URL) | Self-hosted meta-search. Set `api_keys.searxng` to the instance URL. |
| `google` | `_search_google` | `GOOGLE_API_KEY` + Custom Search Engine ID | Highest quality, costs per query. |

When `config.backend` fails (timeout, rate-limit, network error)
and `config.fallback` is set, the runtime retries on the fallback
and stamps the response with a note. If both fail, the action
returns `success: false` with a combined error message.

## Domain filtering

Three layers of allowlist / blocklist, applied in this order:

1. **Per-call** (`allowed_domains` / `blocked_domains` on
   `web.search`). Highest priority.
2. **App-level** (`config.allowed_domains` / `config.blocked_domains`
   on the module). Default for every call.
3. **None** - all domains pass.

Filtering happens **after** the backend returns the results
(`_filter_results_by_domain`); domain matching is
host-suffix-aware (`api.github.com` matches a `github.com`
allowlist entry).

`allowed_domains` and `blocked_domains` are mutually exclusive at
the call level - providing both raises an error.

## Example app

```yaml
app:
  app_id: research-bot
  name: "Research Bot"

runtime:
  mode: conversation

agents:
  - id: researcher
    role: assistant
    brain:
      provider: deepseek
      model: deepseek-chat
      backend: openai_compat
      config:
        api_key: "{{secret.DEEPSEEK_API_KEY}}"
    system_prompt: |
      You research topics. Use WebSearch to find sources, then
      WebFetch the 2-3 most relevant URLs. Cite every claim with
      the source URL. Use Remember to keep important findings
      across the conversation.

tools:
  modules:
    web:
      config:
        search_backend: brave           # primary engine
        search_fallback: duckduckgo     # used if the primary fails
        cache_ttl: 600.0                # seconds, search cache
        fetch_timeout: 30.0             # seconds per fetch
    memory:
      config:
        working_memory: true

  capabilities:
    grant:
      - { module: web, actions: [search, fetch] }
      - { module: memory, actions: [remember] }

dev:
  variables: {}
```

## Cross-references

- Block reference (every module field):
  [App Configuration → tools.modules](02-app-config.md#toolsmodules---module-configuration)
- Built-in tool short aliases (`WebSearch`, `WebFetch`):
  [Built-in Tools → other short-name aliases](04b-builtin-tools.md#other-short-name-aliases)
- Per-module deep reference (sandbox, middleware,
  troubleshooting):
  [modules/reference/web.md](../reference/modules/web.md)
- Capabilities (granting search vs fetch vs download):
  [Security](11-security.md)
