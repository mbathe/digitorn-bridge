"""Local OAuth callback server for standalone CLI mode.

When running `digitorn run --standalone`, there's no daemon HTTP server
to handle OAuth callbacks.  This module provides a lightweight temporary
HTTP server that:

1. Starts on a random local port
2. Serves the OAuth callback endpoint
3. Exchanges the authorization code for tokens
4. Shuts down automatically after the callback is received

Usage::

    token_data = await run_local_oauth_flow(oauth_manager, auth_config, server_id)
"""

from __future__ import annotations

import asyncio
import logging
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from threading import Thread
from typing import Any
from urllib.parse import parse_qs, urlparse

from digitorn.modules.mcp.oauth import OAuthManager, OAuthProviderConfig

logger = logging.getLogger(__name__)

_SUCCESS_HTML = """\
<!DOCTYPE html>
<html>
<head><title>Digitorn - Autorisation réussie</title></head>
<body style="font-family:system-ui;text-align:center;padding:60px">
<h1>&#10004; Autorisation réussie !</h1>
<p>Tu peux fermer cet onglet et revenir au terminal.</p>
</body>
</html>"""

_ERROR_HTML = """\
<!DOCTYPE html>
<html>
<head><title>Digitorn - Erreur</title></head>
<body style="font-family:system-ui;text-align:center;padding:60px">
<h1>&#10008; Erreur d'autorisation</h1>
<p>{error}</p>
</body>
</html>"""


async def run_local_oauth_flow(
    oauth_manager: OAuthManager,
    auth_config: OAuthProviderConfig,
    server_id: str,
    *,
    user_id: str = "cli_user",
    timeout: float = 300.0,
) -> dict[str, Any]:
    """Run a complete OAuth flow with a local callback server.

    Opens the user's browser for authorization and waits for the
    callback. Returns the token response dict on success.

    Args:
        oauth_manager: The OAuthManager instance (holds pending states).
        auth_config: Provider config for this MCP server.
        server_id: MCP server ID.
        user_id: User identifier (default ``"cli_user"`` for standalone).
        timeout: Max seconds to wait for the callback.

    Returns:
        Token response dict (access_token, refresh_token, etc.)

    Raises:
        OAuthLocalError: If the flow fails or times out.
    """
    port = 8913
    if not auth_config.redirect_uri:
        auth_config.redirect_uri = f"http://localhost:{port}/callback"
    else:
        from urllib.parse import urlparse as _urlparse

        parsed_uri = _urlparse(auth_config.redirect_uri)
        port = parsed_uri.port or 8913

    auth_url, state_key = oauth_manager.build_authorize_url(
        auth_config, server_id, user_id,
    )

    loop = asyncio.get_running_loop()
    result_future: asyncio.Future[dict[str, Any]] = loop.create_future()

    class CallbackHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            if parsed.path != "/callback":
                self.send_response(404)
                self.end_headers()
                return

            qs = parse_qs(parsed.query)
            error = qs.get("error", [None])[0]
            if error:
                desc = qs.get("error_description", [error])[0]
                self._send_html(400, _ERROR_HTML.format(error=desc))
                loop.call_soon_threadsafe(
                    result_future.set_exception,
                    OAuthLocalError(f"Authorization denied: {desc}"),
                )
                return

            code = qs.get("code", [None])[0]
            state = qs.get("state", [None])[0]

            if not code or not state:
                self._send_html(400, _ERROR_HTML.format(error="Missing code or state"))
                loop.call_soon_threadsafe(
                    result_future.set_exception,
                    OAuthLocalError("Missing code or state in callback"),
                )
                return

            oauth_state = oauth_manager.get_pending_state(state)
            if oauth_state is None:
                self._send_html(400, _ERROR_HTML.format(error="Invalid or expired state"))
                loop.call_soon_threadsafe(
                    result_future.set_exception,
                    OAuthLocalError("Invalid or expired OAuth state"),
                )
                return

            async def _exchange():
                return await oauth_manager.exchange_code(auth_config, oauth_state, code)

            try:
                future = asyncio.run_coroutine_threadsafe(_exchange(), loop)
                token_data = future.result(timeout=30)
                self._send_html(200, _SUCCESS_HTML)
                loop.call_soon_threadsafe(result_future.set_result, token_data)
            except Exception as exc:
                self._send_html(500, _ERROR_HTML.format(error=str(exc)))
                loop.call_soon_threadsafe(
                    result_future.set_exception,
                    OAuthLocalError(f"Token exchange failed: {exc}"),
                )

        def _send_html(self, status: int, html: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(html.encode("utf-8"))

        def log_message(self, format: str, *args: Any) -> None:
            pass

    server = HTTPServer(("127.0.0.1", port), CallbackHandler)
    server_thread = Thread(target=server.serve_forever, daemon=True)
    server_thread.start()

    logger.info("local_oauth: listening on http://localhost:%d/callback", port)

    webbrowser.open(auth_url)

    try:
        token_data = await asyncio.wait_for(result_future, timeout=timeout)
        return token_data
    except asyncio.TimeoutError:
        raise OAuthLocalError(
            f"OAuth flow timed out after {timeout}s - "
            "the user didn't complete authorization in time."
        )
    finally:
        server.shutdown()
        server_thread.join(timeout=2.0)


class OAuthLocalError(Exception):
    """Raised when the local OAuth flow fails."""
