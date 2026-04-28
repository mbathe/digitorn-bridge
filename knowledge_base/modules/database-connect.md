---
id: database-connect
title: "database.connect (DbConnect)"
type: module-action
module: database
action: connect
fqn: database.connect
short_name: DbConnect
keywords: [database, connect, dbconnect, connection, lifecycle, connecter, ouvrir, connexion, base, bdd]
permissions: [database:admin]
risk_level: medium
irreversible: false
require_approval: false
---

# database.connect (DbConnect)

## Description
Open a named database connection. Supports SQLite, PostgreSQL, MySQL, MSSQL, and Oracle. The connection_id is used to reference this connection in all subsequent actions. Example: connect(connection_id='main', driver='sqlite', database='data.db')

## Parameters
| Name | Type | Required | Default | Description |
|------|------|:--------:|---------|-------------|
| `connection_id` | string | ✓ | - | Unique name for this connection (e.g. 'main', 'analytics', 'crm'). Used to reference the connection in all subsequent actions. |
| `driver` | string | ✓ | - | Database driver: 'sqlite', 'postgresql', 'mysql', 'mssql', 'oracle', 'mongodb', 'redis'. Alias: ``type``. |
| `database` | string |  | - | Database name or file path. Required for all drivers except SQLite (which defaults to ':memory:'). |
| `host` | string |  | - | Database host. Defaults to 'localhost' for network databases. |
| `port` | integer |  | - | Database port. Uses driver default if omitted. |
| `username` | string |  | - | Authentication username. |
| `password` | string |  | - | Authentication password. |
| `url` | string |  | - | Full SQLAlchemy async URL (e.g. 'postgresql+asyncpg://user:pass@host/db'). If provided, overrides host/port/database/username/password. |
| `options` | object |  | - | Engine options: pool_size (default 5), max_overflow (default 10), pool_recycle (default 3600), echo (default false). |
| `persist` | boolean |  | `False` | If true, save this connection config so it auto-reconnects on daemon restart. Passwords are stored as env var references (e.g. '$DB_PASSWORD'), never in plaintext. |
| `password_env` | string |  | - | Environment variable name containing the password (e.g. 'DB_PASSWORD'). Used for persistent connections instead of storing plaintext passwords. |
| `policy` | object |  | - | Security policy for this connection. Controls what operations are allowed. Keys: read_only (bool), blocked_statements (list), table_whitelist (list), table_blacklist (list), column_blacklist (dict)... |
| `role` | string |  | - | Connection role for read replica routing: 'primary' (read+write), 'replica' (read-only, auto-routed for SELECT queries). Omit for standalone connections. |
| `group` | string |  | - | Connection group name for replica routing. Connections in the same group are treated as a primary + replica set. E.g. group='production' with role='primary' and role='replica'. |

## Capability grant (in app YAML)
```yaml
capabilities:
  grant:
    - module: database
      actions: [connect]
```

## Aliases
`connecter`, `ouvrir`, `connexion`, `base`, `bdd`

## Safety
- Required permissions: `database:admin`
- Risk level: **medium**
