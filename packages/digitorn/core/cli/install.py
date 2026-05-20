"""`digitorn install-local` - pair this daemon to a central account."""

from __future__ import annotations

import http.server
import socket
import socketserver
import threading
import time
import urllib.parse
import webbrowser
from getpass import getpass
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console

from digitorn.core.auth.local_device import (
    DEFAULT_SECRETS_PATH,
    LocalDeviceAuth,
)

console = Console()

install_cli = typer.Typer(
    name="install-local",
    help="Pair this daemon to a central Digitorn account (one-time setup).",
    invoke_without_command=True,
    no_args_is_help=False,
)


@install_cli.callback()
def install_local(
    auth_url: Annotated[
        str,
        typer.Option("--auth", "-a", help="Central auth service URL."),
    ] = "http://127.0.0.1:8001",
    label: Annotated[
        str | None,
        typer.Option("--label", "-l", help="Device label (defaults to hostname)."),
    ] = None,
    oauth: Annotated[
        str | None,
        typer.Option(
            "--oauth", help="OAuth provider to use (google|microsoft|azure). Skip for password login.",
        ),
    ] = None,
    username: Annotated[
        str | None,
        typer.Option("--username", "-u", help="Username for password login (prompted if absent)."),
    ] = None,
    secrets_path: Annotated[
        Path,
        typer.Option("--secrets", help="Where to store the encrypted secrets."),
    ] = DEFAULT_SECRETS_PATH,
    timeout: Annotated[
        int,
        typer.Option("--timeout", help="Max seconds to wait for the browser callback."),
    ] = 180,
) -> None:
    """Pair this daemon to a central Digitorn account."""
    label = label or socket.gethostname()

    if secrets_path.exists():
        if not typer.confirm(
            f"This daemon is already paired (secrets at {secrets_path}). Re-pair?",
            default=False,
        ):
            console.print("[yellow]Cancelled[/yellow]")
            raise typer.Exit(0)

    if oauth:
        access_token = _login_via_oauth(auth_url, oauth, timeout)
    else:
        access_token = _login_via_password(auth_url, username)

    # Call the central pair endpoint with the fresh access_token.
    import httpx
    try:
        resp = httpx.post(
            f"{auth_url}/auth/devices/pair",
            json={"label": label},
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=30.0,
        )
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        console.print(f"[red]Pair request failed:[/red] {exc}")
        raise typer.Exit(1) from exc

    data = resp.json()

    central_jwks: dict | None = None
    try:
        jr = httpx.get(f"{auth_url}/.well-known/jwks.json", timeout=10.0)
        if jr.status_code == 200:
            central_jwks = jr.json()
    except httpx.HTTPError as exc:
        console.print(
            f"[yellow]Warning:[/yellow] could not fetch JWKS: {exc}. "
            f"The daemon will fall back to legacy verification if available."
        )

    # Persist locally.
    LocalDeviceAuth.write(
        secrets_path=secrets_path,
        device_id=data["device_id"],
        device_token=data["device_token"],
        central_iss=data["central_iss"],
        auth_url=auth_url,
        central_jwks=central_jwks,
    )

    console.print(f"[green]✓[/green] Paired as [cyan]{label}[/cyan]")
    console.print(f"  Device ID: {data['device_id']}")
    console.print(f"  Stored at: {secrets_path}")
    console.print(
        f"  Token expires: in ~{int((data['expires_at'] - int(time.time())) // 86400)} days"
    )
    console.print()
    console.print(
        "[dim]The daemon will now authenticate you offline. Connect to "
        "the internet at least once every ~60 days so the token can be "
        "rolling-refreshed.[/dim]"
    )


