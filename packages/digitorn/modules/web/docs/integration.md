# Web Module - Integration Guide

## Configuration

```yaml
modules:
  web:
    config:
      search:
        primary: duckduckgo
        fallback: null
        api_keys:
          brave: "{{env.BRAVE_API_KEY}}"
          tavily: "{{env.TAVILY_API_KEY}}"
          google: "{{env.GOOGLE_API_KEY}}"
          google_cx: "{{env.GOOGLE_CX}}"
          searxng_url: "http://localhost:8080"
      max_content_length: 50000
      user_agent: "Digitorn/1.0"
      cache_ttl: 300
      cache_max_size: 50
```

## Search Backends

| Backend | API Key | Cost | Notes |
|---------|---------|------|-------|
| `duckduckgo` | No | Free | Default. HTML scraping via POST. |
| `brave` | `brave` | $0.01/query | Fast API. Good quality. |
| `tavily` | `tavily` | $0.01/query | AI-optimized results. Best for agents. |
| `searxng` | `searxng_url` | Free (self-host) | Meta-search. Aggregates multiple engines. |
| `google` | `google` + `google_cx` | 100 free/day | Highest quality. Google Custom Search. |

### Fallback

If the primary backend fails, the module retries with the fallback:

```yaml
search:
  primary: brave
  fallback: duckduckgo
```

## HTML Parsing

Content is parsed in two modes:

1. **html2text** (default for `fetch`): Converts HTML to markdown-like text. Removes noise tags (script, style, nav, footer, ads).
2. **BeautifulSoup** (for `extract`): CSS selector-based extraction. Targets specific content areas.

## Caching

Fetched pages are cached in memory (300s TTL, 50 max pages). Benefits:
- `fetch` then `extract` on the same URL reuses cached HTML
- Repeated fetches are instant (~0.1ms vs ~800ms)
- Write operations don't invalidate (web content is external)

## Dependencies

- **aiohttp**: Async HTTP client (required)
- **beautifulsoup4**: HTML parsing for search results and `extract` (required)
- **html2text**: HTML to markdown conversion for `fetch` (required)

Install: `pip install aiohttp beautifulsoup4 html2text`

## Security

All actions are low risk except `download` (medium - writes to filesystem). The module never sends user data to search engines beyond the query string. API keys are passed via headers, not URL parameters.
