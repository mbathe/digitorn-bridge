"""CLI commands for MCP server management."""

from __future__ import annotations

import json
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from digitorn.core.cli.auth_helpers import daemon_request

console = Console()

mcp_cli = typer.Typer(
    name="mcp",
    help="Manage MCP servers (search, install, configure, test).",
    no_args_is_help=True,
)

_DEFAULT_DAEMON = "http://127.0.0.1:8000"


def _parse_env_pairs(pairs: list[str]) -> dict[str, str]:
    """Parse `--env KEY=VALUE` repeated flags into a flat dict."""
    out: dict[str, str] = {}
    for raw in pairs:
        if "=" not in raw:
            console.print(f"[red]Invalid --env value: {raw!r} (need KEY=VALUE)[/red]")
            raise typer.Exit(1)
        k, v = raw.split("=", 1)
        out[k.strip()] = v
    return out


def _ok(resp) -> bool:
    """Daemon may wrap responses in `{success, data, error}` envelope OR"""
    if resp.status_code >= 400:
        return False
    body = resp.json() if resp.content else {}
    if isinstance(body, dict) and body.get("success") is False:
        return False
    return True


def _err_msg(resp) -> str:
    """Best-effort error extraction from any daemon response."""
    try:
        body = resp.json()
    except Exception:
        return f"HTTP {resp.status_code}"
    if isinstance(body, dict):
        return (
            body.get("error")
            or body.get("detail")
            or body.get("message")
            or f"HTTP {resp.status_code}"
        )
    return f"HTTP {resp.status_code}"


def _status_style(status: str) -> str:
    """DB status → rich color tag."""
    return {
        "ready": "green",
        "tested": "cyan",
        "installed": "yellow",
        "error": "red",
    }.get(status, "white")


@mcp_cli.command()
def search(
    query: Annotated[str, typer.Argument(help="Search query.")],
    daemon: str = typer.Option(_DEFAULT_DAEMON, "--daemon", "-d"),
) -> None:
    """Search catalog + registry."""
    resp = daemon_request("get", f"{daemon}/api/mcp/search", params={"q": query})
    if not _ok(resp):
        console.print(f"[red]{_err_msg(resp)}[/red]")
        raise typer.Exit(1)
    data = resp.json()
    results = data.get("results", []) if isinstance(data, dict) else []
    if isinstance(results, dict):
        results = results.get("results", [])
    if not results:
        console.print(f"[dim]No results for '{query}'.[/dim]")
        return

    table = Table(title=f"MCP search: {query!r}", show_lines=False)
    table.add_column("Server ID", style="cyan")
    table.add_column("Source")
    table.add_column("Runtime")
    table.add_column("Description", style="dim")
    for r in results:
        if not isinstance(r, dict):
            continue
        sid = r.get("server_id") or r.get("id") or "?"
        src = r.get("source", "?")
        rt = r.get("runtime", "?")
        desc = (r.get("description") or "")[:80]
        table.add_row(sid, src, rt, desc)
    console.print(table)


@mcp_cli.command()
def browse(
    query: str = typer.Option("", "--query", "-q", help="Filter the registry."),
    limit: int = typer.Option(50, "--limit", "-l", min=1, max=100),
    cursor: str | None = typer.Option(None, "--cursor", help="Pagination cursor."),
    daemon: str = typer.Option(_DEFAULT_DAEMON, "--daemon", "-d"),
) -> None:
    """Browse the official MCP registry (~800 servers)."""
    params: dict[str, str | int] = {"limit": limit}
    if query:
        params["q"] = query
    if cursor:
        params["cursor"] = cursor
    resp = daemon_request(
        "get", f"{daemon}/api/mcp/registry/browse", params=params, timeout=15.0,
    )
    if not _ok(resp):
        console.print(f"[red]{_err_msg(resp)}[/red]")
        raise typer.Exit(1)
    data = resp.json() or {}
    servers = data.get("servers") or []
    next_cursor = data.get("next_cursor")

    if not servers:
        console.print(
            f"[dim]Registry returned no servers"
            f"{f' for {query!r}' if query else ''}.[/dim]"
        )
        return

    table = Table(
        title=f"Registry · {len(servers)} servers"
        f"{f' matching {query!r}' if query else ''}",
        show_lines=False,
    )
    table.add_column("Server ID", style="cyan")
    table.add_column("Runtime")
    table.add_column("Transport")
    table.add_column("Env", justify="right")
    table.add_column("Description", style="dim")
    for s in servers:
        if not isinstance(s, dict):
            continue
        sid = s.get("server_id", "?")
        rt = s.get("runtime", "none")
        tr = s.get("transport", "?")
        env_count = str(s.get("required_env_count", 0))
        desc = (s.get("description") or "")[:80]
        sid_color = "yellow" if s.get("status") == "deprecated" else "cyan"
        table.add_row(f"[{sid_color}]{sid}[/{sid_color}]", rt, tr, env_count, desc)
    console.print(table)
    if next_cursor:
        console.print(
            f"\n[dim]More results available. Run with[/dim] "
            f"[cyan]--cursor {next_cursor}[/cyan]"
        )


