---
id: http-request
title: "http.request (HttpRequest)"
type: module-action
module: http
action: request
fqn: http.request
short_name: HttpRequest
keywords: [http, request, httprequest, api, requete, requete_http, fetch, curl, appel_api]
permissions: [net.http]
risk_level: medium
irreversible: false
require_approval: false
---

# http.request (HttpRequest)

## Description
Make an HTTP request with full control over method, headers, body, query params, and authentication. Universal action for any HTTP call.

## Parameters
| Name | Type | Required | Default | Description |
|------|------|:--------:|---------|-------------|
| `url` | string | ✓ | - | Target URL (http:// or https://). |
| `method` | string |  | `GET` | HTTP method: GET, POST, PUT, PATCH, DELETE, HEAD, OPTIONS. |
| `headers` | object |  | - | Custom request headers. |
| `body` | string |  | - | Raw request body. |
| `json_body` | object |  | - | JSON body (auto-sets Content-Type: application/json). |
| `query_params` | object |  | - | URL query parameters. |
| `timeout` | number |  | `30.0` | Request timeout in seconds. |
| `follow_redirects` | boolean |  | `True` | Follow HTTP redirects. |
| `max_redirects` | integer |  | `10` | Maximum redirect hops. |
| `verify_tls` | boolean |  | `True` | Verify TLS certificates. |
| `max_response_bytes` | integer |  | `5000000` | Max response body to read (default 5 MB). |

## Capability grant (in app YAML)
```yaml
capabilities:
  grant:
    - module: http
      actions: [request]
```

## Aliases
`requete`, `requete_http`, `fetch`, `curl`, `appel_api`

## Safety
- Required permissions: `net.http`
- Risk level: **medium**
