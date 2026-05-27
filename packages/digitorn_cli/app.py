"""CLI commands for app YAML management."""

from __future__ import annotations

import logging
from pathlib import Path
import typer

logger = logging.getLogger(__name__)
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.syntax import Syntax

console = Console()

app_cli = typer.Typer(
    name="app",
    help="Application YAML management.",
    no_args_is_help=True,
)


_DEFAULT_DAEMON = "http://127.0.0.1:8000"


@app_cli.command()
def validate(
    path: Path = typer.Argument(..., help="Path to the app YAML file."),
    daemon: str = typer.Option(_DEFAULT_DAEMON, "--daemon", "-d", help="Daemon URL."),
) -> None:
    """Validate an app YAML file without executing it.

    Sends the YAML path to the daemon for compilation against
    the loaded module registry. No local module imports needed.
    """
    from digitorn_cli.auth import daemon_request

    if not path.exists():
        console.print(f"[bold red]File not found: {path}[/bold red]")
        raise typer.Exit(1)

    resp = daemon_request(
        "post",
        f"{daemon}/api/apps/validate",
        daemon=daemon,
        json={"yaml_path": str(path.resolve())},
        timeout=30.0,
    )
    data = resp.json()

    if not data.get("success"):
        errors = data.get("data", {}).get("errors", [data.get("error", "Unknown error")])
        console.print(f"\n[bold red]Validation failed ({len(errors)} error(s)):[/bold red]\n")
        for i, err in enumerate(errors, 1):
            console.print(f"  {i}. {err}")
        console.print()
        raise typer.Exit(1)

    info = data["data"]
    table = Table(title=f"App: {info.get('name', info['app_id'])}", border_style="green")
    table.add_column("Property", style="cyan")
    table.add_column("Value", style="green")
    table.add_row("app_id", info["app_id"])
    table.add_row("version", info.get("version", "?"))
    table.add_row("modules", ", ".join(info.get("modules", [])))
    table.add_row("setup steps", str(info.get("setup_steps", 0)))
    table.add_row("constrained modules", ", ".join(info.get("constrained_modules", [])) or "none")
    table.add_row("security policy", info.get("security_policy") or "none (no capabilities block)")
    table.add_row("max risk level", info.get("max_risk_level") or "n/a")

    console.print()
    console.print(Panel(table, title="[bold]Validation OK[/bold]", border_style="green"))
    console.print()


@app_cli.command()
def schema(
    module_id: str = typer.Argument(..., help="Module ID to show YAML schema for."),
    daemon: str = typer.Option(_DEFAULT_DAEMON, "--daemon", "-d", help="Daemon URL."),
) -> None:
    """Show the YAML configuration schema for a module.

    Fetches module details from the daemon API and displays all
    available actions with their parameter schemas.
    """
    from digitorn_cli.auth import daemon_request

    resp = daemon_request("get", f"{daemon}/api/modules/{module_id}", daemon=daemon)
    if resp.status_code == 404:
        console.print(f"[bold red]Module '{module_id}' not found.[/bold red]")
        # Try to list available modules
        lr = daemon_request("get", f"{daemon}/api/modules", daemon=daemon)
        if lr.status_code == 200:
            mods = lr.json().get("modules", [])
            names = [m.get("module_id", "") for m in mods if m.get("status") == "loaded"]
            if names:
                console.print(f"Available: {', '.join(sorted(names))}")
        raise typer.Exit(1)

    data = resp.json()

    table = Table(title=f"Actions - {module_id}", border_style="cyan")
    table.add_column("Action", style="bold")
    table.add_column("Risk", style="yellow")
    table.add_column("Params", style="dim")

    actions = data.get("actions", [])
    for action in actions:
        # Extract param names from input_schema
        input_schema = action.get("input_schema", {})
        param_names = list(input_schema.get("properties", {}).keys())
        table.add_row(
            action.get("name", "?"),
            action.get("risk_level", "low"),
            ", ".join(param_names) if param_names else "-",
        )

    console.print()
    console.print(table)
    console.print()


