---
id: yaml-schema-moduleblock
title: "ModuleBlock - YAML schema reference"
type: schema-reference
model: ModuleBlock
is_root: false
keywords: [moduleblock, config, constraints, middleware, setup]
---

# ModuleBlock

## Description
Configuration block for a single module in the app YAML.

Three sections:

- ``config``: Static module configuration - pushed via
``module.on_config_update(config)`` at bootstrap time.  Validated
against the module's ``CONFIG_MODEL`` (Pydantic) if declared.

- ``setup``: Ordered list of action calls executed at bootstrap time.

- ``constraints``: Runtime restrictions applied during the app's lifetime.

Example::

perception:
config:
enabled: false
capture_after: true
ocr_enabled: false
timeout_seconds: 10
actions:
browser.take_screenshot:
capture_after: true
ocr_enabled: true
setup:
- action: register_handler
params: { ... }
constraints:
allowed_actions: [capture_screen, parse_screen]

## Fields

| Name | Type | Required | Default | Description |
|------|------|:--------:|---------|-------------|
| `config` | dict[str, any] |  | `{}` | Static module configuration. Pushed to the module via on_config_update() at bootstrap time. Validated against the module's CONFIG_MODEL if declared.  For MCP servers and third-party modules, an optional 'sandbox' key declares OS-level permissions:   sandbox:     permissions: [fs.read, net.http]     paths:       read: ['{{workspace}}']       write: []     allowed_hosts: [api.github.com] |
| `setup` | list[[SetupStep](SetupStep.md)] |  | `[]` | Ordered list of actions to execute at app bootstrap. |
| `constraints` | dict[str, any] |  | `{}` | Runtime constraints. 'allowed_actions' and 'blocked_actions' are universal; other keys are validated against the module's ConstraintSpec declarations. |
| `middleware` | list[dict[str, any]] |  | `[]` | Module-level middleware pipeline. Each entry is a middleware name with optional config: [{audit: {log_params: true}}, {retry: {max_attempts: 3}}] |

## Linked models
- [SetupStep](SetupStep.md)

## Strictness
- `extra: forbid` - unknown keys cause a validation error
