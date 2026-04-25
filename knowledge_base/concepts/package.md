---
id: package
title: "AppPackage"
type: concept
keywords: [package, install, distribute, share, manifest, package.toml, dtpkg, builtin, hub, scope, system, user_scope, isolation, per_user, admin]
related: [credentials-schema, deploy, builder-state-machine, bundle-namespaces]
source: docs/APP_PACKAGES.md
---

# AppPackage

## What it is
An AppPackage is the unit of installation for a Digitorn app. It bundles the `app.yaml` definition with a `package.toml` manifest declaring metadata (id, version, author, license), runtime requirements (modules, recommended models), credentials needed, and explicit permissions (network, filesystem, risk level). Packages can be installed, upgraded, uninstalled, and (in the future) published to the hub for sharing.

## Install scopes — who can see what

Every installed package has a **scope** that determines visibility:

- **`system`** — installed by an admin, **visible to every user** of the daemon. Files live under `~/.digitorn/packages/<package_id>/`. Typical use: pre-installed builtins (digitorn-chat, digitorn-code, digitorn-builder), enterprise apps deployed for the whole team.

- **`user`** — installed by one specific user, **invisible to every other user**. Files live under `~/.digitorn/users/<owner_user_id>/packages/<package_id>/`. Typical use: personal apps, in-progress development, per-user customizations.

### Permission rules for install/uninstall/upgrade
- **Non-admin** users can ONLY install at `scope=user`. `POST /api/packages/install` with `scope=system` returns 403 for them.
- **Admin** users can install at either scope.
- **Uninstall/upgrade** follow the same rule: you can only modify installs you own. Admins can modify system-scoped installs and their own user-scoped installs.
- **MCP servers** and **modules** are admin-only: regular users can read the list but cannot install, remove, or reconfigure them. They attach their own credentials via the unified credential store.

### Shadow rule
A user can install their **own copy** of a package that also exists at the system level. Example: `digitorn-code` ships as a system builtin, but Alice installs her own custom version. When she opens the app, she sees HER version (with her own prompt tweaks); Bob still sees the system version. If Alice uninstalls her copy, she falls back to the system version without losing access.

## When to use
- The user just deployed an app and wants to **reinstall it later** without re-running the build flow
- The user wants to **share** an app with teammates by giving them a directory or a `.dtpkg` archive
- The user is building something they intend to **publish** on the future Digitorn Hub
- The user wants **versioning** + **upgrade tracking** + **content drift detection** for an app they care about
- **An admin wants to deploy an app for every user on the daemon** → use `scope=system`
- **A user wants to install a personal app only they can see** → use `scope=user` (default)

## YAML
A package is a directory with this minimum layout:

```
my-package/
├── package.toml    # required — manifest with id, version, permissions
├── app.yaml        # required — the compilable Digitorn app definition
└── README.md       # recommended — shown on the marketplace card
```

A minimal `package.toml`:

```toml
[package]
id = "my-package"
name = "My Package"
version = "1.0.0"
description = "What this app does in one line."
author = "alice"
license = "MIT"
category = "productivity"

[package.requirements]
modules = ["filesystem", "web"]

[package.permissions]
risk_level = "medium"
network_access = true
filesystem_access = ["read", "write"]
```

## Gotchas
- Package id MUST be kebab-case, 3-64 chars (e.g. `my-package`, NOT `MyPackage` or `my_package`)
- A package can't be installed twice with the same id — the daemon refuses with a 409 collision error (locked design D12)
- Uninstalling a built-in package (`digitorn-chat`, `digitorn-builder`, `digitorn-code`, `digitorn-deepresearch`) requires admin permission AND `force=true` — and the daemon will reinstall it at the next boot anyway (locked design D9)
- Hub source (`source_type: hub`) is **not yet implemented** in v1 — until the hub server ships, install packages from local directories only (`source_type: local`)
- The `package.toml` doesn't have to be written by hand — the daemon's `/api/discovery/generate-package-manifest` route auto-generates one from any compiled YAML

## See also
- credentials-schema
- deploy
- builder-state-machine
