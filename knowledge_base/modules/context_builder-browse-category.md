---
id: context_builder-browse-category
title: "context_builder.browse_category (BrowseCategory)"
type: module-action
module: context_builder
action: browse_category
fqn: context_builder.browse_category
short_name: BrowseCategory
keywords: [context_builder, browse_category, browsecategory, discovery, navigation]
permissions: []
risk_level: low
irreversible: false
require_approval: false
---

# context_builder.browse_category (BrowseCategory)

## Description
Browse all tools in a specific category (module). Shows tool names, descriptions, and risk levels. Paginated (20 tools per page).

## Parameters
| Name | Type | Required | Default | Description |
|------|------|:--------:|---------|-------------|
| `category` | string | ✓ | - | Category (module) ID to browse (e.g. 'database', 'filesystem', 'browser'). |
| `page` | integer |  | `1` | Page number for pagination (20 tools per page). |

## Capability grant (in app YAML)
```yaml
capabilities:
  grant:
    - module: context_builder
      actions: [browse_category]
```

## Safety
- Risk level: **low**
