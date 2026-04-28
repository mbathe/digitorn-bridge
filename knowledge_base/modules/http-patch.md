---
id: http-patch
title: "http.patch (HttpPatch)"
type: module-action
module: http
action: patch
fqn: http.patch
short_name: HttpPatch
keywords: [http, patch, httppatch, write, api, modifier_partiel, api_patch]
permissions: [net.http]
risk_level: medium
irreversible: false
require_approval: false
---

# http.patch (HttpPatch)

## Description
HTTP PATCH - partially update a resource at the target URL.

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
      actions: [patch]
```

## Aliases
`modifier_partiel`, `api_patch`

## Safety
- Required permissions: `net.http`
- Risk level: **medium**
