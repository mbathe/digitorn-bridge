---
id: channels-list-providers
title: "channels.list_providers (ChannelsListProviders)"
type: module-action
module: channels
action: list_providers
fqn: channels.list_providers
short_name: ChannelsListProviders
keywords: [channels, list_providers, channelslistproviders, info, lister_canaux, list_channels]
permissions: []
risk_level: low
irreversible: false
require_approval: false
---

# channels.list_providers (ChannelsListProviders)

## Description
List all configured channel providers and their status.

## Parameters
| Name | Type | Required | Default | Description |
|------|------|:--------:|---------|-------------|
| `include_status` | boolean |  | `True` | Include runtime status for each provider. |

## Capability grant (in app YAML)
```yaml
capabilities:
  grant:
    - module: channels
      actions: [list_providers]
```

## Aliases
`lister_canaux`, `list_channels`

## Safety
- Risk level: **low**
