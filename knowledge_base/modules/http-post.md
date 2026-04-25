---
id: http-post
title: "http.post (HttpPost)"
type: module-action
module: http
action: post
fqn: http.post
short_name: HttpPost
keywords: [http, post, httppost, write, api, envoyer, poster, soumettre, api_post]
permissions: [net.http]
risk_level: medium
irreversible: false
require_approval: false
---

# http.post (HttpPost)

## Description
HTTP POST — send data to a URL with automatic JSON serialization.

## Parameters
| Name | Type | Required | Default | Description |
|------|------|:--------:|---------|-------------|
| `url` | string | ✓ | — | Target URL. |
| `headers` | object |  | — | Custom request headers. |
| `json_body` | object |  | — | JSON payload (auto-sets Content-Type). |
| `body` | string |  | — | Raw body (mutually exclusive with json_body). |
| `timeout` | number |  | `30.0` | Request timeout in seconds. |
| `verify_tls` | boolean |  | `True` | Verify TLS certificates. |

## Capability grant (in app YAML)
```yaml
capabilities:
  grant:
    - module: http
      actions: [post]
```

## Aliases
`envoyer`, `poster`, `soumettre`, `api_post`

## Safety
- Required permissions: `net.http`
- Risk level: **medium**