@mcp_cli.command()
def requirements(
    server_id: Annotated[str, typer.Argument(help="Server ID.")],
    daemon: str = typer.Option(_DEFAULT_DAEMON, "--daemon", "-d"),
) -> None:
    """Show credentials a server needs BEFORE installing it.

    Works for both catalog AND registry servers - saves you a 30s
    npm install only to discover you needed a token.
    """
    resp = daemon_request(
        "get", f"{daemon}/api/mcp/requirements/{server_id}", timeout=15.0,
    )
    if resp.status_code == 404:
        console.print(
            f"[red]Server '{server_id}' not in catalog or registry.[/red]"
        )
        console.print(
            f"[dim]Try:[/dim] [cyan]digitorn mcp search {server_id}[/cyan]"
        )
        raise typer.Exit(1)
    if not _ok(resp):
        console.print(f"[red]{_err_msg(resp)}[/red]")
        raise typer.Exit(1)

    data = resp.json() or {}
    console.print(
        f"\n[bold cyan]{data.get('display_name', server_id)}[/bold cyan]"
        f"  [dim]({data.get('source', '?')})[/dim]"
    )
    desc = data.get("description") or ""
    if desc:
        console.print(f"[dim]{desc[:200]}[/dim]")
    console.print(
        f"\n  transport:  {data.get('transport', '?')}\n"
        f"  runtime:    {data.get('runtime', '?')}\n"
        f"  package:    {data.get('package', '?')}\n"
        f"  oauth:      {data.get('oauth', False)} "
        f"{'(' + data['oauth_provider'] + ')' if data.get('oauth_provider') else ''}"
    )

    creds = data.get("credentials") or []
    if not creds:
        console.print("\n[green]No credentials needed - this server has no config.[/green]")
        return

    table = Table(title="Required credentials", show_lines=False)
    table.add_column("Key", style="cyan")
    table.add_column("Env var")
    table.add_column("Required")
    table.add_column("Description", style="dim")
    for c in creds:
        if not isinstance(c, dict):
            continue
        req = "yes" if c.get("required") else "no"
        table.add_row(
            c.get("key", "?"),
            c.get("env_var", ""),
            req,
            (c.get("description") or "")[:80],
        )
    console.print(table)

    yaml_ex = data.get("yaml_example")
    if yaml_ex:
        console.print("\n[dim]YAML example:[/dim]")
        console.print(f"[cyan]{yaml_ex}[/cyan]")


@mcp_cli.command()
def install(
    server_id: Annotated[str, typer.Argument(help="Server ID to install.")],
    env: list[str] = typer.Option(
        [],
        "--env",
        "-e",
        help="Set a credential / env var (KEY=VALUE). Repeatable.",
    ),
    token: str | None = typer.Option(
        None, "--token", "-t",
        help="Shorthand for the most common credential key (token).",
    ),
    test: bool = typer.Option(
        False, "--test/--no-test",
        help="Run a connection test right after install.",
    ),
    enable: bool = typer.Option(
        False, "--enable",
        help="Auto-start on daemon boot.",
    ),
    daemon: str = typer.Option(_DEFAULT_DAEMON, "--daemon", "-d"),
) -> None:
    """Install an MCP server.

    Examples:

      digitorn mcp install github --token ghp_xxx --test --enable
      digitorn mcp install slack --env SLACK_BOT_TOKEN=xoxb-... --env SLACK_TEAM_ID=T123
      digitorn mcp install todoist --env your_api_key=xxx
    """
    config = _parse_env_pairs(env)
    if token:
        config["token"] = token

    console.print(
        f"Installing [cyan]{server_id}[/cyan]"
        f"{' [dim](this can take 30s+ for npm/pip packages)[/dim]' if not config else ''}..."
    )
    resp = daemon_request(
        "post",
        f"{daemon}/api/mcp/servers",
        json={"server_id": server_id, "config": config},
        timeout=180.0,
    )
    if not _ok(resp):
        console.print(f"[red]✗ Install failed[/red]: {_err_msg(resp)}")
        raise typer.Exit(1)
    console.print(f"[green]✓ Installed[/green] {server_id}")

    if test:
        console.print(f"Testing [cyan]{server_id}[/cyan]...")
        tresp = daemon_request(
            "post",
            f"{daemon}/api/mcp/servers/{server_id}/test",
            timeout=60.0,
        )
        if not _ok(tresp):
            console.print(f"  [yellow]Test skipped: {_err_msg(tresp)}[/yellow]")
        else:
            td = tresp.json() or {}
            ok = bool(td.get("connected")) and td.get("error") is None
            color = "green" if ok else "red"
            mark = "✓" if ok else "✗"
            tools = td.get("tools") or []
            console.print(
                f"  [{color}]{mark}[/{color}] connected={td.get('connected')} "
                f"health={td.get('health')} tools={len(tools)}"
            )
            if not ok and td.get("error"):
                console.print(f"  [red]error:[/red] {td['error'][:200]}")

    if enable:
        eresp = daemon_request(
            "put",
            f"{daemon}/api/mcp/servers/{server_id}/config",
            json={"config": {"auto_start": True}},
        )
        if _ok(eresp):
            console.print(f"[green]✓ auto_start enabled[/green]")
        else:
            console.print(f"[yellow]auto_start toggle failed: {_err_msg(eresp)}[/yellow]")


