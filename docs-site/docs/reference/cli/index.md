---
id: cli-index
title: CLI reference
---

# CLI reference

The `digitorn` command is the entry point for every developer
operation. It is exposed by the `digitorn` PyPI package
(entry point `digitorn = "digitorn.core.server:main"`) and routes
into Typer sub-commands registered in
plus the modules.

## Sub-command map

| Command group | Purpose | Page |
|---------------|---------|------|
| `digitorn start` / `stop` / `status` / `version` | Daemon lifecycle. | [Daemon](#daemon) |
| `digitorn app *` | App lifecycle: validate, deploy, run, list, undeploy, delete, schema. | [App](#app) |
| `digitorn dev *` | Test-against-daemon workflow: deploy, chat, status, history. | [Dev CLI](../../language/46-dev-cli.md) |
| `digitorn yaml *` | Migrate legacy YAMLs (`migrate-v2`, `migrate-credentials`). | [App](#app) |
| `digitorn secret *` | Per-app encrypted secrets (legacy; prefer credentials vault). | [Secrets](#secrets) |
| `digitorn credentials *` | The centralised vault. List, set, grant, test. | [Credentials](../runtime/credentials.md) |
| `digitorn mcp *` | Install and manage MCP server bundles. | [MCP servers](../../language/04d-mcp.md) |
| `digitorn middleware *` | Install and list middleware packages. | [Middleware](../runtime/middleware.md) |
| `digitorn modules *` | Module catalog management. | [Modules](../modules/) |
| `digitorn hub *` | Browse, install, publish to the Hub. | [Hub](#hub) |
| `digitorn package *` | Build and inspect `.dtpkg` app bundles. | [Packages](../../concepts/app-packages.md) |
| `digitorn db *` | Database admin commands. | [Database admin](#database-admin) |
| `digitorn requires *` | Module external requirements - list and install OS / runtime dependencies. | [Requires](#requires) |
| `digitorn install-local` | Pair this daemon to a central Digitorn account (one-time). | [Install-local](#install-local) |
| `digitorn init` / `doctor` | First-run wizard, environment doctor. | [Setup](#setup) |

Run any command with `--help` for the full flag list.

## Daemon

```bash
digitorn start [--host 127.0.0.1] [--port 8000] [--workers N] \
               [--config <path>] [--app <yaml>] [--reload] \
               [--tls-cert <pem>] [--tls-key <pem>]

digitorn stop  [--host 127.0.0.1] [--port 8000]
digitorn status
digitorn version
```

`digitorn start` runs the FastAPI / Uvicorn process. `--app` deploys
a given YAML at startup before the lifespan returns. `--config`
points at an alternate `config.yaml` that overrides the system
defaults but not the user-level `~/.digitorn/config.yaml`. To
override that one, use environment variables (`DIGITORN_*` prefix
with double underscore for nesting, e.g. `DIGITORN_DATABASE__URL`).

## App

```bash
digitorn app validate <app.yaml>           # compile-check, no deploy
digitorn app deploy   <app.yaml>           # deploy + arm triggers
digitorn app run      <app.yaml>           # alias of deploy --force, with trigger summary
digitorn app schema   <module_id>          # dump a module's action schema
digitorn app list                          # list deployed apps
digitorn app undeploy <app_id>             # stop without removing the bundle
digitorn app delete   <app_id>             # remove the deployed bundle entirely

digitorn yaml migrate-v2          <app.yaml>     # legacy flat → 8-block canonical
digitorn yaml migrate-credentials <app.yaml>     # {{secret.X}} → credentials vault
```

## Dev CLI

The dev CLI is the recommended way to test apps from the terminal.
It auto-approves any pending capability prompts and is what humans
use day-to-day.

```bash
# Deploy an app to a running daemon
digitorn dev deploy <app.yaml> [-d <daemon-url>]

# Talk to it
digitorn dev chat <app_id>                       # interactive
digitorn dev chat <app_id> -m "single message"   # one-shot

# Inspect its state
digitorn dev status <app_id>
digitorn dev history <app_id> <session-id>
```

Full reference: [Dev CLI](../../language/46-dev-cli.md).

## Secrets

```bash
digitorn secret set    <app_id> <key> [value]    # interactive prompt if value omitted
digitorn secret get    <app_id> <key>
digitorn secret list   <app_id>
digitorn secret delete <app_id> <key>
```

Secrets are encrypted at rest with Fernet. They are the legacy
mechanism; the centralised credentials vault
([Credentials](../runtime/credentials.md)) is preferred for new
apps.

## Hub

```bash
digitorn hub list
digitorn hub install <package> [--scope user|system]
digitorn hub publish <package.dtpkg>
digitorn hub search  <query>
```

## Database admin

```bash
digitorn db migrate           # apply schema migrations
digitorn db status            # current migration head
digitorn db doctor            # diagnose db config / connectivity
```

## Requires

Modules can declare external runtime requirements (binaries,
language toolchains, OS packages). `digitorn requires` lists what
the catalogue says is needed, what's actually present on the
host, and installs the missing pieces where it knows how.

```bash
digitorn requires list                # what every loaded module needs
digitorn requires check               # diff vs the host
digitorn requires install <package>   # OS-level install (apt / brew / winget when available)
```

The catalogue is per-module, declared via the
`requires` field in the module's `digitorn-module.toml`.

## Install-local

`install-local` pairs the local daemon with a central Digitorn
account (cloud-managed credential vault, hub publish access, ...).
Run it once after setup:

```bash
digitorn install-local            # opens browser for auth, writes ~/.digitorn/account.json
digitorn install-local --revoke   # unlink
```

This is the only command that mutates `~/.digitorn/account.json`.
The pairing is per-host; a fresh machine needs to pair again.

## Setup

```bash
digitorn init                 # first-run wizard
digitorn doctor               # environment + dependency check
```
