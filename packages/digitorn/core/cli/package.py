"""CLI commands for the AppPackages system.

    digitorn package install <path-or-uri>      - Install a package
    digitorn package uninstall <id>             - Remove a package
    digitorn package list                       - List installed packages
    digitorn package init <yaml-path>           - Scaffold package.toml from an app.yaml
    digitorn package validate <path>            - Compile + manifest check
    digitorn package bundle <path> -o <file>    - Make a .dtpkg archive
    digitorn package upgrade <id> <new-uri>     - Upgrade an installed package

The install / uninstall / list / upgrade commands talk to a running
daemon via the standard ``daemon_request`` helper. The init / validate
/ bundle commands work **offline** - they read files from disk
without needing the daemon.
"""

from __future__ import annotations

import json
import sys
import tarfile
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from digitorn.core.cli.auth_helpers import daemon_request

console = Console()

package_cli = typer.Typer(
    name="package",
    help="Manage installed AppPackages.",
    no_args_is_help=True,
)

_DEFAULT_DAEMON = "http://127.0.0.1:8000"


# ────────────────────────────────────────────────────────────────────
# Online commands - talk to the daemon
# ────────────────────────────────────────────────────────────────────


@package_cli.command(name="list")
def list_packages(
    daemon: str = typer.Option(_DEFAULT_DAEMON, "--daemon", "-d"),
    source_type: str = typer.Option(
        "", "--source", "-s",
        help="Filter by source type (builtin, local, hub, git)",
    ),
    json_out: bool = typer.Option(False, "--json", help="Output JSON"),
) -> None:
    """List every installed package."""
    params = {}
    if source_type:
        params["source_type"] = source_type
    resp = daemon_request("get", f"{daemon}/api/packages", params=params)
    body = resp.json()
    data = body.get("data") or {}
    packages = data.get("packages") or []

    if json_out:
        console.print_json(data=packages)
        return

    if not packages:
        console.print("[dim]No packages installed.[/dim]")
        return

    table = Table(title=f"Installed Packages ({len(packages)})")
    table.add_column("ID", style="cyan")
    table.add_column("Version")
    table.add_column("Source")
    table.add_column("Status")
    table.add_column("Risk")
    table.add_column("Updated")

    for p in packages:
        manifest = p.get("manifest") or {}
        perms = (manifest.get("permissions") or {})
        risk = perms.get("risk_level", "?")
        updated = (p.get("updated_at") or "")[:19]
        status_color = {
            "installed": "green",
            "broken": "red",
            "degraded": "orange1",
            "upgrading": "yellow",
        }.get(p.get("status", ""), "white")
        table.add_row(
            p.get("package_id", "?"),
            p.get("version", "?"),
            p.get("source_type", "?"),
            f"[{status_color}]{p.get('status', '?')}[/{status_color}]",
            risk,
            updated,
        )
    console.print(table)