@app_cli.command()
def deploy(
    path: Path = typer.Argument(..., help="Path to the app YAML file."),
    daemon: str = typer.Option(_DEFAULT_DAEMON, "--daemon", "-d", help="Daemon URL."),
    force: bool = typer.Option(False, "--force", "-f", help="Force redeploy if already deployed."),
) -> None:
    """Deploy an app to the running daemon.

    Compiles, bootstraps, and registers the app. The daemon must be running.

    Examples:
        digitorn app deploy my-app.yaml
        digitorn app deploy my-app.yaml --force
    """
    from digitorn_cli.auth import daemon_request

    if not path.exists():
        console.print(f"[bold red]File not found: {path}[/bold red]")
        raise typer.Exit(1)

    import re as _re
    from rich.prompt import Prompt as _Prompt

    yaml_text = path.read_text(encoding="utf-8")
    needed_secrets = sorted(set(_re.findall(r"\{\{secret\.(\w+)\}\}", yaml_text)))
    needed_env = sorted(set(_re.findall(r"\{\{env\.(\w+)\}\}", yaml_text)))

    import os as _os

    inline_secrets: dict[str, str] = {}

    # Prompt for env vars missing from the daemon's environment
    missing_env = [k for k in needed_env if not _os.environ.get(k)]
    if missing_env:
        console.print(f"\n  This app requires {len(missing_env)} environment variable(s):\n")
        for key in missing_env:
            value = _Prompt.ask(f"  {key}", password=True)
            if not value:
                console.print("[red]  Cancelled.[/red]")
                raise typer.Exit(1)
            inline_secrets[key] = value
        console.print()
    if needed_secrets:
        raw_yaml = __import__("yaml").safe_load(yaml_text)
        _app_id = (raw_yaml.get("app") or {}).get("app_id", "")

        existing_keys: list[str] = []
        if _app_id:
            try:
                check_resp = daemon_request("get", f"{daemon}/api/apps/{_app_id}/secrets", daemon=daemon, timeout=5.0)
                if check_resp.status_code == 200:
                    existing_keys = check_resp.json().get("data", {}).get("keys", [])
            except Exception:
                logger.debug("failed to fetch existing secrets from daemon", exc_info=True)

        missing = [k for k in needed_secrets if k not in existing_keys]
        if missing:
            console.print(f"\n  This app requires {len(missing)} secret(s):\n")
            for key in missing:
                value = _Prompt.ask(f"  {key}", password=True)
                if not value:
                    console.print("[red]  Cancelled.[/red]")
                    raise typer.Exit(1)
                inline_secrets[key] = value
            console.print()

    url = f"{daemon}/api/apps/deploy"
    try:
        resp = daemon_request(
            "post",
            url,
            daemon=daemon,
            json={
                "yaml_path": str(path.resolve()),
                "force": force,
                "secrets": inline_secrets if inline_secrets else None,
            },
            timeout=300.0,
        )
        data = resp.json()
    except typer.Exit:
        raise
    except Exception as exc:
        console.print(f"[bold red]Error: {exc}[/bold red]")
        raise typer.Exit(1)

    if not data.get("success"):
        console.print(f"\n[bold red]Deploy failed:[/bold red] {data.get('error')}\n")
        raise typer.Exit(1)

    info = data.get("data") or {}
    app_id = info.get("app_id", "?")
    # `/api/apps/deploy` returns `status: "deploying"` and runs the actual
    # build in a background task. Poll until the app is fully loaded so the
    # "App Deployed" table shows real mode/agents instead of crashing on
    # missing keys.
    if info.get("status") == "deploying":
        import time as _time
        deadline = _time.monotonic() + 90.0
        last_err: str | None = None
        ready_info: dict | None = None
        while _time.monotonic() < deadline:
            try:
                check = daemon_request("get", f"{daemon}/api/apps/{app_id}", daemon=daemon)
                cdata = check.json()
                if cdata.get("success"):
                    cinfo = cdata.get("data") or {}
                    if cinfo.get("mode") and cinfo.get("agents"):
                        ready_info = cinfo
                        break
                try:
                    err_resp = daemon_request(
                        "get",
                        f"{daemon}/api/apps/{app_id}/deploy-status",
                        daemon=daemon,
                    )
                    err_data = err_resp.json()
                    if err_data.get("success"):
                        ed = err_data.get("data") or {}
                        if ed.get("error"):
                            last_err = ed["error"]
                            break
                except Exception as exc:
                    logger.debug("deploy-status best-effort failed: %s", exc)
            except Exception as exc:
                logger.debug("deploy poll best-effort failed: %s", exc)
            _time.sleep(1.0)
        if last_err:
            console.print(f"\n[bold red]Deploy failed:[/bold red] {last_err}\n")
            raise typer.Exit(1)
        if ready_info is None:
            console.print(
                f"\n[yellow]Deploy still in progress after 90s. "
                f"Run `digitorn app list` later to confirm.[/yellow]\n"
            )
            return
        info = ready_info

    table = Table(title=f"App Deployed: {info.get('name', app_id)}", border_style="green")
    table.add_column("Property", style="cyan")
    table.add_column("Value", style="green")
    table.add_row("app_id", info.get("app_id", "?"))
    table.add_row("version", str(info.get("version", "?")))
    table.add_row("mode", info.get("mode", "?"))
    table.add_row("agents", ", ".join(info.get("agents") or []) or "-")
    table.add_row("modules", ", ".join(info.get("modules") or []) or "-")
    table.add_row(
        "tools",
        f"{info.get('total_tools', 0)} across "
        f"{info.get('total_categories', 0)} modules",
    )
    console.print()
    console.print(Panel(table, border_style="green"))
    console.print()


