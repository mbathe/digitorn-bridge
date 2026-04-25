# Web Module — Action Reference

Complete reference for all 4 actions exposed by the web module.

Uses aiohttp for async HTTP, html2text + BeautifulSoup for content parsing, and multiple search backends with DuckDuckGo as the free default.

---

## search

Search the web for information. Returns results with title, URL, and snippet.

**Risk level:** Low

### Parameters

| Name | Type | Required | Default | Description |
|------|------|----------|---------|-------------|
| `query` | string | yes | — | Search query |
| `limit` | integer | no | 5 | Max results (1-20) |

### Returns

```json
{
  "query": "python asyncio",
  "results": [
    {"title": "Async IO in Python", "url": "https://...", "snippet": "A walkthrough..."}
  ],
  "count": 5,
  "backend": "duckduckgo"
}
```

---

## fetch

Fetch a web page and convert HTML to clean readable text (markdown-like). Strips scripts, ads, navigation, cookies banners.

**Risk level:** Low

### Parameters

| Name | Type | Required | Default | Description |
|------|------|----------|---------|-------------|
| `url` | string | yes | — | URL to fetch |
| `max_length` | integer | no | 50000 | Max content length in characters |
| `raw` | bool | no | false | Return raw HTML instead of parsed text |

### Returns

```json
{
  "url": "https://...",
  "title": "Page Title",
  "description": "Meta description",
  "content": "# Page Title\n\nClean content...",
  "length": 4823,
  "cached": false
}
```

---

## extract

Extract specific content from a web page using CSS selectors.

**Risk level:** Low

### Parameters

| Name | Type | Required | Default | Description |
|------|------|----------|---------|-------------|
| `url` | string | yes | — | URL to extract from |
| `selector` | string | no | `main, article, .content, #content, body` | CSS selector |
| `max_length` | integer | no | 30000 | Max content length |

---

## download

Download a file from a URL to a local path.

**Risk level:** Medium

### Parameters

| Name | Type | Required | Default | Description |
|------|------|----------|---------|-------------|
| `url` | string | yes | — | URL to download |
| `path` | string | yes | — | Local file path to save to |

### Returns

```json
{
  "url": "https://example.com/data.csv",
  "path": "/tmp/data.csv",
  "size_bytes": 102400
}
```
