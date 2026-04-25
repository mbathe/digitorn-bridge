---
id: web
---

# Web Module

Web search, fetch, and content extraction with multiple search backends. DuckDuckGo is the free default -- no API key needed.

## Configuration

```yaml
modules:
  web:
    config:
      # Search backend (default: duckduckgo)
      search:
        primary: duckduckgo        # duckduckgo | brave | tavily | searxng | google
        fallback: null              # optional fallback backend
        api_keys:
          brave: "{{env.BRAVE_API_KEY}}"
          tavily: "{{env.TAVILY_API_KEY}}"
          google: "{{env.GOOGLE_API_KEY}}"
          google_cx: "{{env.GOOGLE_CX}}"
          searxng_url: "http://localhost:8080"

      # Content settings
      max_content_length: 50000    # max chars per fetched page (default: 50k)
      user_agent: "Digitorn/1.0"  # HTTP User-Agent header

      # Cache
      cache_ttl: 300               # page cache TTL in seconds (default: 5 min)
      cache_max_size: 50           # max cached pages (default: 50)
```
## Search Backends

| Backend | Quality | Speed | Cost | API Key | Best for |
|---------|---------|-------|------|---------|----------|
| **DuckDuckGo** | Good | ~1s | Free | No | Development, testing |
| **Brave** | Good | ~400ms | $0.01/query | Yes | Production, affordable |
| **Tavily** | Excellent | ~500ms | $0.01/query | Yes | AI agents (structured results) |
| **SearXNG** | Excellent | ~300ms | Free (self-hosted) | No | Meta-search (aggregates Google+Bing) |
| **Google CSE** | Best | ~200ms | 100 free/day | Yes | Highest quality results |

DuckDuckGo is the default because it requires no API key and no configuration. For production use, Tavily is recommended as it returns AI-optimized results.

## Actions (4)

### search

Search the web. Returns results with title, URL, and snippet.

```
search(query="python asyncio tutorial", limit=5)
```

Response:
```json
{
  "query": "python asyncio tutorial",
  "results": [
    {"title": "Async IO in Python", "url": "https://...", "snippet": "A walkthrough..."}
  ],
  "count": 5,
  "backend": "duckduckgo"
}
```

### fetch

Fetch a web page and convert to clean readable text (markdown-like). Strips scripts, ads, navigation.

```
fetch(url="https://realpython.com/async-io-python/", max_length=5000)
```

Response:
```json
{
  "url": "https://...",
  "title": "Async IO in Python",
  "description": "A hands-on walkthrough...",
  "content": "# Async IO in Python\n\nAsync IO is a...",
  "length": 4823,
  "cached": false
}
```

### extract

Extract specific content from a page using CSS selectors.

```
extract(url="https://...", selector="article, .main-content", max_length=10000)
```

### download

Download a file to a local path.

```
download(url="https://example.com/data.csv", path="/tmp/data.csv")
```

## HTML Parsing

The module converts HTML to clean text using two strategies:

1. **html2text** (default) -- converts HTML to markdown-like output. Good for articles, docs, blog posts.
2. **BeautifulSoup** (CSS selectors) -- targeted extraction. Good for structured pages.

Noise removal:
- Strips `<script>`, `<style>`, `<nav>`, `<footer>`, `<header>`, `<noscript>`, `<svg>`, `<iframe>`, `<form>`
- Removes common ad/cookie selectors (`.cookie-banner`, `.ad`, `#sidebar`, etc.)
- Collapses excessive whitespace

## Response Caching

Fetched pages are cached in memory for 5 minutes (configurable). Benefits:

- Same URL fetched twice: second call is instant (~0.1ms vs ~800ms)
- `fetch` then `extract` on the same URL: HTML is reused from cache
- Automatic eviction: oldest entry removed when cache is full
- Cache is per-module-instance (no cross-session pollution)

## Fallback Mechanism

If the primary search backend fails (timeout, API error, rate limit), the module automatically retries with the configured fallback:

```yaml
search:
  primary: brave
  fallback: duckduckgo
```
The response includes a `note` field when fallback was used:
```json
{"note": "Primary backend 'brave' failed, used fallback"}
```
