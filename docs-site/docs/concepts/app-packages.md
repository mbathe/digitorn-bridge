# Digitorn App Packages - Canonical Design Document

> **Status**: ✅ design locked on 2026-04-13. Implementation of
> Phase A starts now. Any change to the design from this point on
> must update this doc first, then the code.
>
> **Scope for v1**: daemon-side only. The **hub server** (where users
> publish / browse / download community apps) is intentionally out
> of scope for v1 and tracked as a separate project - see §14. The
> daemon ships with built-in apps bundled in the wheel plus the
> ability to install local packages from a directory. That's
> enough to prove the architecture. Hub comes later.

## Table of contents

1. [Vision](#1-vision)
2. [Concepts](#2-concepts)
3. [Directory layout of an AppPackage](#3-directory-layout-of-an-apppackage)
4. [package.toml - complete schema](#4-packagetoml---complete-schema)
5. [Auto-generation of package.toml](#5-auto-generation-of-packagetoml)
6. [DB schema extension](#6-db-schema-extension)
7. [Package sources](#7-package-sources)
8. [REST API](#8-rest-api)
9. [Bootstrap flow at daemon startup](#9-bootstrap-flow-at-daemon-startup)
10. [Lifecycle: install / upgrade / uninstall](#10-lifecycle-install--upgrade--uninstall)
11. [Backwards compatibility & migration](#11-backwards-compatibility--migration)
12. [Permissions & security](#12-permissions--security)
13. [Builder agent integration](#13-builder-agent-integration)
14. [Digitorn Hub - future protocol](#14-digitorn-hub---future-protocol)
15. [Implementation roadmap](#15-implementation-roadmap)
16. [Locked decisions](#16-locked-decisions-)

---

## 1. Vision

Digitorn's long-term model is **distributable AI apps**: official
ones ship with the daemon, community ones come from a public hub,
private ones live inside an organisation. A user clicks "install",
the app shows up in their marketplace, and they can run it.

Built-in apps the daemon pre-installs are **not a special case**;
they are the first entries in that
ecosystem. The same infrastructure that serves them today
will serve community packages tomorrow.

### Goals

- **One packaging format** for built-in local, hub, and git packages.
- **Zero friction** for users: installing an app is a single API call
  that presents the permissions and asks for consent.
- **Forward-compatible** with a future hub (publish / search / signed
  releases) without rewriting the daemon.
- **Backwards-compatible** with existing deployed apps (they become
  `local` packages transparently).
- **Auto-generation** of package manifests so app authors (and the
  builder LLM) don't have to hand-write TOML.

### Non-goals for v1 - explicitly out of scope

- **The Hub server itself** - the public service at
  `hub.digitorn.io` where users publish, browse, and download
  community apps. Separate project, tracked in §14. The daemon
  ships with `HubSource` as a stub that raises ``NotImplementedError``
  so the wiring is ready the day the hub exists, but no network
  calls happen in v1.
- **Git source** - same treatment: interface defined, implementation
  is a stub. Easy follow-up when someone asks for it.
- **Package signing (crypto)** - the bundle format keeps a slot for
  a signature file, but v1 doesn't verify it. Comes with the hub.
- **Marketplace UI (browse / search remote packages)** - there's
  nothing to browse until the hub exists.
- **Per-package sandbox isolation** (each app in its own container) -
  separate project.
- **Paid packages / licensing** - out of scope.
- **Package dependencies between apps** (package A needs package B) -
  v1 says "every package is independent".
- **Auto-update from a remote source** - the daemon only auto-updates
  **built-in** packages (via hash check at boot). Local packages
  require an explicit upgrade call.

### Why this matters now

If we ship "built-in apps" as a quick-and-dirty hardcoded loader
today, we pay the price when the hub arrives: re-writing the daemon,
data migrations, re-training the builder agent. Spending 2-3 days
on a proper foundation now saves weeks later. This document is the
blueprint.

---

## 2. Concepts

### AppPackage

A **package** is the unit of distribution. It's a directory with a
`package.toml` manifest, an `app.yaml` (the compilable app
definition), optionally a `README.md`, `ICON.png`, `skills/`,
`workspace/`, and `assets/`.

```text
my-package/
├── package.toml          # manifest (required)
├── app.yaml              # compilable app (required)
├── README.md             # displayed in the marketplace
├── ICON.png              # shown on cards
├── skills/               # optional
├── workspace/            # default workspace contents (optional)
└── assets/               # anything else the YAML references
```

### PackageRegistry

In-daemon registry of **which packages are installed** and where
they came from. Backed by a new SQL table `installed_packages`
(see §6). One row per installed package.

### Source

Where a package came from: `builtin`, `local`, `hub`, `git`. Each
source is implemented by a class that knows how to:

- **Locate** a package (scan filesystem, fetch from URL, clone git)
- **Validate** its manifest + compile its YAML
- **Watch** for updates (optional)

Sources are pluggable. The daemon ships 4 built-in source types;
community can add more via handlers.

### Installation

The act of copying a package into `~/.digitorn/packages/<id>/`,
validating it, registering it, compiling its YAML, and deploying it
as a running app. Idempotent: re-installing the same package at
the same version is a no-op.

### Hash & version

Every package has a semantic version (`1.2.3`) declared in
`package.toml` AND a content hash computed over `app.yaml +
package.toml + every file in the directory`. The hash detects
unexpected edits (drift from the published version) even when the
semantic version didn't change.

---

## 3. Directory layout of an AppPackage

Every AppPackage is a directory with these files:

### Required

| File | Purpose |
|---|---|
| `package.toml` | Manifest (see §4) |
| `app.yaml` | The compilable Digitorn app definition |

### Optional but recommended

| File | Purpose |
|---|---|
| `README.md` | Shown in the marketplace card detail view |
| `ICON.png` (or `icon.svg`) | Card icon, 256×256 recommended |
| `CHANGELOG.md` | Version history shown on update |
| `LICENSE` | License text (required for hub publication) |
| `skills/*.md` | Skill files referenced by `app.yaml` |
| `workspace/` | Default workspace contents |
| `assets/` | Anything else the YAML references via relative paths |

### Reserved (daemon-managed)

These live **alongside** the package at runtime but are owned by
the daemon, not the package author:

| File | Managed by | Purpose |
|---|---|---|
| `.digitorn/manifest.lock` | daemon | Frozen copy of `package.toml` at install time |
| `.digitorn/hash.sha256` | daemon | Content hash at install time |
| `.digitorn/installed_at.txt` | daemon | Install timestamp |

### Runtime storage

The package directory itself is **read-only** once installed. User
data goes elsewhere:

- **Workspace**: `~/.digitorn/workspaces/<app_id>/` (writable)
- **State**: `~/.digitorn/state/<app_id>/` (writable, via JsonStateStore)
- **Credentials**: global credential store (scope = `per_app_shared` or `per_user`)

This separation means an upgrade can replace the whole package dir
without touching user data.

---

## 4. package.toml - complete schema

The canonical format. Every field is documented; most are optional.

```toml
# ─── Identity ────────────────────────────────────────────────────
[package]
id              = "my-app"                           # required, globally unique
name            = "My App"                           # required, display name
version         = "1.0.0"                            # required, semver
description     = "What this app does"               # required, 1-2 sentences
author          = "your-name"                        # required
license         = "MIT"                              # required for hub
homepage        = "https://example.com/apps/my-app"  # optional
icon            = "ICON.png"                         # optional, relative path
category        = "developer-tools"                  # optional, hub category

# ─── Source attribution ──────────────────────────────────────────
[package.source]
type        = "official"                    # official | community | local | private
verified    = true                          # signature (future)
publisher   = "digitorn"                    # publisher id on the hub

# ─── Compatibility ───────────────────────────────────────────────
[package.compatibility]
digitorn_min    = ">=2.0.0"                 # minimum daemon version
digitorn_max    = "<3.0.0"                  # optional
python_min      = ">=3.12"                  # if the package ships code
platforms       = ["linux", "darwin", "win32"]

# ─── Runtime requirements ────────────────────────────────────────
[package.requirements]
modules         = ["rag", "http", "memory", "filesystem"]  # modules the app uses
recommended_models = ["claude-sonnet-4-5", "claude-opus-4-6"]
min_disk_mb     = 100
min_memory_mb   = 512
external_tools  = ["git", "docker"]          # binaries expected in PATH

# ─── Declared credentials (ties into credentials_schema) ─────────
[package.credentials]
required = ["anthropic", "notion"]           # provider names from credentials_schema
optional = ["slack"]

# ─── Permissions (presented at install time) ─────────────────────
[package.permissions]
risk_level         = "low"                   # low | medium | high
network_access     = true
filesystem_access  = ["read", "write"]
filesystem_scopes  = ["workspace", "inbox"]  # which dirs the app touches
requires_approval  = ["shell.bash", "filesystem.rm"]  # dangerous actions

# ─── Hub metadata (only filled when published) ───────────────────
[package.hub]
tags          = ["coding", "productivity", "job-hunt"]
screenshots   = ["screenshot1.png", "screenshot2.png"]
demo_video    = "https://..."
minimum_rating= 0
downloads     = 0

# ─── Changelog / version metadata ────────────────────────────────
[package.release]
released_at   = "2026-04-13"
release_notes = "Initial release"
breaking      = false
upgrade_from  = []                            # version strings where a migration is needed
```

### Field reference

#### `[package]`

- **`id`** (string, required) - Globally unique. Regex:
  `[a-z][a-z0-9-]{2,63}`. Used as the deployed `app_id`.
- **`name`** (string, required) - Human-readable name for the UI.
- **`version`** (semver string, required) - `major.minor.patch`.
- **`description`** (string, required) - One paragraph max. Shown
  on the marketplace card.
- **`author`** (string, required) - Publisher name. For hub
  packages this must match the publisher on the hub side.
- **`license`** (string, optional) - SPDX identifier. Required for
  hub publication.
- **`homepage`** (URL, optional) - External link shown on the card.
- **`icon`** (relative path, optional) - Path to an image file
  inside the package. PNG/SVG, ≤ 512 KB.
- **`category`** (string, optional) - Marketplace category:
  `productivity | developer-tools | assistant | research | creative
  | data | communication | other`.

#### `[package.source]`

- **`type`** (required) - `official | community | local | private`.
  Determined automatically by the install flow:
  - Built-in (shipped with daemon) → `official`
  - Installed from hub → `community` (or `official` if publisher
    == "digitorn")
  - `POST /packages/install {source_type: "local", path: ...}` → `local`
  - Git-backed → `community`
- **`verified`** (bool) - Set to `true` after signature check on hub
  packages (future).
- **`publisher`** (string) - Hub username. For built-in apps it's
  always `digitorn`.

#### `[package.compatibility]`

Package is **rejected at install** if the daemon version doesn't
satisfy `digitorn_min` and `digitorn_max`. The user sees a clean
error: *"This package needs Digitorn ≥2.0.0, you have 1.9.5"*.

#### `[package.requirements]`

Advisory, not enforced at install but surfaced in the UI:

- **`modules`** - which Digitorn modules must be loaded. If one is
  missing or failed to load at daemon startup, the install is
  blocked with a clear message ("missing module 'rag'").
- **`recommended_models`** - sugar for the UI. Doesn't constrain.
- **`min_disk_mb`** / **`min_memory_mb`** - advisory; daemon logs
  a warning if the host is below.
- **`external_tools`** - binaries the app expects in `PATH`. We
  check with `shutil.which` at install and warn (not block) if
  missing.

#### `[package.credentials]`

Links to the `credentials_schema` inside `app.yaml`:

```yaml
# app.yaml
execution:
  credentials_schema:
    providers:
      - name: anthropic
        type: api_key
        scope: per_user
      - name: notion
        type: oauth2
        scope: per_user
        oauth_provider: notion
      - name: slack
        type: multi_field
        scope: per_user
```
```toml
# package.toml
[package.credentials]
required = ["anthropic", "notion"]
optional = ["slack"]
```

At **install time**, the user sees a list of required credentials
and is told they'll need to fill them before the app can run.

#### `[package.permissions]`

Shown on a confirmation screen **before** install. The user must
agree. Think Android install permissions, but scoped to what a
Digitorn agent can actually do.

- **`risk_level`** - computed automatically from the actions the
  app grants:
  - `low` = read-only network + memory
  - `medium` = filesystem write, web fetch, spawning sub-agents
  - `high` = shell exec, remote code execution, destructive ops
- **`network_access`** - does the app do HTTP calls?
- **`filesystem_access`** - `read`, `write`, or `none`.
- **`filesystem_scopes`** - which directories:
  - `workspace` - the app's own `~/.digitorn/workspaces/<id>/`
  - `user_home` - the full ~/
  - `system` - anywhere
  - custom paths like `~/projects`
- **`requires_approval`** - list of FQN actions that trigger an
  interactive prompt before each execution (e.g. `shell.bash`,
  `filesystem.rm`).

---

## 5. Auto-generation of package.toml

> **Key decision** (confirmed with the user): `package.toml` is
> **required** for publication on the hub, but we auto-generate a
> best-effort stub for local packages and built-in migrations so
> the user (and the builder agent) doesn't have to write TOML by
> hand. The stub is then editable.

### The generator

A function `generate_package_toml(app_yaml_path, app_yaml_content,
compiled_app) → str` that builds a valid `package.toml` from:

- The `app:` block of the YAML (id, name, version, description,
  author, category, tags, icon, color)
- The compiled list of modules
- The declared `credentials_schema.providers`
- The declared capabilities (to compute risk_level)

```python
def generate_package_toml(compiled: CompiledApp) -> str:
    """Build a best-effort package.toml from a CompiledApp.

    Used by:
    - `digitorn package init` CLI
    - The builder agent's `publish` tool
    - The daemon migration path for existing local apps
    """
    meta = compiled.meta
    modules = list(compiled.modules.keys())
    cred_schema = compiled.execution.credentials_schema or {}
    required_creds = [
        p["name"] for p in cred_schema.get("providers", [])
        if p.get("required", True)
    ]
    optional_creds = [
        p["name"] for p in cred_schema.get("providers", [])
        if not p.get("required", True)
    ]

    risk = _infer_risk_level(compiled)  # see below

    return _render_toml(
        package_id=meta.app_id,
        name=meta.name,
        version=meta.version or "0.1.0",
        description=meta.description,
        author=meta.author or "unknown",
        icon=meta.icon,
        category=meta.category,
        modules=modules,
        required_creds=required_creds,
        optional_creds=optional_creds,
        risk_level=risk,
        network=_has_network(compiled),
        fs_scopes=_infer_fs_scopes(compiled),
    )
```

`_infer_risk_level` walks `capabilities.grant` and returns:
- `high` if any of `shell.bash`, `filesystem.rm`, `database.sql`,
  `agent_spawn.spawn_agent` is granted without approval
- `medium` if filesystem write or web fetch is granted
- `low` otherwise

### CLI helper

```bash
# From an existing app.yaml, write a package.toml next to it
digitorn package init ./my-app/app.yaml

# Validate an existing package.toml against the schema
digitorn package validate ./my-app/package.toml

# Bundle a package into a .dtpkg file for distribution
digitorn package bundle ./my-app/ -o my-app-1.0.0.dtpkg
```

### Builder agent integration

The App Builder agent can call a new tool
`package.generate_manifest(yaml_text)` which returns the rendered
TOML. When a user finishes building an app, the builder offers:

> *"Here's your compiled YAML. Want me to turn it into a full
> package (with manifest, icon, README) for the marketplace?"*

If yes, the builder invokes `package.generate_manifest` and shows
the result via `ask_user(content=toml)` for the human review
before writing it.

---

## 6. DB schema extension

One new table + 3 columns on an existing table.

### New table: `installed_packages`

```python
class InstalledPackage(Base):
    """One installed app package.

    Maps a package_id to its source + on-disk location + install
    metadata. Deleting a row removes the package from the marketplace
    UI but does NOT touch the package files on disk - those are
    cleaned by the uninstall routine.
    """

    __tablename__ = "installed_packages"

    package_id: Mapped[str] = mapped_column(String(64), primary_key=True)

    # Source attribution
    source_type: Mapped[str] = mapped_column(String(16), nullable=False)
    # "builtin" | "local" | "hub" | "git"
    source_uri: Mapped[str] = mapped_column(String(1024), default="")
    # e.g. "bundle://digitorn/builder"  - for builtin
    #       "file:///abs/path/to/my-app" - for local
    #       "hub://alice/jobhunt@1.2.0"  - for hub (future)
    #       "git+https://github.com/..." - for git

    version: Mapped[str] = mapped_column(String(32), default="0.0.0")
    hash: Mapped[str] = mapped_column(String(64), default="")
    # sha256 of (app.yaml + package.toml + every asset)
    install_dir: Mapped[str] = mapped_column(String(1024), default="")
    # Absolute path on disk

    manifest: Mapped[dict] = mapped_column(JSON, default=dict)
    # Frozen copy of package.toml for fast lookups

    status: Mapped[str] = mapped_column(String(16), default="installed")
    # installed | installing | broken | uninstalling

    installed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow,
    )
    installed_by: Mapped[str] = mapped_column(String(64), default="")
    # user_id who triggered the install (for audit)

    __table_args__ = (
        Index("ix_installed_packages_source", "source_type"),
        Index("ix_installed_packages_updated", "updated_at"),
    )
```

### New columns on `applications`

The existing `applications` table gets 3 new nullable columns so
existing rows keep working:

```python
class Application(Base):
    # … existing columns …
    package_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Links a deployed app back to the package it came from. NULL
    # for apps deployed the old way (before this refactor).
    source_type: Mapped[str] = mapped_column(
        String(16), default="local",
    )
    package_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
```

Migration on boot: every existing row without a `source_type`
becomes `source_type = "local"` automatically (see §11).

---

## 7. Package sources

A **Source** is a class that knows how to fetch, validate, and
install packages of one kind. All sources implement a common ABC:

```python
class PackageSource(ABC):
    """Abstract source - one per source_type."""

    source_type: str

    @abstractmethod
    async def list_available(self) -> list[AvailablePackage]:
        """Return packages this source knows about but hasn't installed yet."""

    @abstractmethod
    async def fetch(self, source_uri: str, dest: Path) -> Path:
        """Copy / download / clone the package into ``dest``.

        Returns the directory inside ``dest`` that contains
        ``package.toml``. Raises ``FetchError`` on failure.
        """

    @abstractmethod
    async def check_update(self, installed_uri: str) -> str | None:
        """Return the latest version available, or None if no update."""

    async def validate_manifest(self, path: Path) -> PackageManifest:
        """Parse + validate package.toml. Default impl is shared."""
```

### Source 1 - `builtin`

Scanned at daemon startup. `fetch` is a local file copy.
`check_update` compares the on-disk hash to the installed hash; if
the wheel has been upgraded and the hash changed, the daemon
re-installs transparently.

### Source 2 - `local`

User points to a local directory:

```
POST (daemon API) {
  "source_type": "local",
  "path": "/home/alice/my-app",
  "link_mode": "symlink"   // symlink | copy
}
```

In `symlink` mode the installed package points back to the user's
dev directory - convenient for authoring, but the daemon shows a
"🔗 linked" badge so the user knows it's not a pinned version.

### Source 3 - `hub` ⏸️ STUB ONLY in v1

The class exists with the full ``PackageSource`` interface but
every method raises ``NotImplementedError("hub source not
available in this daemon - see docs/APP_PACKAGES.md §14")``. The
HTTP route ``POST (daemon API) {source_type: "hub"}``
returns ``501 Not Implemented`` with the same message.

The interface is locked in v1 so that on the day the hub server
ships we can fill in the methods without touching anything else.
When built, this source will:

1. Call `hub.digitorn.io(daemon API)?version=X`
2. Stream the `.dtpkg` bundle (a zipped package directory + signature)
3. Verify the signature (Ed25519, publisher key fetched via well-known URL)
4. Unzip into `~/.digitorn/packages/<id>/`
5. Register

### Source 4 - `git` ⏸️ STUB ONLY in v1

Same treatment as `hub`: full interface declared, all methods
return ``NotImplementedError``. The HTTP install route returns
``501``. Wiring is ready for the day someone needs it.

When implemented, this source will:

```
POST (daemon API) {
  "source_type": "git",
  "source_uri": "https://github.com/alice/jobhunt.git",
  "ref": "v1.2.0"
}
```

Clones a git repo + checks out the ref. Minimal implementation:
just shells out to `git clone --depth 1`. No submodule support.

---

## 8. REST API

### List installed packages

`GET (daemon API)` returns:

```json
{
  "packages": [
    {
      "package_id": "example-builtin",
      "name": "Example Built-in",
      "version": "1.0.0",
      "source_type": "builtin",
      "status": "installed",
      "manifest": { },
      "deployed_app_id": "example-builtin",
      "update_available": null
    },
    {
      "package_id": "alice-jobhunt",
      "source_type": "hub",
      "update_available": "1.3.0"
    }
  ]
}
```

### Get one package detail

`GET (daemon API)` returns the full manifest, install path, and
runtime status for a single package.

### Install a package

Request body:

```json
{
  "source_type": "local",
  "source_uri": "/path/to/package",
  "version": "1.0.0",
  "accept_permissions": true
}
```

`version` is optional for local sources and required for hub
sources. `accept_permissions` is a confirmation flag.

Response:

```json
{
  "package_id": "my-app",
  "status": "installed",
  "installed_at": "...",
  "deployed_app_id": "my-app"
}
```

**First call with `accept_permissions` unset** returns 409 with
the permissions payload so the client can show a confirmation dialog:

```json
{
  "error": "permissions_required",
  "permissions": {
    "risk_level": "medium",
    "network_access": true,
    "filesystem_access": ["read", "write"],
    "requires_approval": ["filesystem.rm"]
  }
}
```

The client re-posts with `accept_permissions: true` once the user clicks OK.

### Uninstall

`POST (daemon API)` returns:

```json
{ "uninstalled": true, "files_removed": 42 }
```

Refuses (`403 builtin_protected`) if `source_type == "builtin"` and
`force != true`.

### Upgrade

Request body (all fields optional, defaults to latest):

```json
{ "version": "1.3.0" }
```

Response:

```json
{ "package_id": "my-app", "from": "1.2.0", "to": "1.3.0" }
```

Failure-safe: downloads new version into a `new/` subdirectory,
validates, then atomically swaps. If anything goes wrong the old
version stays intact.

### Check for updates

`GET (daemon API)` returns:

```json
{
  "updates_available": [
    { "package_id": "...", "current": "1.2.0", "latest": "1.3.0" }
  ]
}
```

Queries all sources that support `check_update` (builtin: local hash
check; hub: HTTP GET; git: `git ls-remote`).

### List available on the hub (future)

`GET (daemon API)?source=hub&q=job+hunter` returns:

```json
{
  "results": [
    { "package_id": "alice-jobhunt", "name": "...", "description": "..." }
  ]
}
```

Stub in v1, real in Phase D.

### Emit package manifest from an app.yaml

The internal builder agent and the `digitorn package init` CLI
walk an app.yaml and produce a package.toml stub. The HTTP
endpoint that drives this is admin-only and not documented
publicly; use the CLI command if you are building outside the
admin UI.

---

## 9. Bootstrap flow at daemon startup

New lifespan step in `server.py` after the existing startup:

```python
async def _bootstrap_packages(app: FastAPI) -> None:
    """Scan + install built-in packages, then reload installed ones."""
    registry = app.state.package_registry = PackageRegistry(
        session_factory=get_session_factory(),
        install_root=Path.home() / ".digitorn" / "packages",
    )

    # 1. Discover built-ins
    builtin_source = BuiltinSource(
        packages_dir=Path(digitorn.__file__).parent / "builtins",
    )
    builtins = await builtin_source.list_available()

    for pkg in builtins:
        installed = await registry.get(pkg.package_id)
        if installed is None:
            await registry.install(pkg, source=builtin_source)
            logger.info("builtin package installed: %s", pkg.package_id)
        elif installed.hash != pkg.hash:
            await registry.upgrade(installed, pkg, source=builtin_source)
            logger.info("builtin package upgraded: %s", pkg.package_id)

    # 2. Reload every installed package as a deployed app
    for row in await registry.list_all():
        try:
            await manager.deploy(
                Path(row.install_dir) / "app.yaml",
                package_id=row.package_id,
            )
        except Exception as exc:
            logger.error("failed to redeploy package %s: %s", row.package_id, exc)
            await registry.mark_broken(row.package_id, str(exc))
```

### Migration safety net

Every existing `applications` row without `package_id` is left
alone and exposed as `source_type="local"` transparently. The
existing `manager.deploy(yaml_path)` path continues to work for
them.

---

## 10. Lifecycle: install / upgrade / uninstall

### Install

1. Fetch the package (`source.fetch`) into
   `~/.digitorn/packages/<id>/new/`.
2. Validate `package.toml` and compile `app.yaml`.
3. Check permissions. If new permissions are required and the user
   has not accepted, return `409`.
4. Check daemon compatibility (version ranges).
5. Check required credentials are available, or advertise what the
   user must fill before the app can run.
6. Atomic move: rename `new/` to `<id>/`.
7. Compute content hash, write `.digitorn/manifest.lock` and
   `.digitorn/hash.sha256`.
8. Insert row in `installed_packages`.
9. Call `manager.deploy(install_dir/app.yaml, package_id=<id>)`.
10. Return success.

### Upgrade

Same flow as install, but step 6 becomes:

1. Rename `<id>/` to `<id>-old/`.
2. Rename `new/` to `<id>/`.
3. Redeploy via `manager.redeploy(force=True)`.
4. On success, delete `<id>-old/`.
5. On failure, rollback: swap `<id>-old/` back to `<id>/` and
   redeploy the old version.

### Uninstall

1. Refuse if `source_type == "builtin"` and `force != true`.
2. Call `manager.undeploy(app_id)` to stop the running app.
3. Delete the `<id>/` directory but preserve
   `~/.digitorn/workspaces/<id>/`.
4. Delete the `installed_packages` row.
5. Credentials scoped to this app are kept by default (user data is
   sacred). Pass `--purge` to drop them too.

---

## 11. Backwards compatibility & migration

### Existing deployed apps

When the daemon starts with this new schema for the first time:

```python
# Migration step run once, idempotent
async def migrate_existing_apps_to_packages() -> None:
    async with session_factory() as db:
        rows = await db.execute(
            select(Application).where(Application.package_id.is_(None))
        )
        for app in rows.scalars():
            # Auto-classify as local package
            app.source_type = "local"
            app.package_id = None  # stays null - they're not packages
        await db.commit()
```

No data is destroyed. The apps continue to deploy via the legacy
path (`manager.deploy(yaml_path)`).

### New vs old deploy routes

- `POST (apps API)` (existing) - works as before, creates an
  untracked `local` deployment. Still used by the CLI and builder.
- `POST (daemon API)` (new) - creates a tracked package
  with manifest + hash.

Eventually `POST (apps API)` will be a thin wrapper over
`POST (daemon API) {source_type: "local"}` that auto-generates
the manifest. Not forced yet - gradual migration.

---

## 12. Permissions & security

### Install-time consent

Every install route call MUST include `accept_permissions: true`
after a 409 permissions probe. The daemon refuses silent installs.

### Runtime enforcement

Permissions declared in `package.toml` are cross-checked against
the compiled app's `capabilities.grant` block:

- If `permissions.filesystem_access = ["read"]` but the app grants
  `filesystem.write` → **install refused** with a clear error
  *"Package declares read-only access but requests filesystem.write.
  Either update the manifest or reduce the capability grants."*

This catches malicious (or sloppy) packages that undersell what
they actually do.

### Master key & credentials

Built-in packages are special in one way: they can access
`system_wide` credentials. Other packages can only access
`per_user` / `per_app_per_user` / `per_app_shared` (the user's
own credentials they've explicitly filled for that app).

### Audit log

Every install, upgrade, uninstall writes to a new `package_events`
table with `{package_id, action, actor_user_id, timestamp, details}`.
Admin operators can inspect it through the admin API.

### Sandbox (future)

Not in v1. Eventually each package should run in a separate
process / container so a malicious agent can't escape its sandbox.
Tracked as a separate project. For now, install-time permission
consent + runtime capability enforcement is the safety net.

---

## 13. Builder agent integration

The existing builder agent (§apps/builder/app.yaml) already:

1. Asks the user what they want
2. Generates / adapts a YAML via templates + concepts
3. Validates via `(apps API)`
4. Saves as a draft

With packages, we add **one more step**: after compile success, the
builder offers:

```
ask_user(
  question="Your app is ready. Do you want me to package it so you can
            install / uninstall / share it later?",
  choices=["yes, create a package",
           "no, just deploy it as a local app"]
)
```

If yes:

1. Generate `package.toml` via `discovery.generate-package-manifest`.
2. Show the TOML in `ask_user(content=toml)` for user review.
3. On approval, create a directory layout:

   ```text
   ~/.digitorn/drafts/<draft_id>/
   ├── package.toml
   ├── app.yaml
   └── README.md           # one-paragraph description
   ```

4. Install via the local install endpoint:

   ```json
   {
     "source_type": "local",
     "source_uri": "~/.digitorn/drafts/<draft_id>",
     "accept_permissions": true
   }
   ```

5. Show the new package card in the chat.

The builder's knowledge base gets one new card type:
`concepts/package.md` - "what is a package, when to publish, how
permissions work". The RAG pulls it when the user says *"I want
to publish my app"*.

---

## 14. Digitorn Hub - future protocol

This section is **NOT implemented in v1** but sketches the contract
so we don't back ourselves into a corner.

### Publish

```
PUT https://hub.digitorn.io(daemon API)
headers: Authorization: Bearer <publisher_token>
body: multipart/form-data
  - manifest.toml
  - bundle.dtpkg        (the whole package dir zipped)
  - signature.sig       (Ed25519 over the bundle, optional for v1)
```

### Download

`GET https://hub.digitorn.io(daemon API)?version=1.2.0` streams the
`.dtpkg` bundle.

### Search

`GET https://hub.digitorn.io(daemon API)?q=job+hunter&category=productivity`
returns:

```json
{ "results": [ ] }
```

### Daemon → hub auth

The daemon stores a hub token in the credential store at scope
`system_wide` under provider `hub`. Set via admin CLI:

```bash
digitorn credentials set --scope system_wide hub api_key YOUR_HUB_TOKEN
```

The token is used for:
- Publishing (requires an authenticated publisher)
- Rate-limited downloads (optional for public packages)
- Telemetry (opt-in "app X was installed by N users" for the hub)

### Ratings / reviews

A separate microservice behind `https://hub.digitorn.io(daemon API)`
that lets users post reviews. Not in v1. Abuse detection + moderation
is a whole project - defer.

---

## 15. Implementation roadmap

Ordered phases. Each phase is independently deployable - we can
ship one and see it work before starting the next.

> **v1 ships phases A-D only**. Phase E (the actual hub server) is a
> separate project tracked outside this doc.

### Phase A - Foundation (2 days)

1. directory
2. `models.py` + migration (3 new columns + 1 new table)
3. `PackageManifest` Pydantic model (`package.toml` parser)
4. `PackageRegistry` CRUD store with hash + version tracking
5. `PackageSource` ABC + `BuiltinSource` + `LocalSource` concrete impl
6. `HubSource` + `GitSource` **stub implementations** (interface in
   place, methods raise ``NotImplementedError``)
7. `InstallFlow` orchestrator (fetch → validate → perms → move → deploy)
8. Permission check at install time (`package.install` capability)
9. Unit tests: parse manifest, install from local dir, install builtin,
   stub source raises correctly, uninstall, hash drift detection

### Phase B - Built-in migration (0.5 day)

Migrate each shipped built-in app to a `package.toml` under then wire
`_bootstrap_packages` into the daemon lifespan.

### Phase C - HTTP routes (1 day)

1. `core(daemon API).py`: list / get / install / uninstall / upgrade /
   generate-manifest
2. Permissions probe → 409 flow
3. Auto-classification of existing apps as `local` source on first boot
4. ``hub`` and ``git`` source paths return 501 with a clear
   "available in v2" message
5. Integration tests

### Phase D - Builder integration (0.5 day)

1. Add `discovery.generate_package_manifest` to the discovery API
2. Update the builder agent system prompt to offer packaging
3. Add `concepts/package.md` to the knowledge base
4. Wire ``ask_user`` flow: "want me to package this app?" → review TOML
   → install via `POST (daemon API)`

**Total for phases A-D**: ~4 days of focused work, parallelisable
with other features.

### Phase E - Real hub service ⏸️ DEFERRED

Separate project. FastAPI + postgres + S3 for bundles. UI for
browsing/searching/publishing. Authentication for publishers.
Signature verification. Moderation.

**Not planned yet**. The daemon is fully usable without it: built-in
apps come from the wheel, custom apps come from the builder agent
or from a local directory. The hub adds **distribution** but isn't
required for **functionality**.

When the hub project starts, the daemon-side wiring is already in
place (HubSource interface) - implementing the real source is
~200 lines of HTTP client code + signature verification.

---

## 16. Locked decisions ✅

Reviewed and validated by the user on 2026-04-13. **Authoritative**
- no question is reopened without updating this section first.

### D1 - Install directory: `~/.digitorn/packages/<id>/`

Per-user under the user's home, alongside `state/`, `workspaces/`,
`drafts/`, `master.key`. One directory to back up everything. The
daemon never writes to system-wide locations.

### D2 - Content hash: every file, sorted, SHA-256

```python
def compute_package_hash(pkg_dir: Path) -> str:
    h = hashlib.sha256()
    for path in sorted(pkg_dir.rglob("*")):
        if path.is_file() and ".digitorn/" not in str(path):
            h.update(path.relative_to(pkg_dir).as_posix().encode())
            h.update(b"\0")
            h.update(path.read_bytes())
            h.update(b"\0")
    return h.hexdigest()
```

The `.digitorn/` subfolder is **excluded** from the hash (it's
daemon-managed metadata). Sorting guarantees deterministic hashes
across machines. For typical 1-10 MB packages the hash takes ~50 ms,
acceptable at boot time.

### D3 - Per-package state isolation

Each installed package gets `~/.digitorn/state/<package_id>/`. The
existing JsonStateStore is extended to accept a namespace argument
so module state lookups become `state/<package_id>/<module>.state.json`.
Uninstall does `rmtree` cleanly. ~30 lines of state_store changes.

### D4 - Version pinning, exact version

Hub/git installs pin a specific version (or git SHA). Upgrades
are explicit: `POST (daemon API) {version: "1.3.0"}`.
Local source supports an additional `link_mode: "symlink"` for
in-place dev iteration - the package directory points back to the
user's working copy and the hash check is disabled.

### D5 - Permissions UX

- **Install** : flat permission list, user clicks accept-all or
  cancel. No granular opt-out (would require runtime capability
  filtering and confuses users).
- **Upgrade** : if the new version's permissions have grown, show a
  diff dialog with the additions highlighted in red. The user must
  re-accept. If permissions haven't changed or have shrunk, the
  upgrade goes through silently (no fatigue).

### D6 - CLI surface (7 commands)

```bash
digitorn package install <uri>           # install from any source
digitorn package uninstall <id>          # remove
digitorn package list                    # list installed
digitorn package init <path>             # scaffold package.toml
digitorn package validate <path>         # check + compile
digitorn package bundle <path> -o <file> # make a .dtpkg archive
digitorn package upgrade <id> [version]  # explicit upgrade
```

`publish` and `search` come later with the hub.

### D7 - `.dtpkg` format: tar.gz with signature slot

V1 is just a tar.gz of the package directory. The bundle layout
includes `.digitorn/manifest.sig` as a placeholder - when the hub
ships, the daemon starts verifying it. Until then it's ignored.

### D8 - Rollback after upgrade

- **Compile failure on the new version**: rollback automatic, the
  new directory is never moved into place.
- **Deploy failure** (compile OK but deploy crashes): rollback
  automatic, swap directories back, redeploy old version.
- **Runtime failure** (deploys OK then crashes during activations):
  no auto-rollback. Mark package as `degraded` in the registry,
  show a red banner in the dashboard with `[Rollback to 1.2.0]`
  button. The user decides - auto-rollback could lose data if the
  crash is from user input rather than the package itself.

### D9 - Uninstall a built-in: admin + force

Both required:

```http
DELETE (daemon API)?force=true
Permissions: *
```

- Without `force` → 403 `builtin_protected`
- With `force` but without `*` permission → 403 `requires_admin`
- With both → success, logged to `package_events` with the actor
  user_id

Built-ins reinstall automatically at the next daemon boot via the
bootstrap loop. The uninstall is effectively temporary unless the
admin removes the package files from the wheel itself.

### D10 - Upgrade with active sessions: kill with warning

Confirmation dialog before upgrade lists every active session that
will be interrupted:

```
Upgrade my-app to 1.3.0?

⚠️  This will interrupt 3 active sessions. Drafts and credentials
    are already persisted, but in-flight agent turns will be aborted.

    [Cancel]   [Upgrade and interrupt sessions]
```

Snapshot/restore of running agent state is technically too complex
for v1 (would require freezing the agent loop, tool call queue,
context window, callbacks, partial LLM streams). Persistent data
(drafts, credentials, payloads, message history) is already saved
to disk continuously, so users only lose the current turn - never
their actual work.

### D11 - Install permission: always permission-gated ✅ NEW

Installation requires the user to have `package.install` capability
(or `*` admin). **No "open dev mode"** - even on solo machines, the
first user created at daemon bootstrap is admin by default and
inherits the permission. This avoids a future surprise the day a
multi-user deployment lets a regular user install whatever they
want.

The permission is checked on every:
- `POST (daemon API)`
- `POST (daemon API)`
- `POST (daemon API)`

403 with a clear error message when the caller lacks the permission.

### D12 - app_id collision: strict refusal

If a package with the same `app_id` is already installed (regardless
of source), the new install refuses with:

```
409 Conflict
{
  "error": "package_already_installed",
  "detail": "Package 'jobhunt' is already installed from source 'local'.
             Uninstall it first or use a different package id.",
  "existing": { "source_type": "local", "version": "0.1.0", ... }
}
```

No silent merging, no auto-rename, no override. The user makes an
explicit choice: uninstall the existing one or pick a new id.

---

## Next steps (now that the design is locked)

The credentials foundation we just built is already a hard
dependency of this design - packages reference `credentials_schema`
heavily and install-time permission consent reuses the same UX
patterns as the credentials form.

**Immediate next actions** (in order):

1. Phase A - Foundation (~2 days). See §16.
2. Phase B - Built-in migration (~0.5 day).
3. Phase C - HTTP routes (~1 day).
4. Phase D - Builder integration (~0.5 day).
5. Test end-to-end with the 4 built-ins via the dashboard.
6. (Later, separate project) Phase E - Hub server.
