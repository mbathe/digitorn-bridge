---
id: database-list-connections
title: "database.list_connections (DbList)"
type: module-action
module: database
action: list_connections
fqn: database.list_connections
short_name: DbList
keywords: [database, list_connections, dblist, connection, info]
permissions: [database:read]
risk_level: low
irreversible: false
require_approval: false
---

# database.list_connections (DbList)

## Description
List all active database connections with metadata. Example: list_connections()

## Parameters
_(no parameters)_

## Capability grant (in app YAML)
```yaml
capabilities:
  grant:
    - module: database
      actions: [list_connections]
```

## Safety
- Required permissions: `database:read`
- Risk level: **low**