@package_cli.command(name="install")
def install_package(
    source_uri: str = typer.Argument(
        ...,
        help="Source path or URI (e.g. /path/to/my-package or hub://alice/jobhunt@1.0.0)",
    ),
    source_type: str = typer.Option(
        "local", "--type", "-t",
        help="Source type: local | builtin | hub | git",
    ),
    yes: bool = typer.Option(
        False, "--yes", "-y",
        help="Auto-accept the permissions consent dialog",
    ),
    daemon: str = typer.Option(_DEFAULT_DAEMON, "--daemon", "-d"),
) -> None:
    """Install a package from a local directory or remote source.

    Without ``--yes``, the daemon returns the requested permissions
    and we display them in a confirmation dialog. The user must
    type 'yes' to proceed.
    """
    body = {
        "source_type": source_type,
        "source_uri": source_uri,
        "accept_permissions": yes,
    }

    # First call - may return 409 with permissions
    resp = daemon_request(
        "post", f"{daemon}/api/packages/install", json=body,
    )

    # 409 → consent flow
    if resp.status_code == 409:
        detail = resp.json().get("detail", {})
        if detail.get("error") == "permissions_required":
            _show_permissions(detail)
            confirm = typer.confirm(
                "Approve these permissions and proceed?", default=False,
            )
            if not confirm:
                console.print("[yellow]Install cancelled.[/yellow]")
                raise typer.Exit(0)
            body["accept_permissions"] = True
            resp = daemon_request(
                "post", f"{daemon}/api/packages/install", json=body,
            )
        elif detail.get("error") == "package_already_installed":
            console.print(
                f"[red]Package '{detail.get('package_id')}' is already "
                f"installed from source '{detail['existing'].get('source_type')}'.[/red]"
            )
            console.print(
                f"[dim]Uninstall it first or use a different package id.[/dim]"
            )
            raise typer.Exit(1)

    if not resp.ok:
        try:
            detail = resp.json().get("detail")
        except Exception:
            detail = resp.text
        console.print(f"[red]Install failed:[/red] {detail}")
        raise typer.Exit(1)

    data = resp.json().get("data") or {}
    console.print(
        Panel(
            f"[green]✓[/green] Installed [cyan]{data.get('package_id')}[/cyan] "
            f"v{data.get('version')}\n"
            f"  source: {data.get('source_type')} → {data.get('install_dir')}\n"
            f"  hash:   {(data.get('hash') or '')[:16]}...\n"
            f"  deployed: {data.get('deployed')}",
            title="Package installed",
            border_style="green",
        )
    )
    if data.get("deploy_error"):
        console.print(f"[yellow]Deploy warning:[/yellow] {data['deploy_error']}")


def _show_permissions(detail: dict) -> None:
    """Pretty-print the permissions dialog from a 409 response."""
    perms = detail.get("permissions") or {}
    pkg_id = detail.get("package_id", "<unknown>")

    risk = perms.get("risk_level", "?")
    risk_color = {"low": "green", "medium": "yellow", "high": "red"}.get(risk, "white")

    body = (
        f"Package: [cyan]{pkg_id}[/cyan]\n"
        f"Risk level: [{risk_color}]{risk.upper()}[/{risk_color}]\n"
        f"Network access: {perms.get('network_access', False)}\n"
        f"Filesystem: {', '.join(perms.get('filesystem_access') or []) or 'none'}\n"
    )
    if perms.get("requires_approval"):
        body += (
            "Requires approval (per call): "
            f"{', '.join(perms['requires_approval'])}\n"
        )

    console.print(
        Panel(
            body,
            title="⚠️  Permissions requested",
            border_style="yellow",
        )
    )


@package_cli.command(name="uninstall")
def uninstall_package(
    package_id: str = typer.Argument(...),
    force: bool = typer.Option(
        False, "--force", "-f",
        help="Required for built-in packages",
    ),
    daemon: str = typer.Option(_DEFAULT_DAEMON, "--daemon", "-d"),
) -> None:
    """Remove a package and its on-disk files."""
    resp = daemon_request(
        "post",
        f"{daemon}/api/packages/{package_id}/uninstall",
        json={"force": force},
    )
    if not resp.ok:
        try:
            detail = resp.json().get("detail")
        except Exception:
            detail = resp.text
        console.print(f"[red]Uninstall failed:[/red] {detail}")
        raise typer.Exit(1)

    console.print(f"[green]✓[/green] Uninstalled [cyan]{package_id}[/cyan]")


