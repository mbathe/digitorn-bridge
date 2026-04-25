---
id: channels-resume-provider
title: "channels.resume_provider (ChannelsResumeProvider)"
type: module-action
module: channels
action: resume_provider
fqn: channels.resume_provider
short_name: ChannelsResumeProvider
keywords: [channels, resume_provider, channelsresumeprovider, admin, reprendre_canal]
permissions: []
risk_level: medium
irreversible: false
require_approval: false
---

# channels.resume_provider (ChannelsResumeProvider)

## Description
Resume a paused provider's inbound listener.

## Parameters
| Name | Type | Required | Default | Description |
|------|------|:--------:|---------|-------------|
| `provider` | string | ✓ | — | Provider instance name to resume. |

## Capability grant (in app YAML)
```yaml
capabilities:
  grant:
    - module: channels
      actions: [resume_provider]
```

## Aliases
`reprendre_canal`

## Safety
- Risk level: **medium**