@app_cli.command(name="list")
def list_apps(
    daemon: str = typer.Option(_DEFAULT_DAEMON, "--daemon", "-d", help="Daemon URL."),
) -> None:
    """List all deployed apps on the daemon."""
    from digitorn_cli.auth import daemon_request

    url = f"{daemon}/api/apps"
    resp = daemon_request("get", url, daemon=daemon)
    data = resp.json()

    apps = data.get("data", [])
    if not apps:
        console.print("\n[dim]No apps deployed.[/dim]\n")
        return

    table = Table(title="Deployed Apps", border_style="cyan")
    table.add_column("App ID", style="bold")
    table.add_column("Name")
    table.add_column("Mode", style="cyan")
    table.add_column("Agents", style="dim")
    table.add_column("Tools", style="green")

    for app_info in apps:
        table.add_row(
            app_info["app_id"],
            app_info["name"],
            app_info["mode"],
            ", ".join(app_info["agents"]),
            str(app_info["total_tools"]),
        )

    console.print()
    console.print(table)
    console.print()


@app_cli.command()
def undeploy(
    app_id: str = typer.Argument(..., help="App ID to undeploy."),
    daemon: str = typer.Option(_DEFAULT_DAEMON, "--daemon", "-d", help="Daemon URL."),
) -> None:
    """Undeploy an app from the daemon.

    Gracefully stops all modules and removes the app.
    """
    from digitorn_cli.auth import daemon_request

    url = f"{daemon}/api/apps/{app_id}"
    resp = daemon_request("delete", url, daemon=daemon)

    if resp.status_code == 200:
        console.print(f"\n[bold green]App '{app_id}' undeployed.[/bold green]\n")
    elif resp.status_code == 404:
        console.print(f"\n[bold red]App '{app_id}' not found.[/bold red]\n")
        raise typer.Exit(1)
    else:
        data = resp.json()
        console.print(f"\n[bold red]Error: {data.get('detail', resp.text)}[/bold red]\n")
        raise typer.Exit(1)