@package_cli.command(name="upgrade")
def upgrade_package(
    package_id: str = typer.Argument(...),
    source_uri: str = typer.Argument(
        ...,
        help="Source path or URI for the new version",
    ),
    source_type: str = typer.Option("local", "--type", "-t"),
    yes: bool = typer.Option(False, "--yes", "-y"),
    daemon: str = typer.Option(_DEFAULT_DAEMON, "--daemon", "-d"),
) -> None:
    """Upgrade an installed package to a new version.

    On compile or deploy failure, the daemon automatically rolls
    back to the previous version (locked design D8).
    """
    body = {
        "source_type": source_type,
        "source_uri": source_uri,
        "accept_permissions": yes,
    }
    resp = daemon_request(
        "post", f"{daemon}/api/packages/{package_id}/upgrade", json=body,
    )

    if resp.status_code == 409:
        detail = resp.json().get("detail", {})
        if detail.get("error") == "permissions_required":
            _show_permissions(detail)
            confirm = typer.confirm(
                "Approve and upgrade?", default=False,
            )
            if not confirm:
                console.print("[yellow]Upgrade cancelled.[/yellow]")
                raise typer.Exit(0)
            body["accept_permissions"] = True
            resp = daemon_request(
                "post", f"{daemon}/api/packages/{package_id}/upgrade", json=body,
            )

    if not resp.ok:
        try:
            detail = resp.json().get("detail")
        except Exception:
            detail = resp.text
        console.print(f"[red]Upgrade failed:[/red] {detail}")
        raise typer.Exit(1)

    data = resp.json().get("data") or {}
    console.print(
        f"[green]✓[/green] Upgraded [cyan]{package_id}[/cyan] "
        f"to v{data.get('version')}"
    )


# ────────────────────────────────────────────────────────────────────
# Offline commands - work on local files without the daemon
# ────────────────────────────────────────────────────────────────────


@package_cli.command(name="init")
def init_package(
    yaml_path: Path = typer.Argument(
        ...,
        help="Path to an existing app.yaml. The package.toml is written next to it.",
    ),
    publisher: str = typer.Option("", "--publisher"),
    license: str = typer.Option("", "--license"),
    homepage: str = typer.Option("", "--homepage"),
    daemon: str = typer.Option(_DEFAULT_DAEMON, "--daemon", "-d"),
    force: bool = typer.Option(False, "--force", help="Overwrite an existing package.toml"),
) -> None:
    """Scaffold a package.toml next to an existing app.yaml.

    Calls the daemon's /api/discovery/generate-package-manifest
    route which compiles the YAML and infers risk_level + modules
    + permissions from the capabilities. The result is written
    next to the YAML.
    """
    if not yaml_path.is_file():
        console.print(f"[red]File not found:[/red] {yaml_path}")
        raise typer.Exit(1)
    if not yaml_path.name.endswith((".yaml", ".yml")):
        console.print(f"[red]Not a YAML file:[/red] {yaml_path}")
        raise typer.Exit(1)

    target = yaml_path.parent / "package.toml"
    if target.exists() and not force:
        console.print(
            f"[red]package.toml already exists at[/red] {target}\n"
            f"[dim]Use --force to overwrite.[/dim]"
        )
        raise typer.Exit(1)

    yaml_text = yaml_path.read_text(encoding="utf-8")
    body = {
        "yaml": yaml_text,
        "publisher": publisher,
        "license": license,
        "homepage": homepage,
    }
    resp = daemon_request(
        "post",
        f"{daemon}/api/discovery/generate-package-manifest",
        json=body,
    )
    payload = resp.json()
    if not payload.get("success"):
        errors = (payload.get("data") or {}).get("errors") or []
        console.print(f"[red]Manifest generation failed:[/red] {payload.get('error')}")
        for e in errors:
            console.print(f"  • {e}")
        raise typer.Exit(1)

    data = payload["data"]
    target.write_text(data["toml"], encoding="utf-8")
    console.print(f"[green]✓[/green] Wrote [cyan]{target}[/cyan]")

    summary = data.get("summary") or {}
    console.print(
        f"  package_id: {summary.get('package_id')}\n"
        f"  version:    {summary.get('version')}\n"
        f"  risk:       {summary.get('risk_level')}\n"
        f"  modules:    {', '.join(summary.get('modules') or [])}"
    )

    warnings = data.get("warnings") or []
    if warnings:
        console.print()
        console.print("[yellow]Warnings:[/yellow]")
        for w in warnings:
            console.print(f"  • {w}")


