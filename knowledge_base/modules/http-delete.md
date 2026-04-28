---
id: http-delete
title: "http.delete (HttpDelete)"
type: module-action
module: http
action: delete
fqn: http.delete
short_name: HttpDelete
keywords: [http, delete, httpdelete, api, supprimer_distant, api_delete, effacer_ressource]
permissions: [net.http]
risk_level: medium
irreversible: true
require_approval: false
---

# http.delete (HttpDelete)

## Description
HTTP DELETE - remove a resource at the target URL.

## Parameters
| Name | Type | Required | Default | Description |
|------|------|:--------:|---------|-------------|
| `url` | string | ✓ | - | Target URL. |
| `headers` | object |  | - | Custom request headers. |
| `query_params` | object |  | - | URL query parameters. |
| `timeout` | number |  | `30.0` | Request timeout in seconds. |
| `verify_tls` | boolean |  | `True` | Verify TLS certificates. |

## Capability grant (in app YAML)
```yaml
capabilities:
  grant:
    - module: http
      actions: [delete]
```

## Aliases
`supprimer_distant`, `api_delete`, `effacer_ressource`

## Safety
- Required permissions: `net.http`
- Risk level: **medium**
- ⚠️ **Irreversible** - cannot be undone once executed
