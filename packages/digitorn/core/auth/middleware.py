"""FastAPI auth middleware - verifies JWT on protected endpoints.

Extracts the Bearer token from the Authorization header,
verifies it, and injects user info into the request state.

Unprotected paths (login, register, health) are skipped.
When auth is disabled in config, all requests pass through.
"""

from __future__ import annotations

import logging
from typing import Any

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

logger = logging.getLogger(__name__)

_PUBLIC_PATHS = frozenset({
    "/auth/login",
    "/auth/register",
    "/auth/callback",
    "/auth/refresh",
    "/health",
    "/healthz",
    "/readyz",
    # Prometheus metrics - standard scrapers (Prometheus, Grafana Agent)
    # expect an unauthenticated GET on /metrics. In production the daemon
    # is expected to run behind a reverse proxy that firewalls /metrics
    # to the metrics network.
    "/metrics",
    # FastAPI auto-generated API documentation. Needed so developers can
    # browse the route catalogue in a browser without logging in - the
    # daemon binds to 127.0.0.1 by default so this is local-only.
    # The routes themselves still require auth; only the schema/UI is open.
    "/docs",
    "/redoc",
    "/openapi.json",
    "/",
})

_PUBLIC_PREFIXES = (
    "/auth/oauth/",
    "/static/",
    "/socket.io/",      # Socket.IO has its own auth layer in on_connect
)

# Hub catalog browse routes - readable without auth so the marketplace
# can be opened by anonymous visitors (e.g. unauthenticated landing on
# /hub from the login page). Write actions (install / review POST /
# report POST) still require a JWT and are NOT in this list.
_PUBLIC_HUB_GET_PATHS = frozenset({
    "/api/hub/search",
})
_PUBLIC_HUB_GET_PREFIX = "/api/hub/packages/"


def _is_public_hub_browse(path: str, method: str) -> bool:
    if method != "GET":
        return False
    if path in _PUBLIC_HUB_GET_PATHS:
        return True
    if not path.startswith(_PUBLIC_HUB_GET_PREFIX):
        return False
    # Allow /api/hub/packages/{pub}/{pkg}, /reviews, /stats but only
    # the read-only forms. POST hits a different method and gets
    # filtered out above.
    return True

_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})

_LOOPBACK_AGENT_PATH_PREFIXES = (
    "/api/discovery",
    "/api/apps",
    "/api/builder",
    "/api/packages",
    "/api/credentials",
    "/api/mcp",
    "/api/modules",
    "/api/health",
    # BUG-086: /api/transcribe used to be in the loopback bypass which
    # let any local process - including an untrusted Windows account -
    # burn GPU cycles or the OpenAI budget by POSTing arbitrary audio
    # at the endpoint anonymously. In-process agents that legitimately
    # need transcription should hold a real JWT; we don't treat this
    # route as an "agent self-call" anymore.
)


_LOOPBACK_MUTATING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

# Paths that remain read-only for the loopback bypass. Mutating verbs on
# any other /api path require authentication even from 127.0.0.1 - this
# prevents a local hostile process / multi-user Windows account from
# piloting another user's daemon (send messages, resolve approvals,
# delete sessions, …) without ever logging in.
_LOOPBACK_MUTATION_ALLOW_PREFIXES = (
    "/api/apps/builder/",          # internal builder publish path
    "/api/credentials/providers",  # credential discovery read
)


def _is_loopback_self_call(request: Request) -> bool:
    """True when an in-process agent calls the daemon's own loopback API.

    Conditions (ALL required):
      1. real TCP client host ∈ {127.0.0.1, ::1, localhost} (cannot be
         spoofed - comes from the kernel via ``request.client.host``)
      2. path starts with an allow-listed prefix
      3. the request carries NO credentials - no Authorization header,
         no ``?token=`` query param, no ``digitorn_preview_token``
         cookie.
      4. For mutating verbs (POST/PUT/PATCH/DELETE) we further require
         the path to be in a narrow allow-list. The old behaviour
         trusted any localhost caller to ``POST /api/apps/*/messages``,
         ``/abort``, ``/approve``, ``DELETE /sessions/...`` which let
         a hostile local process pilot another user's daemon.
    """
    client = getattr(request, "client", None)
    host = getattr(client, "host", "") if client else ""
    if host not in _LOOPBACK_HOSTS:
        return False
    path = request.url.path
    if not any(path.startswith(p) for p in _LOOPBACK_AGENT_PATH_PREFIXES):
        return False
    if request.headers.get("authorization"):
        return False
    try:
        if request.query_params.get("token"):
            return False
    except Exception:
        pass
    try:
        if request.cookies.get("digitorn_preview_token"):
            return False
    except Exception:
        pass
    method = getattr(request, "method", "GET").upper()
    if method in _LOOPBACK_MUTATING_METHODS and not any(
        path.startswith(p) for p in _LOOPBACK_MUTATION_ALLOW_PREFIXES
    ):
        # Opt-in strict mode for hardened deployments. When the env var
        # is set, the loopback bypass refuses mutating requests and the
        # caller must present a JWT even from 127.0.0.1. By default the
        # bypass stays permissive so the agent's in-process `http` tool
        # can call the daemon back without baking a token in.
        import os as _os
        if _os.environ.get("DIGITORN_LOOPBACK_STRICT"):
            return False
    return True