_SCAFFOLD_TEMPLATES = {
    "chat": {
        "description": "Interactive chat app with a single agent",
        "app_yaml": """app:
  app_id: "{app_id}"
  name: "{name}"
  version: "1.0.0"
  description: "{description}"
  icon: "{{{{asset.icon}}}}"
  category: "assistant"

variables:
  greeting: "Hello! How can I help you?"

modules:
  filesystem:
    config:
      workspace: "."
  llm_provider:
    config: {{}}
  memory:
    config: {{}}

agents:
  - id: main
    role: assistant
    brain:
      provider: anthropic
      model: claude-sonnet-4-5
      config:
        api_key: "{{{{env.ANTHROPIC_API_KEY}}}}"
      temperature: 0.1
    system_prompt: "{{{{prompt.main}}}}"

execution:
  mode: interactive
  entry_agent: main
  greeting: "{{{{greeting}}}}"
""",
        "prompts/main.md": """---
version: 1
description: "Main system prompt for the chat assistant"
---

You are {{app.name}}.

Your role is to help users accomplish their tasks. Be concise,
accurate, and friendly. Ask clarifying questions when needed.
""",
        "README.md": """# {name}

{description}

## Setup

1. Set your `ANTHROPIC_API_KEY` environment variable
2. Deploy: `digitorn app deploy ./app.yaml`
3. Chat: `digitorn app run {app_id}`
""",
    },
    "background": {
        "description": "Background agent triggered by cron / webhook",
        "app_yaml": """app:
  app_id: "{app_id}"
  name: "{name}"
  version: "1.0.0"
  description: "{description}"
  icon: "{{{{asset.icon}}}}"
  category: "background"

variables:
  schedule: "0 9 * * *"

modules:
  web:
    config: {{}}
  llm_provider:
    config: {{}}
  memory:
    config: {{}}

agents:
  - id: worker
    role: assistant
    brain:
      provider: anthropic
      model: claude-sonnet-4-5
      config:
        api_key: "{{{{env.ANTHROPIC_API_KEY}}}}"
      temperature: 0.0
    system_prompt: "{{{{prompt.worker}}}}"

execution:
  mode: background
  entry_agent: worker
  triggers:
    - id: daily
      type: schedule
      schedule: "{{{{schedule}}}}"
""",
        "prompts/worker.md": """---
version: 1
description: "Background worker prompt"
---

You are a background worker for {{app.name}}.

Your job runs on a schedule. Each run, check for new data, process
it, and report results. Be idempotent - the same input should
always produce the same output.
""",
        "README.md": """# {name}

{description}

Runs in background mode on the schedule `0 9 * * *` (9 AM daily).
""",
    },
    "multi-agent": {
        "description": "Coordinator with specialist workers",
        "app_yaml": """app:
  app_id: "{app_id}"
  name: "{name}"
  version: "1.0.0"
  description: "{description}"
  icon: "{{{{asset.icon}}}}"
  category: "developer-tools"

modules:
  filesystem:
    config:
      workspace: "."
  shell:
    config: {{}}
  llm_provider:
    config: {{}}
  agent_spawn:
    config: {{}}
  memory:
    config: {{}}

agents:
  - id: coordinator
    role: coordinator
    brain:
      provider: anthropic
      model: claude-opus-4-6
      config:
        api_key: "{{{{env.ANTHROPIC_API_KEY}}}}"
    system_prompt: "{{{{prompt.coordinator}}}}"
    capabilities: [delegate]
  - id: researcher
    role: specialist
    brain:
      provider: anthropic
      model: claude-sonnet-4-5
      config:
        api_key: "{{{{env.ANTHROPIC_API_KEY}}}}"
    system_prompt: "{{{{prompt.researcher}}}}"
    specialty: "Research and information gathering"
  - id: implementer
    role: specialist
    brain:
      provider: anthropic
      model: claude-sonnet-4-5
      config:
        api_key: "{{{{env.ANTHROPIC_API_KEY}}}}"
    system_prompt: "{{{{prompt.implementer}}}}"
    specialty: "Code and document drafting"

execution:
  mode: interactive
  entry_agent: coordinator
""",
        "prompts/coordinator.md": """---
version: 1
---

You are the coordinator for {{app.name}}. Analyze each user
request, break it down into subtasks, and delegate to specialists
via `spawn_agent`. Summarize their results for the user.
""",
        "prompts/researcher.md": """---
version: 1
---

You are a research specialist. Gather accurate information from
reliable sources and present concise findings.
""",
        "prompts/implementer.md": """---
version: 1
---

You are an implementation specialist. Draft code, documents, and
technical content based on research and specifications.
""",
        "skills/delegate.md": """---
version: 1
---

# Delegation

Use `spawn_agent` to delegate work to specialists:
- `researcher` for info gathering
- `implementer` for drafting code/docs

Wait for their results with `agent_wait`, then synthesize.
""",
    },
    "rag": {
        "description": "Simple RAG with context_builder index",
        "app_yaml": """app:
  app_id: "{app_id}"
  name: "{name}"
  version: "1.0.0"
  description: "{description}"
  category: "data"

modules:
  context_builder:
    config:
      index:
        enabled: true
  llm_provider:
    config: {{}}

agents:
  - id: main
    role: assistant
    brain:
      provider: anthropic
      model: claude-sonnet-4-5
      config:
        api_key: "{{{{env.ANTHROPIC_API_KEY}}}}"
    system_prompt: "{{{{prompt.main}}}}"

execution:
  mode: interactive
  entry_agent: main
""",
        "prompts/main.md": """---
version: 1
---

You are a knowledge assistant for {{app.name}}.

Use the `search_index` tool to find relevant information before
answering. Cite sources. Say "I don't know" when the index has
nothing relevant.
""",
        "README.md": """# {name}

{description}

Index your documents with `digitorn app index-add <path>`.
""",
    },
    "researcher": {
        "description": "Deep research agent with web tools",
        "app_yaml": """app:
  app_id: "{app_id}"
  name: "{name}"
  version: "1.0.0"
  description: "{description}"
  category: "research"

modules:
  web:
    config: {{}}
  memory:
    config: {{}}
  llm_provider:
    config: {{}}

agents:
  - id: researcher
    role: assistant
    brain:
      provider: anthropic
      model: claude-opus-4-6
      config:
        api_key: "{{{{env.ANTHROPIC_API_KEY}}}}"
      temperature: 0.3
    system_prompt: "{{{{prompt.main}}}}"

execution:
  mode: interactive
  entry_agent: researcher
""",
        "prompts/main.md": """---
version: 1
description: "Deep research agent prompt"
---

You are {{app.name}}, a deep-research assistant.

Workflow for every query:
1. Decompose the question into searchable sub-questions
2. Use `web_search` to find 5+ sources per sub-question
3. Use `web_fetch` to read the most relevant ones fully
4. Cross-reference, spot contradictions
5. Write a structured report with citations

Be thorough. Never skip step 2 or 4.
""",
        "README.md": """# {name}

{description}

Deep research agent that uses the web module to gather and
cross-reference sources. Powered by Claude Opus.
""",
    },
}


