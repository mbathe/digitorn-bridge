"""CLI commands for session management (user's own sessions)."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

console = Console()

sessions_cli = typer.Typer(
    name="sessions",
    help="Manage your sessions (list, resume, delete).",
    no_args_is_help=True,
)

_DEFAULT_DAEMON = "http://127.0.0.1:8000"


def _time_ago(ts: float) -> str:
    """Human-readable time ago from a Unix timestamp."""
    now = datetime.now()
    diff = now - datetime.fromtimestamp(ts)
    seconds = int(diff.total_seconds())
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        return f"{seconds // 60}m ago"
    if seconds < 86400:
        return f"{seconds // 3600}h ago"
    return f"{seconds // 86400}d ago"


@sessions_cli.command(name="list")
def list_sessions(
    app_id: Annotated[str, typer.Argument(help="Application ID.")],
    daemon: str = typer.Option(_DEFAULT_DAEMON, "--daemon", "-d"),
    limit: int = typer.Option(20, "--limit", "-n", help="Max sessions to show."),
) -> None:
    """List your sessions for an app."""
    from digitorn_cli.auth import daemon_request

    resp = daemon_request("get", f"{daemon}/api/apps/{app_id}/sessions",
                          daemon=daemon, params={"limit": limit})
    data = resp.json()

    if not data.get("success"):
        console.print(f"[red]{data.get('error', 'Unknown error')}[/red]")
        raise typer.Exit(1)

    items = data.get("data", {}).get("sessions", [])
    if not items:
        console.print(f"\n[dim]No sessions for '{app_id}'.[/dim]\n")
        return

    table = Table(title=f"Sessions - {app_id}", border_style="blue")
    table.add_column("#", style="bold cyan", width=3)
    table.add_column("Session ID", style="dim", max_width=12)
    table.add_column("Title", style="white", max_width=40)
    table.add_column("Msgs", style="green", justify="right", width=5)
    table.add_column("Last Active", style="dim", width=10)

    for i, s in enumerate(items, 1):
        sid = s.get("session_id", "?")[:12]
        title = s.get("title", "")[:40] or "[dim]untitled[/dim]"
        msgs = str(s.get("message_count", 0))
        ts = s.get("last_active", 0)
        ago = _time_ago(ts) if ts else "?"
        table.add_row(str(i), sid, title, msgs, ago)

    console.print()
    console.print(table)
    console.print()
    console.print(f"[dim]Resume: digitorn-cli run {app_id} --session <session_id>[/dim]")


@sessions_cli.command(name="resume")
def resume_session(
    app_id: Annotated[str, typer.Argument(help="Application ID.")],
    session_id: Annotated[str, typer.Argument(help="Session ID to resume.")],
    daemon: str = typer.Option(_DEFAULT_DAEMON, "--daemon", "-d"),
) -> None:
    """Resume an existing session in the TUI."""
    from digitorn_cli.tui import launch_tui
    from digitorn_cli.auth import get_auth_headers

    auth_headers = get_auth_headers(daemon)
    launch_tui(
        daemon_url=daemon,
        app_id=app_id,
        session_id=session_id,
        auth_headers=auth_headers,
    )


@sessions_cli.command(name="delete")
def delete_session(
    app_id: Annotated[str, typer.Argument(help="Application ID.")],
    session_id: Annotated[str, typer.Argument(help="Session ID to delete.")],
    daemon: str = typer.Option(_DEFAULT_DAEMON, "--daemon", "-d"),
) -> None:
    """Delete a session."""
    from digitorn_cli.auth import daemon_request

    resp = daemon_request("delete", f"{daemon}/api/apps/{app_id}/sessions/{session_id}",
                          daemon=daemon)
    if resp.status_code == 200:
        console.print(f"[green]Session '{session_id[:12]}' deleted.[/green]")
    elif resp.status_code == 404:
        console.print(f"[red]Session not found.[/red]")
        raise typer.Exit(1)
    else:
        console.print(f"[red]Error: {resp.text[:200]}[/red]")
        raise typer.Exit(1)


@sessions_cli.command(name="history")
def session_history(
    app_id: Annotated[str, typer.Argument(help="Application ID.")],
    session_id: Annotated[str, typer.Argument(help="Session ID.")],
    daemon: str = typer.Option(_DEFAULT_DAEMON, "--daemon", "-d"),
) -> None:
    """Show conversation history for a session."""
    from digitorn_cli.auth import daemon_request

    resp = daemon_request("get", f"{daemon}/api/apps/{app_id}/sessions/{session_id}/history",
                          daemon=daemon)
    if resp.status_code == 404:
        console.print(f"[red]Session not found.[/red]")
        raise typer.Exit(1)

    data = resp.json().get("data", {})
    messages = data.get("messages", [])

    if not messages:
        console.print("[dim]No messages.[/dim]")
        return

    for msg in messages:
        role = msg.get("role", "?")
        content = msg.get("content", "")
        if role == "user":
            console.print(f"\n[bold green]> {content[:200]}[/bold green]")
        elif role == "assistant":
            console.print(f"\n[#e2e8f0]{content[:500]}[/#e2e8f0]")
        elif role == "system":
            console.print(f"\n[dim][system] {content[:100]}...[/dim]")
