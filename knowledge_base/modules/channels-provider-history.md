---
id: channels-provider-history
title: "channels.provider_history (ChannelsProviderHistory)"
type: module-action
module: channels
action: provider_history
fqn: channels.provider_history
short_name: ChannelsProviderHistory
keywords: [channels, provider_history, channelsproviderhistory, info, historique_canaux]
permissions: []
risk_level: low
irreversible: false
require_approval: false
---

# channels.provider_history (ChannelsProviderHistory)

## Description
Get recent event history for channels.

## Parameters
| Name | Type | Required | Default | Description |
|------|------|:--------:|---------|-------------|
| `provider` | string |  | `` | Provider name. Empty = all providers. |
| `limit` | integer |  | `20` | Max events to return. |
| `direction` | string |  | `all` | Filter by direction: 'inbound', 'outbound', or 'all'. |

## Capability grant (in app YAML)
```yaml
capabilities:
  grant:
    - module: channels
      actions: [provider_history]
```

## Aliases
`historique_canaux`

## Safety
- Risk level: **low**