@mcp_cli.command(name="list")
def list_servers(
    status: str | None = typer.Option(
        None, "--status", "-s", help="Filter by status."
    ),
    daemon: str = typer.Option(_DEFAULT_DAEMON, "--daemon", "-d"),
) -> None:
    """List all installed MCP servers."""
    params: dict[str, str] = {}
    if status:
        params["status"] = status
    resp = daemon_request("get", f"{daemon}/api/mcp/servers", params=params)
    if not _ok(resp):
        console.print(f"[red]{_err_msg(resp)}[/red]")
        raise typer.Exit(1)
    data = resp.json() or {}
    servers = data.get("servers", []) if isinstance(data, dict) else []
    if isinstance(servers, dict):
        servers = servers.get("servers", [])
    if not servers:
        console.print("[dim]No MCP servers installed.[/dim]")
        return

    table = Table(title="MCP Servers")
    table.add_column("Server ID", style="cyan")
    table.add_column("Status")
    table.add_column("Tools", justify="right")
    table.add_column("Transport")
    table.add_column("Source")
    table.add_column("Auto-start", justify="center")
    for s in servers:
        if not isinstance(s, dict):
            continue
        status_val = s.get("status", "?")
        style = _status_style(status_val)
        tools = s.get("tools_count")
        auto = "✓" if s.get("auto_start") else ""
        table.add_row(
            s.get("server_id", "?"),
            f"[{style}]{status_val}[/{style}]",
            str(tools) if tools is not None else "?",
            s.get("transport", "stdio"),
            s.get("source", "?"),
            auto,
        )
    console.print(table)


@mcp_cli.command()
def info(
    server_id: Annotated[str, typer.Argument(help="Server ID.")],
    daemon: str = typer.Option(_DEFAULT_DAEMON, "--daemon", "-d"),
) -> None:
    """Full details on an installed server (tools, env, status, health)."""
    resp = daemon_request("get", f"{daemon}/api/mcp/servers/{server_id}")
    if resp.status_code == 404:
        console.print(f"[red]Server '{server_id}' not installed.[/red]")
        raise typer.Exit(1)
    if not _ok(resp):
        console.print(f"[red]{_err_msg(resp)}[/red]")
        raise typer.Exit(1)
    s = resp.json() or {}

    console.print(
        f"\n[bold cyan]{s.get('display_name', server_id)}[/bold cyan] "
        f"[{_status_style(s.get('status', '?'))}]{s.get('status', '?')}[/]"
    )
    if s.get("description"):
        console.print(f"[dim]{s['description'][:200]}[/dim]")
    console.print(
        f"\n  source:     {s.get('source', '?')}\n"
        f"  transport:  {s.get('transport', '?')}\n"
        f"  runtime:    {s.get('runtime', '?')}\n"
        f"  package:    {s.get('package', '')}\n"
        f"  command:    {s.get('command', '')}\n"
        f"  url:        {s.get('url', '')}\n"
        f"  timeout:    {s.get('timeout', '?')}s\n"
        f"  rate_limit: {s.get('rate_limit_rpm', 'n/a')} rpm\n"
        f"  auto_start: {s.get('auto_start', False)}\n"
        f"  installed:  {s.get('installed_at', '?')}\n"
        f"  health:     {s.get('health_ok')} (last check: {s.get('last_health_check', 'never')})"
    )
    if s.get("status_message"):
        console.print(f"\n[yellow]Status message:[/yellow] {s['status_message']}")

    tools = s.get("tools") or []
    if tools:
        table = Table(title=f"Tools ({len(tools)})")
        table.add_column("Name", style="cyan")
        table.add_column("Description", style="dim")
        for t in tools[:50]:
            if isinstance(t, dict):
                table.add_row(
                    t.get("name", "?"),
                    (t.get("description") or "")[:80],
                )
        console.print(table)
        if len(tools) > 50:
            console.print(f"[dim]... and {len(tools) - 50} more[/dim]")