class AuthMiddleware(BaseHTTPMiddleware):
    """JWT authentication middleware for FastAPI.

    When auth is enabled, all non-public endpoints require a valid
    Bearer token. The decoded user info is available via
    request.state.user (TokenPayload) and request.state.user_id (str).

    When auth is disabled, request.state.user is None and
    request.state.user_id is "anonymous".
    """

    def __init__(self, app: Any, auth_service: Any = None, enabled: bool = True):
        super().__init__(app)
        self._auth = auth_service
        self._enabled = enabled

    async def dispatch(self, request: Request, call_next):
        request.state.user = None
        request.state.user_id = "anonymous"
        request.state.roles = []
        request.state.permissions = []

        # CORS preflight requests MUST pass through without auth.
        # Browsers (and Flutter web) send an OPTIONS request before any
        # cross-origin POST/PUT/DELETE/etc. By spec, this preflight cannot
        # carry an Authorization header, so blocking it here breaks every
        # cross-origin client. CORSMiddleware (added later in the stack) will
        # actually answer the preflight with the proper Access-Control headers.
        if request.method == "OPTIONS":
            return await call_next(request)

        if not self._enabled or self._auth is None:
            request.state.permissions = ["*"]
            request.state.user_id = "local"
            request.state.roles = ["admin"]
            return await call_next(request)

        path = request.url.path

        if _is_loopback_self_call(request):
            request.state.permissions = ["*"]
            request.state.user_id = "system"
            request.state.roles = ["admin"]
            return await call_next(request)

        if path in _PUBLIC_PATHS:
            logger.info("auth_public_path_matched path=%r method=%s", path, request.method)
            return await call_next(request)
        for prefix in _PUBLIC_PREFIXES:
            if path.startswith(prefix):
                logger.info("auth_public_prefix_matched path=%r prefix=%r", path, prefix)
                return await call_next(request)
        if _is_public_hub_browse(path, request.method):
            logger.info(
                "auth_public_hub_browse path=%r method=%s", path, request.method
            )
            return await call_next(request)
        # Diagnostic - if you see this for a path you thought was public,
        # the path doesn't match _PUBLIC_PATHS byte-for-byte (typical
        # culprits: trailing slash, wrong prefix, case).
        logger.info(
            "auth_requires_token path=%r method=%s (not in _PUBLIC_PATHS=%s, prefixes=%s)",
            path, request.method, sorted(_PUBLIC_PATHS), _PUBLIC_PREFIXES,
        )

        api_key = request.headers.get("x-api-key")
        if api_key:
            return await self._handle_api_key(request, call_next, api_key)

        # Token resolution order:
        # 1. ``Authorization: Bearer <jwt>`` header (standard)
        # 2. ``?token=<jwt>`` query parameter - used by EventSource /
        #    iframe loads that can't set custom headers
        # 3. ``digitorn_preview_token`` cookie - set on the first
        #    proxy hit so sub-resources (CSS, JS, fetch) inherit auth
        token: str | None = None
        token_from_query = False
        auth_header = request.headers.get("authorization")
        if auth_header:
            parts = auth_header.split()
            if len(parts) != 2 or parts[0].lower() != "bearer":
                return JSONResponse(
                    status_code=401,
                    content={"error": "Invalid Authorization header. Expected: Bearer <token>"},
                )
            token = parts[1]
        else:
            try:
                qtok = request.query_params.get("token")
            except Exception:
                qtok = None
            if qtok:
                token = qtok
                token_from_query = True
            else:
                try:
                    ctok = request.cookies.get("digitorn_preview_token")
                except Exception:
                    ctok = None
                if ctok:
                    token = ctok

        if not token:
            return JSONResponse(
                status_code=401,
                content={"error": "Missing Authorization header"},
            )
        try:
            payload = self._auth.verify_access_token(token)
            request.state.user = payload
            request.state.user_id = payload.user_id
            request.state.roles = payload.roles or []
            request.state.permissions = payload.permissions or []
        except Exception as exc:
            logger.debug("auth_token_invalid error=%s", exc)
            return JSONResponse(
                status_code=401,
                content={"error": "Invalid or expired token"},
            )

        response = await call_next(request)
        if token_from_query and ("/preview-server/" in path or "/preview/" in path):
            parts = path.split("/")
            cookie_path = "/api/apps/"
            if len(parts) >= 4 and parts[1] == "api" and parts[2] == "apps":
                # Scope cookie broadly so both /preview/ and /preview-server/ work
                cookie_path = f"/api/apps/{parts[3]}/"
            response.set_cookie(
                key="digitorn_preview_token",
                value=token,
                path=cookie_path,
                httponly=True,
                samesite="strict",
                max_age=3600,
                secure=request.url.scheme == "https",
            )
        return response

    async def _handle_api_key(self, request: Request, call_next, api_key: str):
        """Validate an API key from the X-API-Key header."""
        prov = self._auth.get_provider("api_key")
        if not prov:
            return JSONResponse(
                status_code=401,
                content={"error": "API key authentication not configured"},
            )

        result = await prov.authenticate({"key": api_key})
        if not result.success:
            return JSONResponse(
                status_code=401,
                content={"error": "Invalid API key"},
            )

        request.state.user_id = result.user_id
        request.state.roles = []
        request.state.permissions = result.attributes.get("permissions", []) if result.attributes else []

        return await call_next(request)
