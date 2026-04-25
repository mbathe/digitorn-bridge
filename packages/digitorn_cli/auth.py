"""CLI authentication helpers — token storage, auto-refresh, authenticated HTTP.

Stores credentials in ``~/.digitorn/credentials.json`` with:
- access_token, refresh_token, expires_at, daemon_url, user info

All CLI commands that call the daemon should use :func:`daemon_request` instead
of raw ``httpx`` calls. It handles:
1. Loading stored tokens
2. Adding the Authorization header
3. Auto-refreshing expired tokens (via /auth/refresh)
4. Prompting login when no credentials exist
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

import httpx
import typer
from rich.console import Console

console = Console()

_CREDENTIALS_PATH = Path.home() / ".digitorn" / "credentials.json"


def _load_credentials() -> dict[str, Any] | None:
    """Load stored credentials, or None if absent/corrupt."""
    if not _CREDENTIALS_PATH.exists():
        return None
    try:
        data = json.loads(_CREDENTIALS_PATH.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or "access_token" not in data:
            return None
        return data
    except Exception:
        return None


def _save_credentials(data: dict[str, Any]) -> None:
    """Save credentials to disk with restrictive permissions from the start."""
    _CREDENTIALS_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(data, indent=2).encode("utf-8")
    fd = os.open(str(_CREDENTIALS_PATH), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, payload)
    finally:
        os.close(fd)


def clear_credentials() -> None:
    """Delete stored credentials."""
    if _CREDENTIALS_PATH.exists():
        _CREDENTIALS_PATH.unlink()


def _is_token_expired(creds: dict[str, Any]) -> bool:
    """Check if the access token is expired (with 30s margin)."""
    expires_at = creds.get("expires_at", 0)
    return time.time() > (expires_at - 30)


def _refresh_token(daemon: str, creds: dict[str, Any]) -> dict[str, Any] | None:
    """Try to refresh the access token. Returns updated creds or None."""
    refresh = creds.get("refresh_token")
    if not refresh:
        return None
    try:
        resp = httpx.post(
            f"{daemon}/auth/refresh",
            json={"refresh_token": refresh},
            timeout=10.0,
        )
        if resp.status_code != 200:
            return None
        data = resp.json()
        creds["access_token"] = data["access_token"]
        if data.get("refresh_token"):
            creds["refresh_token"] = data["refresh_token"]
        creds["expires_at"] = time.time() + data.get("expires_in", 900)
        _save_credentials(creds)
        return creds
    except Exception:
        return None


def _prompt_login(daemon: str) -> dict[str, Any]:
    """Prompt user for credentials and login. Returns creds dict or exits."""
    # Check daemon is reachable BEFORE asking for credentials
    try:
        _check = httpx.get(f"{daemon}/health", timeout=5.0)
    except (httpx.ConnectError, httpx.ReadTimeout, httpx.TimeoutException, OSError):
        console.print(f"\n[bold red]Cannot connect to daemon at {daemon}[/bold red]")
        console.print("Start the daemon first: [cyan]digitorn start[/cyan]\n")
        raise typer.Exit(1)

    from rich.prompt import Prompt

    console.print("\n[bold]Authentication required.[/bold]")
    console.print(f"[dim]Daemon: {daemon}[/dim]\n")

    identity = Prompt.ask("  Username or email")
    password = Prompt.ask("  Password", password=True)

    if not identity or not password:
        console.print("[red]  Cancelled.[/red]")
        raise typer.Exit(1)

    # Send as email if it looks like one, otherwise as username
    if "@" in identity:
        payload = {"email": identity, "password": password}
    else:
        payload = {"username": identity, "password": password}

    try:
        resp = httpx.post(
            f"{daemon}/auth/login",
            json=payload,
            timeout=30.0,
        )
    except (httpx.ConnectError, httpx.ReadTimeout, httpx.TimeoutException, OSError):
        console.print(f"\n[bold red]Cannot connect to daemon at {daemon}[/bold red]")
        console.print("Start the daemon first: [cyan]digitorn start[/cyan]")
        raise typer.Exit(1)

    if resp.status_code != 200:
        try:
            error = resp.json().get("error", "Login failed")
        except Exception:
            error = f"HTTP {resp.status_code}"
        console.print(f"\n[bold red]Login failed:[/bold red] {error}")

        # Offer to register
        from rich.prompt import Confirm

        if Confirm.ask("\n  Register a new account?", default=True):
            return _prompt_register(daemon, identity, password)

        raise typer.Exit(1)

    data = resp.json()
    creds = {
        "access_token": data["access_token"],
        "refresh_token": data.get("refresh_token"),
        "expires_at": time.time() + data.get("expires_in", 900),
        "daemon_url": daemon,
        "user_id": data.get("user_id"),
        "display_name": data.get("display_name"),
        "username": identity,
    }
    _save_credentials(creds)
    console.print(f"[green]  Logged in as {data.get('display_name') or identity}.[/green]\n")
    return creds


def _prompt_register(daemon: str, username: str, password: str) -> dict[str, Any]:
    """Register a new account and return creds."""
    from rich.prompt import Prompt

    email = Prompt.ask("  Email (optional)", default="")

    try:
        resp = httpx.post(
            f"{daemon}/auth/register",
            json={
                "username": username,
                "password": password,
                "email": email or None,
            },
            timeout=10.0,
        )
    except httpx.ConnectError:
        console.print(f"[bold red]Cannot connect to daemon at {daemon}[/bold red]")
        raise typer.Exit(1)

    if resp.status_code != 200:
        error = resp.json().get("error", "Registration failed")
        console.print(f"\n[bold red]Registration failed:[/bold red] {error}")
        raise typer.Exit(1)

    data = resp.json()
    creds = {
        "access_token": data["access_token"],
        "refresh_token": data.get("refresh_token"),
        "expires_at": time.time() + data.get("expires_in", 900),
        "daemon_url": daemon,
        "user_id": data.get("user_id"),
        "display_name": data.get("display_name"),
        "username": username,
    }
    _save_credentials(creds)
    console.print(f"[green]  Registered and logged in as {username}.[/green]\n")
    return creds


def get_auth_headers(daemon: str) -> dict[str, str]:
    """Get Authorization headers for daemon requests.

    Loads stored credentials, refreshes if expired, prompts login if needed.
    """
    creds = _load_credentials()

    if creds is None:
        creds = _prompt_login(daemon)

    if _is_token_expired(creds):
        refreshed = _refresh_token(daemon, creds)
        if refreshed is None:
            # Refresh failed — need to re-login
            clear_credentials()
            creds = _prompt_login(daemon)
        else:
            creds = refreshed

    return {"Authorization": f"Bearer {creds['access_token']}"}


def daemon_request(
    method: str,
    url: str,
    daemon: str = "http://127.0.0.1:8000",
    **kwargs: Any,
) -> httpx.Response:
    """Make an authenticated HTTP request to the daemon.

    Handles auth headers, token refresh, and 401 retry.
    """
    headers = get_auth_headers(daemon)
    # Merge with any existing headers
    if "headers" in kwargs:
        kwargs["headers"].update(headers)
    else:
        kwargs["headers"] = headers

    if "timeout" not in kwargs:
        kwargs["timeout"] = 10.0

    try:
        resp = getattr(httpx, method)(url, **kwargs)
    except (httpx.ConnectError, httpx.ReadTimeout, httpx.TimeoutException, OSError):
        console.print(f"\n[bold red]Cannot connect to daemon at {daemon}[/bold red]")
        console.print("Start the daemon first: [cyan]digitorn start[/cyan]")
        raise typer.Exit(1)

    # Token expired mid-session — try refresh and retry once
    if resp.status_code == 401:
        creds = _load_credentials()
        if creds:
            refreshed = _refresh_token(daemon, creds)
            if refreshed:
                kwargs["headers"]["Authorization"] = f"Bearer {refreshed['access_token']}"
                resp = getattr(httpx, method)(url, **kwargs)

        if resp.status_code == 401:
            clear_credentials()
            console.print("[yellow]Session expired. Please log in again.[/yellow]")
            new_headers = get_auth_headers(daemon)
            kwargs["headers"]["Authorization"] = new_headers["Authorization"]
            resp = getattr(httpx, method)(url, **kwargs)

    return resp


# Public alias
prompt_login = _prompt_login
