---
id: http-fetch-page
title: "http.fetch_page (HttpFetchPage)"
type: module-action
module: http
action: fetch_page
fqn: http.fetch_page
short_name: HttpFetchPage
keywords: [http, fetch_page, httpfetchpage, scrape, html, read, page_web, scraper, extraire_page, lire_page, web_page]
permissions: [net.http]
risk_level: low
irreversible: false
require_approval: false
---

# http.fetch_page (HttpFetchPage)

## Description
Fetch a web page and extract readable text from HTML. Strips scripts, styles, and navigation. Returns text with basic structure, page title, and links found on the page.

## Parameters
| Name | Type | Required | Default | Description |
|------|------|:--------:|---------|-------------|
| `url` | string | ✓ | - | Page URL to fetch. |
| `headers` | object |  | - | Custom request headers. |
| `timeout` | number |  | `30.0` | Request timeout in seconds. |
| `verify_tls` | boolean |  | `True` | Verify TLS certificates. |
| `max_response_bytes` | integer |  | `5000000` | Max HTML to download. |
| `extract_links` | boolean |  | `True` | Include list of links found on the page. |
| `max_text_length` | integer |  | `50000` | Truncate extracted text to this many characters. |

## Capability grant (in app YAML)
```yaml
capabilities:
  grant:
    - module: http
      actions: [fetch_page]
```

## Aliases
`page_web`, `scraper`, `extraire_page`, `lire_page`, `web_page`

## Safety
- Required permissions: `net.http`
- Risk level: **low**
