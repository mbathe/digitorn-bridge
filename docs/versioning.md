---
id: versioning
title: Versioning and stability
---

# Versioning and stability

Digitorn ships under semantic versioning at the package level
(`pyproject.toml`) and an explicit YAML language version
(`schema_version`).

## YAML language v1

The 8-block YAML grammar documented in
[Language](language/) is **the v1 language**. Once an app declares
`schema_version: 2` (the canonical form, where v2 = "8-block"
shape and v1 was the legacy flat shape), it is portable across
every minor and patch release of the daemon that supports v1.

### What "frozen" means

For the lifetime of v1:

- **No required field is added.** Every existing YAML keeps
  parsing without modification.
- **No required field is removed.** Every field documented under
  [Language](language/) keeps doing what it did.
- **No field type is narrowed.** A field that accepts a string
  today won't reject the same string after an upgrade.
- **Default values are stable.** If a field's default changed, the
  daemon would emit a deprecation warning and honour the previous
  default for at least one minor release.

### What CAN change in v1

- **New optional fields.** A new YAML key under any block, with a
  safe default, can land in any release.
- **New modules.** New entries are added to `tools.modules.<id>`.
  Existing modules don't go away without deprecation.
- **New `runtime.mode` values.** Adding modes is backward-compat;
  removing is not.
- **New CLI sub-commands.** `digitorn ...` grows over time.
- **Internal implementation.** Everything inside the daemon -
  the SQL schema, the IPC protocol between worker and child
  processes, the cache file layout - is internal and may change
  in any release.

### Deprecation policy

A field deprecated in `1.X.0` continues to work and emit a
warning. It is removed no sooner than `1.(X+2).0`. Deprecations
are listed in the [archive](archive/) under the matching release
report.

## Daemon version

The daemon's own version follows SemVer (`MAJOR.MINOR.PATCH`).
Breaking changes to internal APIs (the Python module surface, the
internal HTTP routes, the Socket.IO event payload shape if a
widely-deployed client depends on it) increment the MAJOR version.
The public REST API
([reference/api/rest.md](reference/api/rest.md)) is the contract:
breaking changes there are reflected as new versioned routes
(`/api/apps_v2/...`) before the old ones are removed.

## Schema version field

The optional `schema_version` declaration at the top of a YAML
file is a *forward-compat* signal:

```yaml
schema_version: 2
```

When set, the alias pass that converts legacy v1 flat shape to
v2 canonical is skipped (the YAML is already v2). This is the
canonical form: new apps should always set it.

When `schema_version` is absent, the alias pass auto-detects the
shape and applies the v1 → v2 reshape before validation. This is
how every legacy YAML keeps working without modification.

## Migrating from legacy flat shape

The migration table is in
[language/00-index.md](language/00-index.md#migration-from-the-legacy-flat-shape).
The CLI does the rewrite for you:

```bash
digitorn yaml migrate-v2 path/to/app.yaml
```

This is a one-time, in-place rewrite. It is safe: the YAML
remains valid before and after, and the app's runtime behaviour
is identical (the alias pass produces the same compiled output).

## Module API

Modules subclass `BaseModule` and decorate methods with `@action`.
The decorator's surface (`@action(description, tool_prompt,
risk_level, hidden, params_model, ...)`) is part of v1.

Module-internal helpers (anything not decorated with `@action` and
not exposed via `CONSTRAINTS`) is not part of v1 and may move
between releases.
