"""``digitorn auth`` — manage CLI authentication via browser-OAuth.

Same UX as ``gh auth login``, ``gcloud auth login``, ``vercel login``:
the CLI opens your default browser, you authenticate with the Digitorn
central auth service (``auth.digitorn.ai`` or whatever ``--auth`` points
to), the auth service redirects back to a localhost HTTP listener that
captures the token, and the listener closes. No password prompt, no
copy-paste.

Three commands:

  - ``digitorn auth login`` — start the browser flow, save credentials
  - ``digitorn auth logout`` — clear local credentials
  - ``digitorn auth whoami`` — display the current logged-in user

The CLI credentials end up in ``~/.digitorn/credentials.json`` (same
file ``daemon_request`` from ``auth_helpers.py`` reads), so once you
``auth login`` every other CLI command (``dev``, ``hub``, ``credentials``,
…) authenticates transparently.

The OAuth browser-bounce primitives are shared with ``install-local``
(see ``install.py``); this module just wraps them with a more ergonomic
top-level command + adds the whoami/logout management commands.
"""

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

# Default central auth URL. Overrideable via ``--auth``. The production
# default points at the public auth service; local dev users pass
# ``--auth http://127.0.0.1:8001`` (or whatever their local auth
# instance listens on).
DEFAULT_AUTH_URL = "https://auth.digitorn.ai"

# Same file ``auth_helpers.py`` uses to read tokens. Keeping the path in
# sync means a successful ``auth login`` immediately authenticates every
# other CLI command.
_CREDENTIALS_PATH = Path.home() / ".digitorn" / "credentials.json"


auth_cli = typer.Typer(
    name="auth",
    help="Authenticate the CLI against the Digitorn central auth service.",
    no_args_is_help=True,
)


# ── Commands ───────────────────────────────────────────────────────


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
    listener; we capture it, store it in ``~/.digitorn/credentials.json``,
    and every subsequent CLI command authenticates with that token.

    Defaults to Google OAuth; use ``--provider microsoft`` or
    ``--provider azure`` for the other supported flows.
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
    """Clear the locally stored CLI credentials.

    The token on the auth service is NOT revoked — only the local copy
    is removed. To fully revoke, also visit the auth service's session
    management UI.
    """
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


# ── OAuth browser flow ─────────────────────────────────────────────


def _browser_oauth(auth_url: str, provider: str, timeout: int) -> dict[str, str]:
    """Open the browser, capture the full OAuth callback state.

    Returns the dict captured from the callback URL (access_token,
    refresh_token, expires_in, provider, ...). The shared helpers
    from ``install.py`` handle the listener + HTML pages. We just
    wrap them and surface the full state instead of only the access
    token (which is what ``_login_via_oauth`` returns).
    """
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


# ── Credentials persistence ────────────────────────────────────────


def _build_credentials(auth_url: str, oauth_state: dict[str, str]) -> dict[str, Any]:
    """Assemble the credentials dict from the OAuth callback state.

    Decodes the JWT payload (no signature check) to pull user_id +
    email + name for display purposes. The signature verification
    happens server-side on every request; the client only needs the
    raw token bytes.
    """
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

    # Pull user identity out of the JWT for whoami + UX. JWT format is
    # ``header.payload.signature``; payload is base64url-encoded JSON
    # without padding. No signature check here -- server-side verifies
    # on every request.
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
    """Persist credentials with restrictive permissions from creation.

    Uses ``os.open(O_CREAT, mode=0o600)`` so the file is never world-
    readable, even momentarily. Mirrors the pattern in
    ``auth_helpers._save_credentials``.
    """
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
