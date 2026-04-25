---
id: web-extract
title: "web.extract (WebExtract)"
type: module-action
module: web
action: extract
fqn: web.extract
short_name: WebExtract
keywords: [web, extract, webextract, internal]
permissions: []
risk_level: low
irreversible: false
require_approval: false
---

# web.extract (WebExtract)

## Description
Extract content from a web page using CSS selectors. Internal — use Fetch(extract=true) instead.

## Parameters
| Name | Type | Required | Default | Description |
|------|------|:--------:|---------|-------------|
| `url` | string | ✓ | — | URL to extract from. |
| `selector` | string |  | `main, article, .content, #content, body` |  |
| `max_length` | integer |  | `30000` |  |

## Capability grant (in app YAML)
```yaml
capabilities:
  grant:
    - module: web
      actions: [extract]
```

## Safety
- Risk level: **low**
