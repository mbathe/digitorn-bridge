---
id: context_builder-call-app
title: "context_builder.call_app (CallApp)"
type: module-action
module: context_builder
action: call_app
fqn: context_builder.call_app
short_name: CallApp
keywords: [context_builder, call_app, callapp, composition, pipeline, orchestration]
permissions: []
risk_level: medium
irreversible: false
require_approval: false
---

# context_builder.call_app (CallApp)

## Description
Call another deployed Digitorn app and return its result. The target app must be deployed on the daemon and in one_shot mode. Use this to compose multiple apps into a pipeline. Example: call_app(app_id='code-analyzer', input='src/auth.py')

## Parameters
| Name | Type | Required | Default | Description |
|------|------|:--------:|---------|-------------|
| `app_id` | string | ✓ | — | The app_id of the deployed app to call. |
| `input` | string | ✓ | — | The input to send to the app. |
| `timeout` | number |  | `120.0` | Timeout in seconds. |

## Capability grant (in app YAML)
```yaml
capabilities:
  grant:
    - module: context_builder
      actions: [call_app]
```

## Safety
- Risk level: **medium**
