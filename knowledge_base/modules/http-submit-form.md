---
id: http-submit-form
title: "http.submit_form (HttpSubmitForm)"
type: module-action
module: http
action: submit_form
fqn: http.submit_form
short_name: HttpSubmitForm
keywords: [http, submit_form, httpsubmitform, form, submit, formulaire, soumettre_formulaire, form_post]
permissions: [net.http]
risk_level: medium
irreversible: false
require_approval: false
---

# http.submit_form (HttpSubmitForm)

## Description
Submit an HTML form (application/x-www-form-urlencoded). Auto-encodes key-value pairs.

## Parameters
| Name | Type | Required | Default | Description |
|------|------|:--------:|---------|-------------|
| `url` | string | ✓ | — | Form action URL. |
| `fields` | object | ✓ | — | Form field key-value pairs. |
| `method` | string |  | `POST` | HTTP method (POST or PUT). |
| `headers` | object |  | — | Custom request headers. |
| `timeout` | number |  | `30.0` | Request timeout in seconds. |
| `verify_tls` | boolean |  | `True` | Verify TLS certificates. |

## Capability grant (in app YAML)
```yaml
capabilities:
  grant:
    - module: http
      actions: [submit_form]
```

## Aliases
`formulaire`, `soumettre_formulaire`, `form_post`

## Safety
- Required permissions: `net.http`
- Risk level: **medium**
