"""`digitorn auth` - manage CLI authentication via browser-OAuth."""

from __future__ import annotations

import base64
import json
import os
import socket
import time
import urllib.parse
import webbrowser
from pathlib import Path
from typing import Annotated, Any

import typer
from rich.console import Console
from rich.table import Table

from digitorn.core.cli.install import (
    _start_callback_server,
    _wait_for,
)

console = Console()

DEFAULT_AUTH_URL = "https://auth.digitorn.ai"

_CREDENTIALS_PATH = Path.home() / ".digitorn" / "credentials.json"


auth_cli = typer.Typer(
    name="auth",
    help="Authenticate the CLI against the Digitorn central auth service.",
    no_args_is_help=True,
)


@auth_cli.command("login")
def login_cmd(
    provider: Annotated[
        str,
        typer.Option(
            "--provider", "-p",
            help="OAuth provider to use (google | microsoft | azure).",
        ),
    ] = "google",
    auth_url: Annotated[
        str,
        typer.Option(
            "--auth", "-a",
            help="Central auth service URL. Defaults to the public service.",
        ),
    ] = DEFAULT_AUTH_URL,
    timeout: Annotated[
        int,
        typer.Option(
            "--timeout",
            help="Max seconds to wait for the browser callback.",
        ),
    ] = 180,
    force: Annotated[
        bool,
        typer.Option(
            "--force", "-f",
            help="Re-login even if credentials already exist.",
        ),
    ] = False,
) -> None:
    """Open the browser, authenticate against the central auth service.

    The auth service bounces the access token back to a localhost HTTP
    listener; we capture it, store it in `~/.digitorn/credentials.json`,
    and every subsequent CLI command authenticates with that token.

    Defaults to Google OAuth; use `--provider microsoft` or
    `--provider azure` for the other supported flows.
    """
    if _CREDENTIALS_PATH.exists() and not force:
        existing = _load_credentials()
        if existing is not None:
            who = existing.get("email") or existing.get("user_id") or "?"
            console.print(f"[yellow]Already logged in as[/yellow] [cyan]{who}[/cyan]")
            console.print(
                "Run [bold]digitorn auth logout[/bold] first, or pass "
                "[bold]--force[/bold] to overwrite.",
            )
            raise typer.Exit(0)

    if provider not in ("google", "microsoft", "azure"):
        console.print(
            f"[red]Unknown provider:[/red] {provider}. "
            "Valid: google | microsoft | azure.",
        )
        raise typer.Exit(2)

    state = _browser_oauth(auth_url, provider, timeout)

    creds = _build_credentials(auth_url, state)
    _save_credentials(creds)

    who = creds.get("email") or creds.get("user_id") or "(unknown user)"
    console.print(f"\n[green]✓[/green] Logged in as [cyan]{who}[/cyan]")
    console.print(f"  Credentials saved to: [dim]{_CREDENTIALS_PATH}[/dim]")
    if creds.get("expires_at"):
        remaining = max(0, int(creds["expires_at"] - time.time()))
        console.print(f"  Access token expires in: ~{remaining // 60} min (auto-refreshes)")


@auth_cli.command("logout")
def logout_cmd() -> None:
    """Clear the locally stored CLI credentials."""
    if not _CREDENTIALS_PATH.exists():
        console.print("[yellow]No credentials to clear.[/yellow]")
        raise typer.Exit(0)
    _CREDENTIALS_PATH.unlink()
    console.print(f"[green]✓[/green] Credentials cleared ([dim]{_CREDENTIALS_PATH}[/dim])")


@auth_cli.command("whoami")
def whoami_cmd() -> None:
    """Show the currently logged-in user."""
    creds = _load_credentials()
    if creds is None:
        console.print("[yellow]Not logged in.[/yellow] Run [bold]digitorn auth login[/bold] to start.")
        raise typer.Exit(0)

    table = Table(show_header=False, box=None, pad_edge=False)
    table.add_column(style="dim")
    table.add_column(style="cyan")

    if creds.get("email"):
        table.add_row("Email", creds["email"])
    if creds.get("name"):
        table.add_row("Name", creds["name"])
    if creds.get("user_id"):
        table.add_row("User ID", creds["user_id"])
    if creds.get("provider"):
        table.add_row("Provider", creds["provider"])
    if creds.get("auth_url"):
        table.add_row("Auth service", creds["auth_url"])
    if creds.get("expires_at"):
        remaining = int(creds["expires_at"] - time.time())
        if remaining > 0:
            table.add_row("Token expires", f"in {remaining // 60} min")
        else:
            table.add_row("Token", "[red]expired[/red] (will auto-refresh on next call)")

    console.print(table)


def _browser_oauth(auth_url: str, provider: str, timeout: int) -> dict[str, str]:
    """Open the browser, capture the full OAuth callback state."""
    callback_port = _find_free_port()
    callback_url = f"http://127.0.0.1:{callback_port}/oauth-callback"
    bounce_param = urllib.parse.quote(callback_url, safe="")

    state: dict[str, str] = {}
    listener = _start_callback_server(callback_port, state)

    try:
        login_url = (
            f"{auth_url.rstrip('/')}/auth/oauth/{provider}"
            f"?bounce_to={bounce_param}"
        )
        console.print(f"\n[cyan]Opening browser for OAuth ({provider})[/cyan]")
        console.print(f"  If the browser does not open, visit:\n  [dim]{login_url}[/dim]")
        console.print(f"  Waiting for callback on {callback_url} (timeout {timeout}s)...\n")
        webbrowser.open(login_url)

        if not _wait_for(state, "access_token", timeout=timeout):
            err = state.get("oauth_error") or "timeout"
            console.print(f"[red]OAuth flow failed:[/red] {err}")
            raise typer.Exit(1)
    finally:
        listener.shutdown()
        listener.server_close()

    return state


def _build_credentials(auth_url: str, oauth_state: dict[str, str]) -> dict[str, Any]:
    """Assemble the credentials dict from the OAuth callback state."""
    access_token = oauth_state["access_token"]
    creds: dict[str, Any] = {
        "access_token": access_token,
        "auth_url": auth_url,
    }
    refresh = oauth_state.get("refresh_token")
    if refresh:
        creds["refresh_token"] = refresh
    expires_in = oauth_state.get("expires_in")
    if expires_in:
        try:
            creds["expires_at"] = time.time() + int(expires_in)
        except (TypeError, ValueError):
            pass
    provider = oauth_state.get("provider")
    if provider:
        creds["provider"] = provider

    try:
        parts = access_token.split(".")
        if len(parts) >= 2:
            padded = parts[1] + "=" * (-len(parts[1]) % 4)
            payload = json.loads(base64.urlsafe_b64decode(padded).decode("utf-8"))
            for src, dst in (("sub", "user_id"), ("email", "email"), ("name", "name")):
                if payload.get(src):
                    creds[dst] = payload[src]
    except Exception:
        pass  # token may be opaque; fall back to anonymous display

    return creds


def _save_credentials(data: dict[str, Any]) -> None:
    """Persist credentials with restrictive permissions from creation."""
    _CREDENTIALS_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(data, indent=2).encode("utf-8")
    fd = os.open(
        str(_CREDENTIALS_PATH),
        os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
        0o600,
    )
    try:
        os.write(fd, payload)
    finally:
        os.close(fd)


def _load_credentials() -> dict[str, Any] | None:
    if not _CREDENTIALS_PATH.exists():
        return None
    try:
        data = json.loads(_CREDENTIALS_PATH.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or "access_token" not in data:
            return None
        return data
    except Exception:
        return None


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