@package_cli.command(name="new")
def new_package(
    name: str = typer.Argument(..., help="App name (kebab-case recommended)"),
    template: str = typer.Option(
        "chat", "--template", "-t",
        help="Template: chat | background | multi-agent | rag | researcher",
    ),
    directory: Path = typer.Option(
        Path("."), "--dir", "-d",
        help="Parent directory to create the new package in",
    ),
    description: str = typer.Option("", "--description"),
    force: bool = typer.Option(
        False, "--force",
        help="Overwrite existing directory",
    ),
) -> None:
    """Scaffold a new Digitorn app bundle from a template.

    Creates a directory with ``package.toml``, ``app.yaml``,
    ``prompts/``, ``skills/``, ``assets/``, and ``README.md``
    pre-filled based on the chosen template. The result is
    ready to ``digitorn app deploy``.

    Templates::

        chat         single-agent interactive chat
        background   cron-triggered background worker
        multi-agent  coordinator + specialist workers
        rag          knowledge assistant with index
        researcher   deep-research agent with web tools
    """
    if template not in _SCAFFOLD_TEMPLATES:
        available = ", ".join(sorted(_SCAFFOLD_TEMPLATES.keys()))
        console.print(
            f"[red]Unknown template '{template}'.[/red]\n"
            f"Available: {available}"
        )
        raise typer.Exit(1)

    # Sanitize the name to a valid app_id
    app_id = name.lower().replace("_", "-")
    if not _validate_kebab(app_id):
        console.print(
            f"[red]Invalid name '{name}'[/red] - must be "
            f"kebab-case (letters, digits, hyphens, 3-64 chars)"
        )
        raise typer.Exit(1)

    target = (directory / name).resolve()
    if target.exists():
        if not force:
            console.print(
                f"[red]Directory already exists:[/red] {target}\n"
                f"[dim]Use --force to overwrite.[/dim]"
            )
            raise typer.Exit(1)
        import shutil
        shutil.rmtree(target)

    target.mkdir(parents=True)
    (target / "prompts").mkdir()
    (target / "skills").mkdir()
    (target / "assets").mkdir()

    tpl = _SCAFFOLD_TEMPLATES[template]
    fmt_args = {
        "app_id": app_id,
        "name": name,
        "description": description or tpl["description"],
    }

    for rel_path, content in tpl.items():
        if rel_path == "description":
            continue
        file_path = target / rel_path
        file_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            file_path.write_text(content.format(**fmt_args), encoding="utf-8")
        except KeyError:
            # Template has a field we didn't supply - write as-is
            file_path.write_text(content, encoding="utf-8")

    # Placeholder icon - a 1x1 transparent PNG so {{asset.icon}}
    # resolves during the first compile. User can replace with
    # their real icon later.
    _write_placeholder_icon(target / "assets" / "icon.png")

    # Write package.toml
    (target / "package.toml").write_text(
        f"""[package]
id = "{app_id}"
name = "{name}"
version = "1.0.0"
description = "{fmt_args['description']}"
author = ""
license = "MIT"
icon = "assets/icon.png"
category = "{tpl.get('category', 'other')}"

[package.source]
type = "local"
publisher = ""

[package.compatibility]
digitorn_min = ">=1.0.0"
""",
        encoding="utf-8",
    )

    # .gitignore for common outputs
    (target / ".gitignore").write_text(
        "*.pyc\n__pycache__/\n.digitorn/\n*.log\n",
        encoding="utf-8",
    )

    console.print(
        f"[green]✓[/green] Created [cyan]{target}[/cyan]\n"
        f"  template: {template}\n"
        f"  app_id:   {app_id}\n\n"
        f"Next steps:\n"
        f"  [dim]cd {target}[/dim]\n"
        f"  [dim]# edit prompts/*.md and app.yaml to taste[/dim]\n"
        f"  [dim]digitorn app deploy ./app.yaml[/dim]\n"
    )


