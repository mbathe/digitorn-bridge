"""CLI commands for MCP server management.

    digitorn mcp search <query>            — Search MCP server catalog
    digitorn mcp install <server_id>       — Install an MCP server
    digitorn mcp list                      — List installed servers
    digitorn mcp test <server_id>          — Test server connection
    digitorn mcp remove <server_id>        — Remove a server
    digitorn mcp pool                      — Show live pool status
    digitorn mcp health                    — Health check all servers
"""

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
    help="Manage MCP servers (search, install, test, configure).",
    no_args_is_help=True,
)

_DEFAULT_DAEMON = "http://127.0.0.1:8000"


@mcp_cli.command()
def search(
    query: Annotated[str, typer.Argument(help="Search query.")],
    daemon: str = typer.Option(_DEFAULT_DAEMON, "--daemon", "-d", help="Daemon URL."),
) -> None:
    """Search the MCP server catalog."""
    resp = daemon_request("get", f"{daemon}/api/mcp/search", params={"q": query})
    data = resp.json()

    results = data.get("results", data.get("data", []))
    if isinstance(results, dict):
        results = results.get("results", [])

    if not results:
        console.print(f"[dim]No results for '{query}'.[/dim]")
        return

    for r in results:
        if isinstance(r, dict):
            console.print(f"  [cyan]{r.get('id', r.get('name', '?'))}[/cyan] — {r.get('description', '')[:80]}")


@mcp_cli.command()
def install(
    server_id: Annotated[str, typer.Argument(help="Server ID to install.")],
    daemon: str = typer.Option(_DEFAULT_DAEMON, "--daemon", "-d", help="Daemon URL."),
) -> None:
    """Install an MCP server."""
    console.print(f"Installing [cyan]{server_id}[/cyan]...", end=" ")
    resp = daemon_request(
        "post",
        f"{daemon}/api/mcp/servers",
        json={"server_id": server_id, "config": {}},
        timeout=60.0,
    )
    data = resp.json()

    if resp.status_code in (200, 201):
        console.print("[green]OK[/green]")
    else:
        console.print("[red]FAILED[/red]")
        console.print(f"  {data.get('error', json.dumps(data)[:200])}")


@mcp_cli.command(name="list")
def list_servers(
    daemon: str = typer.Option(_DEFAULT_DAEMON, "--daemon", "-d", help="Daemon URL."),
) -> None:
    """List all installed MCP servers."""
    resp = daemon_request("get", f"{daemon}/api/mcp/servers")
    data = resp.json()

    servers = data.get("servers", data.get("data", []))
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

    for s in servers:
        if isinstance(s, dict):
            status = s.get("status", "?")
            style = "green" if status == "connected" else "yellow"
            table.add_row(
                s.get("server_id", s.get("id", "?")),
                f"[{style}]{status}[/{style}]",
                str(s.get("tools_count", "?")),
                s.get("transport", "stdio"),
            )

    console.print(table)


@mcp_cli.command()
def test(
    server_id: Annotated[str, typer.Argument(help="Server ID to test.")],
    daemon: str = typer.Option(_DEFAULT_DAEMON, "--daemon", "-d", help="Daemon URL."),
) -> None:
    """Test an MCP server connection."""
    console.print(f"Testing [cyan]{server_id}[/cyan]...", end=" ")
    resp = daemon_request("post", f"{daemon}/api/mcp/servers/{server_id}/test", timeout=30.0)
    data = resp.json()

    if data.get("ok", data.get("success")):
        console.print("[green]OK[/green]")
    else:
        console.print("[red]FAILED[/red]")
        console.print(f"  {data.get('error', '?')}")


@mcp_cli.command()
def remove(
    server_id: Annotated[str, typer.Argument(help="Server ID to remove.")],
    daemon: str = typer.Option(_DEFAULT_DAEMON, "--daemon", "-d", help="Daemon URL."),
) -> None:
    """Remove an installed MCP server."""
    resp = daemon_request("delete", f"{daemon}/api/mcp/servers/{server_id}")
    data = resp.json()

    if data.get("status") == "removed" or data.get("success"):
        console.print(f"[green]Removed[/green] {server_id}")
    else:
        console.print(f"[red]Failed[/red] — {data.get('error', '?')}")


@mcp_cli.command()
def pool(
    daemon: str = typer.Option(_DEFAULT_DAEMON, "--daemon", "-d", help="Daemon URL."),
) -> None:
    """Show live MCP connection pool status."""
    resp = daemon_request("get", f"{daemon}/api/mcp/pool")
    console.print(json.dumps(resp.json(), indent=2))


@mcp_cli.command()
def health(
    daemon: str = typer.Option(_DEFAULT_DAEMON, "--daemon", "-d", help="Daemon URL."),
) -> None:
    """Health check all connected MCP servers."""
    resp = daemon_request("get", f"{daemon}/api/mcp/pool/health")
    data = resp.json()

    results = data.get("results", data)
    if isinstance(results, dict):
        for sid, status in results.items():
            ok = status.get("ok", True) if isinstance(status, dict) else status
            style = "green" if ok else "red"
            console.print(f"  [{style}]{sid}[/{style}]")
    else:
        console.print(json.dumps(data, indent=2))
