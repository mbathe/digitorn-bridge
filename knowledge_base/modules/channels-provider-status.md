---
id: channels-provider-status
title: "channels.provider_status (ChannelsProviderStatus)"
type: module-action
module: channels
action: provider_status
fqn: channels.provider_status
short_name: ChannelsProviderStatus
keywords: [channels, provider_status, channelsproviderstatus, info]
permissions: []
risk_level: low
irreversible: false
require_approval: false
---

# channels.provider_status (ChannelsProviderStatus)

## Description
Get detailed status of a specific provider.

## Parameters
| Name | Type | Required | Default | Description |
|------|------|:--------:|---------|-------------|
| `provider` | string | ✓ | — | Provider instance name. |

## Capability grant (in app YAML)
```yaml
capabilities:
  grant:
    - module: channels
      actions: [provider_status]
```

## Safety
- Risk level: **low**
