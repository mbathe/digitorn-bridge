---
id: dev_tools-app
title: "dev_tools.app (DevToolsApp)"
type: module-action
module: dev_tools
action: app
fqn: dev_tools.app
short_name: DevToolsApp
keywords: [dev_tools, app, devtoolsapp, dev]
permissions: []
risk_level: medium
irreversible: false
require_approval: false
---

# dev_tools.app (DevToolsApp)

## Description
App lifecycle + discovery + packages + MCP + drafts + security.

## Parameters
| Name | Type | Required | Default | Description |
|------|------|:--------:|---------|-------------|
| `yaml_path` | string |  | `` | Path to app YAML (deploy/validate). |
| `app_id` | string |  | `` | App ID (status/undeploy/secrets/tools). |
| `yaml_content` | string |  | `` | Inline YAML content (alternative to yaml_path). |
| `validate_only` | boolean |  | `False` | Validate YAML without deploying. |
| `compile_yaml` | boolean |  | `False` | Compile YAML and return resolved config. |
| `prompt_preview` | boolean |  | `False` | Preview the resolved system prompt for an agent. |
| `generate_manifest` | boolean |  | `False` | Generate a package manifest from YAML. |
| `agent_id` | string |  | `` | Agent ID for prompt_preview. |
| `undeploy` | boolean |  | `False` | Undeploy the app. |
| `list_apps` | boolean |  | `False` | List all deployed apps. |
| `list_modules` | boolean |  | `False` | List all available modules (discovery). |
| `list_templates` | boolean |  | `False` | List all app templates. |
| `list_triggers` | boolean |  | `False` | List available trigger types (discovery). |
| `secret_key` | string |  | `` | Set a secret: key name. |
| `secret_value` | string |  | `` | Set a secret: value. |
| `credential_provider` | string |  | `` | User-level credential provider (e.g. deepseek). |
| `credential_fields` | object |  | - | Credential fields (e.g. {api_key: sk-...}). |
| `list_credentials` | boolean |  | `False` | List user credentials. |
| `delete_credential_id` | string |  | `` | Delete a user credential by id. |
| `search_tools` | string |  | `` | Search tools in the app. Empty = list categories. |
| `get_tool` | string |  | `` | Get full schema of a tool by name. |
| `package_source` | string |  | `` | Install package from source (git url / path / registry id). |
| `list_packages` | boolean |  | `False` | List installed packages. |
| `uninstall_package` | string |  | `` | Uninstall a package by id. |
| `upgrade_package` | string |  | `` | Upgrade a package by id. |
| `mcp_catalog` | boolean |  | `False` | List MCP server catalog. |
| `mcp_install` | object |  | - | Install an MCP server (body). |
| `mcp_list` | boolean |  | `False` | List installed MCP servers. |
| `mcp_delete_id` | string |  | `` | Delete an MCP server by id. |
| `mcp_test_id` | string |  | `` | Test an MCP server connection by id. |
| `list_drafts` | boolean |  | `False` | List builder drafts. |
| `create_draft_yaml` | string |  | `` | Create a draft with this YAML. |
| `draft_name` | string |  | `` | Draft name. |
| `update_draft_id` | string |  | `` | Update draft by id (with yaml_content). |
| `deploy_draft_id` | string |  | `` | Deploy a draft by id. |
| `delete_draft_id` | string |  | `` | Delete a draft by id. |
| `security_profile` | boolean |  | `False` | Get security profile for app_id. |
| `health` | boolean |  | `False` | Daemon health. |
| `diagnostics` | boolean |  | `False` | App diagnostics for app_id. |

## Capability grant (in app YAML)
```yaml
capabilities:
  grant:
    - module: dev_tools
      actions: [app]
```

## Tool usage instructions
```
Manage apps on the live daemon: validate, deploy, undeploy, configure, and the full builder surface (compile, prompt_preview, drafts, MCP, packages).

## Lifecycle
  App(yaml_path='app.yaml', validate_only=true)
  App(yaml_path='app.yaml')                    - deploy (from file)
  App(yaml_content='<yaml string>')            - deploy (inline, builder-friendly)
  App(app_id='my-app')                         - status + required secrets
  App(app_id='my-app', undeploy=true)
  App(list_apps=true)

## Secrets & credentials
  App(app_id='my-app', secret_key='X', secret_value='Y')
  App(credential_provider='deepseek', credential_fields={'api_key': 'sk-...'})
  App(list_credentials=true)
  App(delete_credential_id='<uuid>')

## Discovery & builder
  App(yaml_content=..., compile_yaml=true)
  App(yaml_content=..., prompt_preview=true, agent_id='main')
  App(yaml_content=..., generate_manifest=true)
  App(list_modules=true) / list_templates=true / list_triggers=true

## Drafts (builder iteration loop)
  App(create_draft_yaml=..., draft_name=...)
  App(list_drafts=true)
  App(update_draft_id=..., yaml_content=...)
  App(deploy_draft_id=...)
  App(delete_draft_id=...)

## Packages & MCP
  App(list_packages=true) / package_source='<git url>' / uninstall_package=...
  App(mcp_catalog=true) / mcp_list=true / mcp_install={...} / mcp_test_id=...

## Tool discovery (what the agent can call inside an app)
  App(app_id='my-app', search_tools='read')    - filter by keyword
  App(app_id='my-app', get_tool='Write')       - full schema

## Observability
  App(health=true)
  App(app_id='my-app', diagnostics=true)
  App(app_id='my-app', security_profile=true)

## Rules
- ALWAYS validate before deploying
- ALWAYS check required_secrets after deploy - the app won't work without them
- Prefer yaml_content for ephemeral tests; yaml_path for real artifacts
```

## Safety
- Risk level: **medium**