def _validate_kebab(s: str) -> bool:
    import re
    return bool(re.match(r"^[a-z][a-z0-9-]{2,63}$", s))


def _write_placeholder_icon(path: Path) -> None:
    """Write a 1x1 transparent PNG - avoids shipping binary blobs
    in the source tree while still giving the user a valid icon
    file on disk from day one."""
    import base64
    # 1x1 transparent PNG, base64-encoded
    png_b64 = (
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR4"
        "2mNgAAIAAAUAAen63NgAAAAASUVORK5CYII="
    )
    path.write_bytes(base64.b64decode(png_b64))


@package_cli.command(name="validate")
def validate_package(
    package_dir: Path = typer.Argument(
        ...,
        help="Path to a package directory (containing package.toml + app.yaml)",
    ),
    daemon: str = typer.Option(_DEFAULT_DAEMON, "--daemon", "-d"),
) -> None:
    """Validate a package directory: parse manifest + compile YAML.

    Works offline for the manifest parsing; calls the daemon's
    /api/discovery/compile route for the YAML compilation step.
    """
    if not package_dir.is_dir():
        console.print(f"[red]Not a directory:[/red] {package_dir}")
        raise typer.Exit(1)

    toml_path = package_dir / "package.toml"
    yaml_path = package_dir / "app.yaml"

    if not toml_path.is_file():
        console.print(f"[red]Missing package.toml in[/red] {package_dir}")
        raise typer.Exit(1)
    if not yaml_path.is_file():
        console.print(f"[red]Missing app.yaml in[/red] {package_dir}")
        raise typer.Exit(1)

    # 1. Parse manifest
    sys.path.insert(0, str(_packages_root()))
    from digitorn.core.packages import PackageManifest

    try:
        manifest = PackageManifest.from_path(toml_path)
        console.print(f"[green]✓[/green] package.toml parses ({manifest.id} v{manifest.version})")
    except Exception as exc:
        console.print(f"[red]package.toml validation failed:[/red] {exc}")
        raise typer.Exit(1)

    # 2. Compile YAML via the daemon
    yaml_text = yaml_path.read_text(encoding="utf-8")
    resp = daemon_request(
        "post",
        f"{daemon}/api/discovery/compile",
        json={"yaml": yaml_text, "source_path": str(yaml_path)},
    )
    body = resp.json()
    data = body.get("data") or {}
    if data.get("valid"):
        summary = data.get("summary") or {}
        console.print(
            f"[green]✓[/green] app.yaml compiles "
            f"(mode={summary.get('mode')}, agents={summary.get('agents')})"
        )
    else:
        console.print(f"[red]✗ app.yaml does not compile:[/red]")
        for e in data.get("errors") or []:
            console.print(f"  • {e}")
        raise typer.Exit(1)

    # 3. Cross-check: package.toml id must match app.yaml app.app_id
    summary = data.get("summary") or {}
    if summary.get("app_id") != manifest.id:
        console.print(
            f"[yellow]⚠ Mismatch:[/yellow] package.toml id is "
            f"'{manifest.id}' but app.yaml app_id is '{summary.get('app_id')}'"
        )

    console.print("\n[green]Package is valid ✓[/green]")


