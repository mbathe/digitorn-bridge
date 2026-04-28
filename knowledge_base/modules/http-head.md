---
id: http-head
title: "http.head (HttpHead)"
type: module-action
module: http
action: head
fqn: http.head
short_name: HttpHead
keywords: [http, head, httphead, read, metadata, en_tete, verifier_url, check_url]
permissions: [net.http]
risk_level: low
irreversible: false
require_approval: false
---

# http.head (HttpHead)

## Description
HTTP HEAD - retrieve response headers without downloading the body. Useful for checking if a URL exists, getting content size, or last-modified timestamps.

## Parameters
| Name | Type | Required | Default | Description |
|------|------|:--------:|---------|-------------|
| `url` | string | ✓ | - | Target URL. |
| `headers` | object |  | - | Custom request headers. |
| `timeout` | number |  | `15.0` | Request timeout in seconds. |
| `verify_tls` | boolean |  | `True` | Verify TLS certificates. |

## Capability grant (in app YAML)
```yaml
capabilities:
  grant:
    - module: http
      actions: [head]
```

## Aliases
`en_tete`, `verifier_url`, `check_url`

## Safety
- Required permissions: `net.http`
- Risk level: **low**
