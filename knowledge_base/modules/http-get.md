---
id: http-get
title: "http.get (HttpGet)"
type: module-action
module: http
action: get
fqn: http.get
short_name: HttpGet
keywords: [http, get, httpget, read, api, obtenir, recuperer, telecharger_page, fetch_url]
permissions: [net.http]
risk_level: low
irreversible: false
require_approval: false
---

# http.get (HttpGet)

## Description
HTTP GET — fetch a URL and auto-parse the response based on content type (JSON, text, HTML).

## Parameters
| Name | Type | Required | Default | Description |
|------|------|:--------:|---------|-------------|
| `url` | string | ✓ | — | Target URL. |
| `headers` | object |  | — | Custom request headers. |
| `query_params` | object |  | — | URL query parameters. |
| `timeout` | number |  | `30.0` | Request timeout in seconds. |
| `verify_tls` | boolean |  | `True` | Verify TLS certificates. |
| `max_response_bytes` | integer |  | `5000000` | Max response body to read. |

## Capability grant (in app YAML)
```yaml
capabilities:
  grant:
    - module: http
      actions: [get]
```

## Aliases
`obtenir`, `recuperer`, `telecharger_page`, `fetch_url`

## Safety
- Required permissions: `net.http`
- Risk level: **low**