def _login_via_password(auth_url: str, username: str | None) -> str:
    """Interactive username + password login, no browser involved."""
    import httpx
    if not username:
        username = typer.prompt("Username (or email)")
    password = getpass("Password: ")

    try:
        resp = httpx.post(
            f"{auth_url}/auth/login",
            json={"username": username, "password": password},
            timeout=30.0,
        )
    except httpx.HTTPError as exc:
        console.print(f"[red]Cannot reach auth service:[/red] {exc}")
        raise typer.Exit(1) from exc

    if resp.status_code != 200:
        try:
            err = resp.json().get("detail", resp.text)
        except Exception:
            err = resp.text
        console.print(f"[red]Login failed:[/red] {err}")
        raise typer.Exit(1)
    return resp.json()["access_token"]


def _login_via_oauth(auth_url: str, provider: str, timeout: int) -> str:
    """OAuth browser-bounce login."""
    callback_port = _find_free_port()
    callback_url = f"http://127.0.0.1:{callback_port}/oauth-callback"
    bounce_param = urllib.parse.quote(callback_url, safe="")

    state: dict[str, str] = {}
    listener = _start_callback_server(callback_port, state)

    try:
        login_url = f"{auth_url}/auth/oauth/{provider}?bounce_to={bounce_param}"
        console.print(f"[cyan]Opening browser for OAuth ({provider}):[/cyan] {login_url}")
        console.print(f"  Listening on {callback_url} (timeout {timeout}s)")
        webbrowser.open(login_url)

        if not _wait_for(state, "access_token", timeout=timeout):
            err = state.get("oauth_error") or "timeout"
            console.print(f"[red]OAuth failed:[/red] {err}")
            raise typer.Exit(1)
    finally:
        listener.shutdown()
        listener.server_close()

    return state["access_token"]


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _start_callback_server(
    port: int,
    state: dict[str, str],
) -> socketserver.TCPServer:
    """Tiny HTTP listener that captures the OAuth bounce query string."""

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 (stdlib API)
            parsed = urllib.parse.urlparse(self.path)
            qs = urllib.parse.parse_qs(parsed.query)
            for k in ("access_token", "refresh_token", "expires_in", "provider", "oauth_error"):
                if k in qs:
                    state[k] = qs[k][0]

            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            if "oauth_error" in qs:
                self.wfile.write(_html_error(qs["oauth_error"][0]).encode("utf-8"))
            elif "access_token" in qs:
                self.wfile.write(_html_success().encode("utf-8"))
            else:
                self.wfile.write(_html_unexpected().encode("utf-8"))

        def log_message(self, *args, **kwargs):  # silence default access log
            return

    httpd = socketserver.TCPServer(("127.0.0.1", port), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    return httpd


def _wait_for(state: dict[str, str], key: str, timeout: int) -> bool:
    """Poll `state` until `key` arrives or timeout elapses."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if key in state or "oauth_error" in state:
            return key in state
        time.sleep(0.2)
    return False


_HTML_BASE = """\
<!doctype html>
<html><head><meta charset="utf-8"><title>Digitorn pairing</title>
<style>
  body{{font:14px system-ui,sans-serif;max-width:480px;margin:80px auto;padding:24px;
       background:#0f1115;color:#e6e6e6}}
  h1{{font-size:18px;margin:0 0 12px}}
  .ok{{color:#4ade80}}.err{{color:#f87171}}
  code{{background:#1f2937;padding:2px 6px;border-radius:4px}}
</style></head>
<body>{body}</body></html>"""


def _html_success() -> str:
    return _HTML_BASE.format(body=(
        "<h1 class='ok'>✓ Daemon paired</h1>"
        "<p>You can close this tab and go back to your terminal.</p>"
    ))


def _html_error(err: str) -> str:
    return _HTML_BASE.format(body=(
        f"<h1 class='err'>Pairing failed</h1>"
        f"<p>The auth service reported: <code>{err}</code></p>"
        f"<p>Close this tab, fix the issue and run <code>digitorn install-local</code> again.</p>"
    ))


def _html_unexpected() -> str:
    return _HTML_BASE.format(body=(
        "<h1>Unexpected callback</h1>"
        "<p>No token was found in the URL. Try again.</p>"
    ))
