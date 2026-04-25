---
id: web-fetch
title: "web.fetch (WebFetch)"
type: module-action
module: web
action: fetch
fqn: web.fetch
short_name: WebFetch
keywords: [web, fetch, webfetch]
permissions: []
risk_level: low
irreversible: false
require_approval: false
---

# web.fetch (WebFetch)

## Description
Fetch a web page and return its content as text.

## Parameters
| Name | Type | Required | Default | Description |
|------|------|:--------:|---------|-------------|
| `url` | string | ✓ | — | URL to fetch. |
| `prompt` | string |  | `` | What to extract from the page. Leave empty for full page content. |
| `extract` | boolean |  | `False` | Extract main content only (removes nav, ads, etc). Default: false. |
| `max_length` | integer |  | `50000` |  |
| `raw` | boolean |  | `False` |  |
| `format` | string |  | `text` |  |

## Capability grant (in app YAML)
```yaml
capabilities:
  grant:
    - module: web
      actions: [fetch]
```

## Tool usage instructions
```
Fetch a web page and convert to clean readable text.

## When to use
- After Search — fetch the 2-3 most relevant URLs for full content
- Reading documentation pages, API references, tutorials
- Extracting specific data from a known URL

## When NOT to use
- Don't fetch every search result — scan snippets first, fetch only what's relevant
- Don't fetch the same URL twice — results are cached 5 minutes
- Don't fetch large download pages — use Bash with curl for raw downloads

## Modes
- Default: full page → markdown text (scripts/ads stripped)
- extract=true: main content only (article body, strips nav/footer)
- prompt='pricing table': hint to focus extraction on specific content

## Behavior
- After fetching, Remember the key facts — don't re-fetch later
- If a page is too long, use prompt to extract the relevant section
- Skip dead links, paywalls, and obviously spammy content
```

## Safety
- Risk level: **low**
