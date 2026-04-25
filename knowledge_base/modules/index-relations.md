---
id: index-relations
title: "index.relations (IndexRelations)"
type: module-action
module: index
action: relations
fqn: index.relations
short_name: IndexRelations
keywords: [index, relations, indexrelations, graph]
permissions: [index:read]
risk_level: low
irreversible: false
require_approval: false
---

# index.relations (IndexRelations)

## Description
Explore the relation graph from an entry. Shows what an entry imports/calls/references and what references it.

## Parameters
| Name | Type | Required | Default | Description |
|------|------|:--------:|---------|-------------|
| `entry_id` | string | ✓ | — | Entry ID to get relations for (from a previous query result). |
| `direction` | string |  | `both` | Direction: 'in' (who references me), 'out' (what I reference), 'both'. |
| `kind` | string |  | — | Filter by relation kind: 'imports', 'calls', 'contains', 'inherits', etc. |
| `depth` | integer |  | `1` | Traversal depth in the relation graph. |

## Capability grant (in app YAML)
```yaml
capabilities:
  grant:
    - module: index
      actions: [relations]
```

## Safety
- Required permissions: `index:read`
- Risk level: **low**
