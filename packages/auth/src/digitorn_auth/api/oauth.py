"""OAuth2 authorization-code flow for Google / Microsoft.

Two endpoints implement the standard 3-leg flow:

  GET /auth/oauth/{provider}            -> 302 to provider consent screen
  GET /auth/oauth/{provider}/callback   -> exchange code, mint JWT, bounce

The browser flow drops the user back on the web app with the access
token in the URL fragment (#access_token=…). Desktop clients can pass
``?bounce_to=http://127.0.0.1:<port>/...`` so the localhost callback
gets the token in the query string instead (URL fragments aren't
accessible to a stdlib ``http.server`` listener).

1:1 with the daemon's ``digitorn.core.api.auth`` OAuth surface — only
the resolution helpers (settings, web_origin) point at the standalone
config here.
"""

from __future__ import annotations

import logging
from urllib.parse import urlencode, urlparse

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse

from digitorn_auth.config import get_settings

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth/oauth", tags=["oauth"])


_PROVIDER_IDS = {"google", "azure", "microsoft"}
# Bounce-target overrides keyed by OAuth state nonce.
# Populated by ``oauth_start`` when a desktop client passes ``bounce_to``,
# popped one-shot by ``oauth_callback``. Hard-cap to prevent memory abuse.
_BOUNCE_OVERRIDES: dict[str, str] = {}
_BOUNCE_OVERRIDES_CAP = 1000


def _is_safe_localhost(url: str) -> bool:
    """Allow only ``http://127.0.0.1:<port>/*`` or ``http://localhost:<port>/*``.

    Prevents the OAuth start endpoint from being abused as an open
    redirect to arbitrary external URLs.
    """
    try:
        u = urlparse(url)
    except Exception:
        return False
    if u.scheme != "http":
        return False
    if u.hostname not in ("127.0.0.1", "localhost", "::1"):
        return False
    return True


def _web_origin_for_callback(request: Request) -> str:
    """Resolve the front-end origin to bounce back to after OAuth."""
    settings = get_settings()
    base = settings.oauth_redirect_base or ""
    if base:
        return base.rstrip("/")
    referer = request.headers.get("referer") or request.headers.get("origin")
    if referer:
        return referer.split("?", 1)[0].rstrip("/")
    return "http://localhost:3000"


def _normalise_provider(provider: str) -> str:
    """Map url-friendly aliases to the registered provider id."""
    if provider == "microsoft":
        return "azure"
    return provider


@router.get("/{provider}")
async def oauth_start(provider: str, request: Request):
    """Step 1: redirect the browser to the OAuth provider consent screen."""
    pid = _normalise_provider(provider)
    if pid not in _PROVIDER_IDS:
        raise HTTPException(status_code=404, detail=f"Unknown OAuth provider '{provider}'")

    auth_service = getattr(request.app.state, "auth_service", None)
    if auth_service is None:
        raise HTTPException(status_code=503, detail="Auth service not initialised")

    prov = auth_service.get_provider(pid)
    if prov is None:
        raise HTTPException(
            status_code=503,
            detail=(
                f"OAuth provider '{pid}' is not configured. "
                f"Set DIGITORN_AUTH_OAUTH_{pid.upper()}_CLIENT_ID and CLIENT_SECRET."
            ),
        )

    if not hasattr(prov, "get_authorize_url"):
        raise HTTPException(status_code=503, detail="Provider does not support OAuth flow")

    url, state = prov.get_authorize_url()

    bounce_to = request.query_params.get("bounce_to", "").strip()
    if bounce_to and _is_safe_localhost(bounce_to):
        if len(_BOUNCE_OVERRIDES) > _BOUNCE_OVERRIDES_CAP:
            _BOUNCE_OVERRIDES.clear()
        _BOUNCE_OVERRIDES[state] = bounce_to

    return RedirectResponse(url, status_code=302)


@router.get("/{provider}/callback")
async def oauth_callback(provider: str, request: Request):
    """Step 2: provider redirected back. Exchange code, mint JWT, bounce."""
    pid = _normalise_provider(provider)
    if pid not in _PROVIDER_IDS:
        raise HTTPException(status_code=404, detail=f"Unknown OAuth provider '{provider}'")

    auth_service = getattr(request.app.state, "auth_service", None)
    if auth_service is None:
        raise HTTPException(status_code=503, detail="Auth service not initialised")

    web = _web_origin_for_callback(request)
    state = request.query_params.get("state", "")
    desktop_bounce = _BOUNCE_OVERRIDES.pop(state, None)

    error = request.query_params.get("error")
    if error:
        params = urlencode({"oauth_error": error})
        target = desktop_bounce or f"{web}/login"
        return RedirectResponse(f"{target}?{params}", status_code=302)

    code = request.query_params.get("code", "")
    if not code:
        params = urlencode({"oauth_error": "missing_code"})
        target = desktop_bounce or f"{web}/login"
        return RedirectResponse(f"{target}?{params}", status_code=302)

    result = await auth_service.login(
        {"code": code, "state": state},
        provider=pid,
    )
    if not result.success or not result.access_token:
        params = urlencode({"oauth_error": result.error or "auth_failed"})
        target = desktop_bounce or f"{web}/login"
        return RedirectResponse(f"{target}?{params}", status_code=302)

    bounce_params = {
        "access_token": result.access_token,
        "refresh_token": result.refresh_token or "",
        "expires_in": str(result.expires_in or 0),
        "provider": pid,
    }

    if desktop_bounce:
        # Localhost callers (Flutter desktop, CLI install-local) need the
        # token in the query string because stdlib http.server cannot
        # read URL fragments.
        return RedirectResponse(
            f"{desktop_bounce}?{urlencode(bounce_params)}",
            status_code=302,
        )

    # Browser path: stash the token in the URL fragment (#) so it never
    # ends up in server logs or Referer headers when the SPA redirects.
    return RedirectResponse(
        f"{web}/auth/oauth-return#{urlencode(bounce_params)}",
        status_code=302,
    )