@mcp_cli.command()
def config(
    server_id: Annotated[str, typer.Argument(help="Server ID.")],
    set_pairs: list[str] = typer.Option(
        [],
        "--set", "-s",
        help="KEY=VALUE pair to set / update. Repeatable.",
    ),
    daemon: str = typer.Option(_DEFAULT_DAEMON, "--daemon", "-d"),
) -> None:
    """Update credentials / options on an installed server.

    The daemon maps shorthand keys to the right env var via the catalog
    or registry mapping. Status auto-resets to `installed` so a
    re-test is required before agents pick up the change.

    Examples:

      digitorn mcp config github --set token=ghp_xxx
      digitorn mcp config slack --set bot_token=xoxb-... --set team_id=T123
    """
    if not set_pairs:
        console.print(
            "[red]Nothing to set. Use --set KEY=VALUE (repeatable).[/red]"
        )
        raise typer.Exit(1)
    new_config = _parse_env_pairs(set_pairs)
    resp = daemon_request(
        "put",
        f"{daemon}/api/mcp/servers/{server_id}/config",
        json={"config": new_config},
    )
    if not _ok(resp):
        console.print(f"[red]✗ Config update failed[/red]: {_err_msg(resp)}")
        raise typer.Exit(1)
    console.print(
        f"[green]✓ Updated[/green] {server_id} ({len(new_config)} key(s)). "
        f"[dim]Run[/dim] [cyan]digitorn mcp test {server_id}[/cyan] [dim]to verify.[/dim]"
    )


@mcp_cli.command()
def test(
    server_id: Annotated[str, typer.Argument(help="Server ID to test.")],
    daemon: str = typer.Option(_DEFAULT_DAEMON, "--daemon", "-d"),
) -> None:
    """Connect, list tools, run a healthcheck."""
    console.print(f"Testing [cyan]{server_id}[/cyan]...")
    resp = daemon_request(
        "post", f"{daemon}/api/mcp/servers/{server_id}/test", timeout=60.0,
    )
    if resp.status_code == 404:
        console.print(f"[red]Server '{server_id}' not installed.[/red]")
        raise typer.Exit(1)
    if not _ok(resp):
        console.print(f"[red]{_err_msg(resp)}[/red]")
        raise typer.Exit(1)
    data = resp.json() or {}
    # Real shape: {connected, tools, resources_count, prompts_count, health, error}
    connected = bool(data.get("connected"))
    health = bool(data.get("health"))
    tools = data.get("tools") or []
    if connected and health:
        console.print(
            f"[green]✓ OK[/green]  connected · health · {len(tools)} tools"
        )
    elif connected:
        console.print(
            f"[yellow]~ Partial[/yellow]  connected but health failed · {len(tools)} tools"
        )
    else:
        console.print("[red]✗ FAILED[/red]")
        if data.get("error"):
            console.print(f"  {data['error'][:300]}")


def _toggle_auto_start(server_id: str, daemon: str, enable: bool) -> None:
    resp = daemon_request(
        "put",
        f"{daemon}/api/mcp/servers/{server_id}/config",
        json={"config": {"auto_start": enable}},
    )
    if not _ok(resp):
        console.print(f"[red]{_err_msg(resp)}[/red]")
        raise typer.Exit(1)
    label = "enabled" if enable else "disabled"
    console.print(f"[green]✓ {server_id} auto_start {label}[/green]")


@mcp_cli.command()
def enable(
    server_id: Annotated[str, typer.Argument(help="Server ID.")],
    daemon: str = typer.Option(_DEFAULT_DAEMON, "--daemon", "-d"),
) -> None:
    """Auto-start this server when the daemon boots."""
    _toggle_auto_start(server_id, daemon, True)


