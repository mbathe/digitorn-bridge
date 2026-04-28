---
id: vector-update-metadata
title: "vector.update_metadata (VectorUpdateMetadata)"
type: module-action
module: vector
action: update_metadata
fqn: vector.update_metadata
short_name: VectorUpdateMetadata
keywords: [vector, update_metadata, vectorupdatemetadata, write, modifier_metadata]
permissions: []
risk_level: low
irreversible: false
require_approval: false
---

# vector.update_metadata (VectorUpdateMetadata)

## Description
Update metadata for existing documents.

## Parameters
| Name | Type | Required | Default | Description |
|------|------|:--------:|---------|-------------|
| `collection` | string | ✓ | - | Collection name. |
| `ids` | array | ✓ | - | Document IDs to update. |
| `metadata` | object | ✓ | - | New metadata fields to set/merge. |

## Capability grant (in app YAML)
```yaml
capabilities:
  grant:
    - module: vector
      actions: [update_metadata]
```

## Aliases
`modifier_metadata`

## Safety
- Risk level: **low**
