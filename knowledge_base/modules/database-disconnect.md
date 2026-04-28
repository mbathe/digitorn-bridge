---
id: database-disconnect
title: "database.disconnect (DbDisconnect)"
type: module-action
module: database
action: disconnect
fqn: database.disconnect
short_name: DbDisconnect
keywords: [database, disconnect, dbdisconnect, connection, lifecycle]
permissions: [database:admin]
risk_level: low
irreversible: false
require_approval: false
---

# database.disconnect (DbDisconnect)

## Description
Close a named database connection. Use connection_id='*' to close all. Example: disconnect(connection_id='main')

## Parameters
| Name | Type | Required | Default | Description |
|------|------|:--------:|---------|-------------|
| `connection_id` | string | ✓ | - | Connection to close. Use '*' to close all connections. |

## Capability grant (in app YAML)
```yaml
capabilities:
  grant:
    - module: database
      actions: [disconnect]
```

## Safety
- Required permissions: `database:admin`
- Risk level: **low**
