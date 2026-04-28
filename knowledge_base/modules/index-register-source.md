---
id: index-register-source
title: "index.register_source (IndexRegisterSource)"
type: module-action
module: index
action: register_source
fqn: index.register_source
short_name: IndexRegisterSource
keywords: [index, register_source, indexregistersource, config]
permissions: [index:admin]
risk_level: low
irreversible: false
require_approval: false
---

# index.register_source (IndexRegisterSource)

## Description
Register a data source to be indexed. Sources are owned by a specific module (filesystem, database, etc.). After registration, call 'scan' to populate the index.

## Parameters
| Name | Type | Required | Default | Description |
|------|------|:--------:|---------|-------------|
| `source_id` | string | ✓ | - | Unique identifier for this source (e.g. 'backend-project', 'crm-db'). |
| `module_id` | string | ✓ | - | Module responsible for reading this source. For filesystem sources use 'filesystem', for databases use 'database', etc. |
| `root` | string | ✓ | - | Root path or URI of the source (e.g. '/home/user/project', 'postgres://...'). |
| `extractor` | string |  | `auto` | Extractor to use: 'auto' (detect from content), 'text' (simple), 'python' (AST-based), or a custom extractor registered by another module. |
| `scan_pattern` | string |  | `**/*` | Glob/filter pattern for scanning (e.g. '**/*.py', 'public.*'). |
| `metadata` | object |  | - | Extra config passed to the extractor (e.g. encoding, schema). |
| `watch` | boolean |  | `False` | Enable automatic change detection on this source. When true, the watcher service monitors the source and automatically re-indexes changed content. |
| `watch_mode` | string |  | `ephemeral` | Watch lifecycle mode: 'ephemeral' - watch stops when the app disconnects. 'persistent' - watch survives daemon restarts. |

## Capability grant (in app YAML)
```yaml
capabilities:
  grant:
    - module: index
      actions: [register_source]
```

## Safety
- Required permissions: `index:admin`
- Risk level: **low**
