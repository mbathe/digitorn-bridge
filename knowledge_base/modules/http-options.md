---
id: http-options
title: "http.options (HttpOptions)"
type: module-action
module: http
action: options
fqn: http.options
short_name: HttpOptions
keywords: [http, options, httpoptions, read, metadata, methodes_autorisees, cors_check]
permissions: [net.http]
risk_level: low
irreversible: false
require_approval: false
---

# http.options (HttpOptions)

## Description
HTTP OPTIONS — discover allowed methods and CORS configuration for a URL.

## Parameters
| Name | Type | Required | Default | Description |
|------|------|:--------:|---------|-------------|
| `url` | string | ✓ | — | Target URL. |
| `headers` | object |  | — | Custom request headers. |
| `timeout` | number |  | `15.0` | Request timeout in seconds. |
| `verify_tls` | boolean |  | `True` | Verify TLS certificates. |

## Capability grant (in app YAML)
```yaml
capabilities:
  grant:
    - module: http
      actions: [options]
```

## Aliases
`methodes_autorisees`, `cors_check`

## Safety
- Required permissions: `net.http`
- Risk level: **low**
