---
id: http-json-api
title: "http.json_api (HttpJsonApi)"
type: module-action
module: http
action: json_api
fqn: http.json_api
short_name: HttpJsonApi
keywords: [http, json_api, httpjsonapi, api, json, api_json, appel_api_json, rest_api, api_call]
permissions: [net.http]
risk_level: medium
irreversible: false
require_approval: false
---

# http.json_api (HttpJsonApi)

## Description
Call a JSON API endpoint. Auto-sends Accept: application/json, parses JSON response, supports Bearer token auth. The easiest way to call REST APIs.

## Parameters
| Name | Type | Required | Default | Description |
|------|------|:--------:|---------|-------------|
| `url` | string | ✓ | - | API endpoint URL. |
| `method` | string |  | `GET` | HTTP method. |
| `data` | object |  | - | Request payload (auto-serialized to JSON). |
| `headers` | object |  | - | Custom request headers. |
| `query_params` | object |  | - | URL query parameters. |
| `auth_bearer` | string |  | - | Bearer token (auto-builds Authorization header). |
| `timeout` | number |  | `30.0` | Request timeout in seconds. |
| `verify_tls` | boolean |  | `True` | Verify TLS certificates. |

## Capability grant (in app YAML)
```yaml
capabilities:
  grant:
    - module: http
      actions: [json_api]
```

## Aliases
`api_json`, `appel_api_json`, `rest_api`, `api_call`

## Safety
- Required permissions: `net.http`
- Risk level: **medium**