@mcp_cli.command()
def disable(
    server_id: Annotated[str, typer.Argument(help="Server ID.")],
    daemon: str = typer.Option(_DEFAULT_DAEMON, "--daemon", "-d"),
) -> None:
    """Stop auto-starting this server. Existing connections stay alive."""
    _toggle_auto_start(server_id, daemon, False)


@mcp_cli.command()
def remove(
    server_id: Annotated[str, typer.Argument(help="Server ID to remove.")],
    daemon: str = typer.Option(_DEFAULT_DAEMON, "--daemon", "-d"),
) -> None:
    """Remove an installed MCP server (uninstall + cleanup)."""
    resp = daemon_request("delete", f"{daemon}/api/mcp/servers/{server_id}")
    if resp.status_code == 404:
        console.print(f"[yellow]{server_id} was not installed.[/yellow]")
        return
    if not _ok(resp):
        console.print(f"[red]✗ Remove failed[/red]: {_err_msg(resp)}")
        raise typer.Exit(1)
    console.print(f"[green]✓ Removed[/green] {server_id}")


@mcp_cli.command()
def pool(
    daemon: str = typer.Option(_DEFAULT_DAEMON, "--daemon", "-d"),
) -> None:
    """Live MCP connection pool status (per-server ref-count + apps)."""
    resp = daemon_request("get", f"{daemon}/api/mcp/pool")
    if not _ok(resp):
        console.print(f"[red]{_err_msg(resp)}[/red]")
        raise typer.Exit(1)
    data = resp.json() or {}
    servers = data.get("servers") or []
    if not servers:
        msg = data.get("message") or "No servers in the daemon pool."
        console.print(f"[dim]{msg}[/dim]")
        return

    table = Table(title="MCP Pool")
    table.add_column("Server ID", style="cyan")
    table.add_column("Status")
    table.add_column("Transport")
    table.add_column("Tools", justify="right")
    table.add_column("Apps", justify="right")
    table.add_column("App refs", style="dim")
    for s in servers:
        if not isinstance(s, dict):
            continue
        st = s.get("status", "?")
        style = "green" if st == "connected" else "yellow"
        refs = s.get("app_refs") or []
        table.add_row(
            s.get("server_id", "?"),
            f"[{style}]{st}[/{style}]",
            s.get("transport_type", "?"),
            str(s.get("tools_count", "?")),
            str(s.get("ref_count", 0)),
            ", ".join(refs[:3]) + (" …" if len(refs) > 3 else ""),
        )
    console.print(table)


@mcp_cli.command()
def health(
    daemon: str = typer.Option(_DEFAULT_DAEMON, "--daemon", "-d"),
) -> None:
    """Health check all connected pool servers."""
    resp = daemon_request("get", f"{daemon}/api/mcp/pool/health")
    if not _ok(resp):
        console.print(f"[red]{_err_msg(resp)}[/red]")
        raise typer.Exit(1)
    body = resp.json() or {}
    # The endpoint wraps in {success, data: {results, pool_initialized}}.
    # Older deployments returned bare {results: {...}}. Handle both.
    data = body.get("data") if isinstance(body.get("data"), dict) else body
    results = data.get("results") if isinstance(data.get("results"), dict) else {}
    if not results:
        if not data.get("pool_initialized", True):
            console.print("[dim]Daemon pool not initialised.[/dim]")
        else:
            console.print("[dim]No servers in the pool.[/dim]")
        return

    table = Table(title="MCP Health")
    table.add_column("Server ID", style="cyan")
    table.add_column("OK", justify="center")
    for sid, ok in sorted(results.items()):
        if isinstance(ok, dict):
            ok = ok.get("ok", False)
        mark = "[green]✓[/green]" if ok else "[red]✗[/red]"
        table.add_row(sid, mark)
    console.print(table)


registry_cli = typer.Typer(
    name="registry",
    help="Manage the official MCP registry cache.",
    no_args_is_help=True,
)
mcp_cli.add_typer(registry_cli)


@registry_cli.command(name="refresh")
def registry_refresh(
    daemon: str = typer.Option(_DEFAULT_DAEMON, "--daemon", "-d"),
) -> None:
    """Force-refresh the daemon's registry cache. **Admin-only.**"""
    resp = daemon_request("post", f"{daemon}/api/mcp/registry/refresh")
    if resp.status_code == 403:
        console.print("[red]✗ Admin permission required.[/red]")
        raise typer.Exit(1)
    if not _ok(resp):
        console.print(f"[red]{_err_msg(resp)}[/red]")
        raise typer.Exit(1)
    body = resp.json() or {}
    cleared = body.get("cleared", 0)
    console.print(f"[green]✓ Registry cache flushed[/green] ({cleared} entries cleared)")
