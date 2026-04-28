---
id: http-put
title: "http.put (HttpPut)"
type: module-action
module: http
action: put
fqn: http.put
short_name: HttpPut
keywords: [http, put, httpput, write, api, remplacer, mettre_a_jour, api_put]
permissions: [net.http]
risk_level: medium
irreversible: false
require_approval: false
---

# http.put (HttpPut)

## Description
HTTP PUT - replace a resource at the target URL.

## Parameters
| Name | Type | Required | Default | Description |
|------|------|:--------:|---------|-------------|
| `url` | string | ✓ | - | Target URL. |
| `headers` | object |  | - | Custom request headers. |
| `json_body` | object |  | - | JSON payload. |
| `body` | string |  | - | Raw body. |
| `timeout` | number |  | `30.0` | Request timeout in seconds. |
| `verify_tls` | boolean |  | `True` | Verify TLS certificates. |

## Capability grant (in app YAML)
```yaml
capabilities:
  grant:
    - module: http
      actions: [put]
```

## Aliases
`remplacer`, `mettre_a_jour`, `api_put`

## Safety
- Required permissions: `net.http`
- Risk level: **medium**