@package_cli.command(name="bundle")
def bundle_package(
    package_dir: Path = typer.Argument(...),
    output: Path = typer.Option(
        None, "--output", "-o",
        help="Output .dtpkg path. Defaults to <id>-<version>.dtpkg in cwd.",
    ),
) -> None:
    """Pack a package directory into a .dtpkg tar.gz archive.

    The .dtpkg format is a plain tar.gz of the package directory.
    A signature slot exists in the bundle layout but is not yet
    populated in v1 (deferred to the hub phase).
    """
    if not package_dir.is_dir():
        console.print(f"[red]Not a directory:[/red] {package_dir}")
        raise typer.Exit(1)
    toml_path = package_dir / "package.toml"
    if not toml_path.is_file():
        console.print(f"[red]Missing package.toml in[/red] {package_dir}")
        raise typer.Exit(1)

    sys.path.insert(0, str(_packages_root()))
    from digitorn.core.packages import PackageManifest

    try:
        manifest = PackageManifest.from_path(toml_path)
    except Exception as exc:
        console.print(f"[red]package.toml validation failed:[/red] {exc}")
        raise typer.Exit(1)

    if output is None:
        output = Path.cwd() / f"{manifest.id}-{manifest.version}.dtpkg"

    output.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(output, "w:gz") as tar:
        tar.add(package_dir, arcname=manifest.id)

    size_mb = output.stat().st_size / (1024 * 1024)
    console.print(
        f"[green]✓[/green] Bundled [cyan]{manifest.id}[/cyan] v{manifest.version}\n"
        f"  → {output}\n"
        f"  size: {size_mb:.2f} MB"
    )


# ────────────────────────────────────────────────────────────────────
# Helpers
# ────────────────────────────────────────────────────────────────────


def _packages_root() -> Path:
    """Locate the digitorn ``packages/`` directory (where the wheel installs to).

    Used by offline commands that need to import ``digitorn.core.packages``
    without a running daemon. In editable mode this is the repo's
    ``packages/`` directory; in production it's the site-packages.
    """
    # Resolve from this file's location: <root>/packages/digitorn/core/cli/package.py
    here = Path(__file__).resolve()
    # 5 parents up: cli → core → digitorn → packages → <root>
    return here.parents[4] if len(here.parents) > 4 else here.parent
