"""Credentials API - the HTTP surface for the universal credentials system.

Every interaction between the Flutter client and the daemon's
credentials subsystem goes through these routes. No Flutter code
should touch the old per-app ``/api/apps/{id}/secrets/{key}`` endpoint
for anything new - that one stays for backwards compat only.

Routes
------

    GET    /api/apps/{app_id}/credentials/schema
        Return the declared ``credentials_schema`` of an app +
        the current fill state for the authenticated user. This is
        what the Flutter form renders from.

    GET    /api/users/me/credentials/{app_id}/{provider}
        Fetch one credential's metadata (never plaintext fields) -
        used by the form to display "filled" / "missing" / masked
        previews.

    PUT    /api/users/me/credentials/{app_id}/{provider}
        Create or overwrite the fields of one credential. Runs the
        handler's ``validate_fields`` before accepting.

    DELETE /api/users/me/credentials/{app_id}/{provider}
        Delete the user's credential for this provider+app.

    GET    /api/users/me/credentials
        List every credential owned by the authenticated user
        across all apps (user-global dashboard).

    POST   /api/users/me/credentials/{app_id}/{provider}/oauth/start
    GET    /api/users/me/credentials/{app_id}/{provider}/oauth/status
    POST   /api/users/me/credentials/{app_id}/{provider}/oauth/refresh
        OAuth flow endpoints. Currently stubbed - full
        implementation is a follow-up task once the OAuth provider
        registry is designed.

    POST   /api/users/me/credentials/{app_id}/{provider}/mcp/start
    POST   /api/users/me/credentials/{app_id}/{provider}/mcp/stop
    GET    /api/users/me/credentials/{app_id}/{provider}/mcp/status
        MCP server lifecycle routes. Currently stubbed - they'll
        delegate to the MCP module's pool once the bridge is wired.

All routes are authenticated via the standard middleware that sets
``request.state.user_id``. No route lets an unauthenticated client
read or write any credential.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from digitorn.core.api.apps_v2 import AppResponse, _get_manager
from digitorn.core.credentials import (
    CredentialAuthRequired,
    CredentialNotFound,
    CredentialStore,
    OwnerType,
    Scope,
    Status,
    ValidationError,
    default_registry,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["credentials"])


# ────────────────────────────────────────────────────────────────────
# Dependencies
# ────────────────────────────────────────────────────────────────────


def _get_user_id(request: Request) -> str:
    """Pull the authenticated user id off ``request.state.user_id``.

    The auth middleware always populates this - the ``"anonymous"``
    fallback covers dev mode where auth is disabled.
    """
    return getattr(request.state, "user_id", None) or "anonymous"


def _get_credential_store(request: Request) -> CredentialStore:
    """Return the singleton CredentialStore stored on app.state."""
    store: CredentialStore | None = getattr(
        request.app.state, "credential_store", None,
    )
    if store is None:
        raise HTTPException(
            status_code=503,
            detail="Credential store not initialized",
        )
    return store


def _get_app_credentials_schema(
    request: Request, app_id: str,
) -> tuple[dict[str, Any] | None, Any]:
    """Return ``(schema_dict, compiled_app)`` for a deployed app.

    Raises 404 if the app is not deployed. Returns ``(None, app)``
    when the app has no credentials_schema declared.
    """
    manager = _get_manager(request)
    deployed = manager.get(app_id)
    if deployed is None:
        raise HTTPException(
            status_code=404, detail=f"App '{app_id}' not deployed",
        )
    schema = getattr(deployed.compiled.execution, "credentials_schema", None)
    return schema, deployed


# ────────────────────────────────────────────────────────────────────
# Request bodies
# ────────────────────────────────────────────────────────────────────


class CredentialUpsertRequest(BaseModel):
    """PUT /credentials/{app_id}/{provider} body."""

    fields: dict[str, Any] = Field(
        ...,
        description=(
            "Plaintext field values - they are immediately encrypted "
            "server-side before being persisted."
        ),
    )
    scope: str | None = Field(
        default=None,
        description=(
            "Override scope. Defaults to the scope declared in the "
            "app's credentials_schema for this provider."
        ),
    )


# ── New unified model bodies ─────────────────────────────────────────


class UserCredentialCreateRequest(BaseModel):
    """POST /api/credentials body for a user-owned credential.

    Unlike the legacy upsert, this endpoint does NOT attach the
    credential to an app - the user can later grant it to any app
    via ``POST /api/credentials/{id}/grants``.
    """

    provider_name: str = Field(..., description="Provider identifier (e.g. 'deepseek', 'notion').")
    provider_type: str = Field(default="api_key", description="Handler type: api_key, oauth2, mcp_server, ...")
    label: str = Field(default="default", description="Human-readable label for the picker UI.")
    fields: dict[str, Any] = Field(..., description="Plaintext fields to encrypt.")


class UserCredentialUpdateRequest(BaseModel):
    """PUT /api/credentials/{id} body - overwrites fields + label."""

    label: str | None = None
    fields: dict[str, Any] | None = None


class CreateGrantRequest(BaseModel):
    """POST /api/credentials/{id}/grants body - authorize an app."""

    app_id: str
    scopes_granted: list[str] | None = None


class SystemCredentialCreateRequest(BaseModel):
    """POST /api/admin/credentials body for admin/system credentials."""

    provider_name: str
    provider_type: str = "api_key"
    label: str = "default"
    app_id: str | None = Field(
        default=None,
        description="Restrict the system credential to a single app (enterprise case). None = visible to every app.",
    )
    fields: dict[str, Any]


# ────────────────────────────────────────────────────────────────────
# Routes
# ────────────────────────────────────────────────────────────────────


@router.get("/apps/{app_id}/credentials/schema", response_model=AppResponse)
async def get_app_credentials_schema(
    request: Request, app_id: str,
) -> AppResponse:
    """Return the app's declared credentials schema + current fill state.

    The Flutter form uses this to render the provider cards and tell
    the user which fields are required, filled, missing, or expired.
    """
    schema, _ = _get_app_credentials_schema(request, app_id)
    if schema is None:
        return AppResponse(
            success=True,
            data={
                "app_id": app_id,
                "credentials_schema": None,
                "complete": True,
                "providers": [],
                "missing_required": [],
            },
        )

    store = _get_credential_store(request)
    user_id = _get_user_id(request)

    providers = schema.get("providers") or []
    state = await store.list_for_schema(
        user_id=user_id,
        app_id=app_id,
        schema_providers=providers,
    )

    return AppResponse(
        success=True,
        data={
            "app_id": app_id,
            "credentials_schema": {
                "required": schema.get("required", True),
                "providers": providers,  # the full declared schema
            },
            "complete": state["complete"],
            "missing_required": state["missing_required"],
            "providers": state["providers"],  # with fill state
        },
    )


@router.get(
    "/users/me/credentials/{app_id}/{provider_name}",
    response_model=AppResponse,
)
async def get_credential(
    request: Request, app_id: str, provider_name: str,
) -> AppResponse:
    """Return one credential's metadata for the authenticated user.

    Never returns plaintext field values - only masked previews and
    status info. Use this to drive the form display.
    """
    store = _get_credential_store(request)
    user_id = _get_user_id(request)
    # First try per_app_per_user, then per_user (personal global).
    cred = await store.get_credential(
        user_id=user_id, app_id=app_id, provider_name=provider_name,
    )
    if cred is None:
        cred = await store.get_credential(
            user_id=user_id, app_id=None, provider_name=provider_name,
        )
    if cred is None:
        return AppResponse(
            success=True,
            data={"filled": False, "provider_name": provider_name},
        )
    return AppResponse(
        success=True,
        data={"filled": True, **cred},
    )


@router.put(
    "/users/me/credentials/{app_id}/{provider_name}",
    response_model=AppResponse,
)
async def upsert_credential(
    request: Request,
    app_id: str,
    provider_name: str,
    body: CredentialUpsertRequest,
) -> AppResponse:
    """Create or overwrite a credential for the authenticated user + app.

    Validates via the declared handler before storing. Use ``app_id="_global"``
    to store a per-user credential that is shared across every app the
    user runs (the per_user scope).
    """
    user_id = _get_user_id(request)
    store = _get_credential_store(request)

    # Resolve scope + provider_type from the app schema (or _global default).
    scope = body.scope
    provider_type = "api_key"
    schema_fields: list[dict[str, Any]] = []
    schema_provider: dict[str, Any] = {}

    if app_id == "_global":
        effective_app_id: str | None = None
        effective_scope = Scope.PER_USER
    else:
        schema, _ = _get_app_credentials_schema(request, app_id)
        if schema is None:
            raise HTTPException(
                status_code=404,
                detail=(
                    f"App '{app_id}' has no credentials_schema - cannot "
                    f"upsert credentials against an app that doesn't "
                    f"declare what it needs."
                ),
            )
        for p in (schema.get("providers") or []):
            if p.get("name") == provider_name:
                schema_provider = p
                break
        if not schema_provider:
            raise HTTPException(
                status_code=404,
                detail=f"Provider '{provider_name}' not declared in app schema",
            )
        declared_scope = schema_provider.get("scope", "per_user")
        provider_type = schema_provider.get("type", "api_key")
        schema_fields = schema_provider.get("fields", []) or []

        # Figure out the effective scope tuple from the declared scope.
        if declared_scope == "per_user":
            effective_scope = Scope.PER_USER
            effective_app_id = None  # per-user crosses apps
        elif declared_scope == "per_app_shared":
            effective_scope = Scope.PER_APP_SHARED
            effective_app_id = app_id
            # Per-app shared is admin-only. We keep it simple and
            # check a permission flag. TODO: real RBAC.
            perms = getattr(request.state, "permissions", [])
            if "*" not in perms:
                raise HTTPException(
                    status_code=403,
                    detail="per_app_shared credentials require admin permissions",
                )
            user_id = None
        elif declared_scope == "system_wide":
            raise HTTPException(
                status_code=403,
                detail=(
                    "system_wide credentials must be set via the daemon "
                    "configuration, not via this route"
                ),
            )
        else:
            raise HTTPException(
                status_code=400,
                detail=f"unknown scope '{declared_scope}' in schema",
            )
        if scope:
            # Body override for power users
            effective_scope = scope

    # Validate via the handler
    handler = default_registry.get(provider_type)
    try:
        handler.validate_fields(body.fields, schema_fields)
    except ValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    stored = await store.upsert_credential(
        user_id=user_id,
        app_id=effective_app_id,
        provider_name=provider_name,
        provider_type=provider_type,
        scope=effective_scope,
        fields=body.fields,
        status=Status.FILLED,
    )

    # Fire the on_credential_filled hook (MCP spawn, OAuth prefetch, etc.)
    try:
        ctx = _build_handler_ctx(request)
        await handler.on_credential_filled(
            {**stored, "fields": body.fields}, schema_provider, ctx,
        )
    except Exception as exc:
        logger.warning(
            "on_credential_filled hook failed for %s/%s: %s",
            app_id, provider_name, exc,
        )

    return AppResponse(success=True, data=stored)


@router.delete(
    "/users/me/credentials/{app_id}/{provider_name}",
    response_model=AppResponse,
)
async def delete_credential(
    request: Request, app_id: str, provider_name: str,
) -> AppResponse:
    """Delete the caller's credential for this provider+app."""
    user_id = _get_user_id(request)
    store = _get_credential_store(request)

    # Try per_app_per_user then per_user (global)
    effective_app_id: str | None = app_id if app_id != "_global" else None

    ok = await store.delete_credential(
        user_id=user_id,
        app_id=effective_app_id,
        provider_name=provider_name,
    )
    if not ok:
        raise HTTPException(status_code=404, detail="Credential not found")
    return AppResponse(success=True, data={"deleted": True})


@router.get("/users/me/credentials", response_model=AppResponse)
async def list_user_credentials(request: Request) -> AppResponse:
    """List every credential owned by the authenticated user.

    Used by the "My Credentials" global dashboard - shows every
    provider the user has configured across every app, with their
    status and last_updated.
    """
    user_id = _get_user_id(request)
    store = _get_credential_store(request)
    creds = await store.list_credentials(user_id=user_id)
    return AppResponse(
        success=True,
        data={"credentials": creds, "count": len(creds)},
    )


# ────────────────────────────────────────────────────────────────────
# OAuth - real implementation
# ────────────────────────────────────────────────────────────────────
#
# Flow:
#
#   1. Client → POST /oauth/start
#        → daemon creates a PendingFlow with a CSRF state uuid
#        → daemon returns { auth_url, state }
#
#   2. Browser opens auth_url, user consents on the provider's site
#
#   3. Provider redirects to the callback URL with ?code=... &state=...
#      → daemon's /api/oauth/callback route looks up the state,
#        exchanges the code for tokens, writes the credential, and
#        marks the flow as "connected"
#
#   4. Client polls GET /oauth/status?state=xxx until status == connected


@router.post(
    "/users/me/credentials/{app_id}/{provider_name}/oauth/start",
    response_model=AppResponse,
)
async def oauth_start(
    request: Request, app_id: str, provider_name: str,
) -> AppResponse:
    """Begin the OAuth flow - returns ``auth_url`` and a ``state`` uuid.

    The client opens ``auth_url`` in a browser; the user consents;
    the provider redirects back to the daemon's callback route. The
    client polls ``oauth/status`` to know when the flow completes.
    """
    from digitorn.core.credentials.oauth_flow import (
        build_auth_url,
        get_default_flow_store,
    )
    from digitorn.core.credentials.oauth_providers import get_default_registry

    user_id = _get_user_id(request)

    # Look up the schema to find the oauth_provider name and scopes
    schema, _ = _get_app_credentials_schema(request, app_id)
    schema_provider: dict[str, Any] = {}
    if schema is not None:
        for p in (schema.get("providers") or []):
            if p.get("name") == provider_name:
                schema_provider = p
                break

    oauth_provider_name = schema_provider.get("oauth_provider") or provider_name
    scopes = schema_provider.get("oauth_scopes")

    # Incremental scope upgrade - if the user already has a cred for
    # this provider and its granted scopes are a subset of what we
    # need, expand the auth request to the UNION so we don't lose
    # previously granted scopes when re-consenting.
    store = _get_credential_store(request)
    try:
        existing = await store.list_user_credentials(
            user_id=user_id, provider_name=oauth_provider_name,
        )
        if existing and scopes:
            prior: set[str] = set()
            for cred in existing:
                prior.update(
                    (cred.get("display_metadata") or {}).get("oauth_scopes") or []
                )
            wanted = set(scopes) | prior
            if wanted != set(scopes):
                scopes = sorted(wanted)
                logger.info(
                    "oauth_start: incremental scope upgrade for provider=%s "
                    "prior=%s requested=%s",
                    oauth_provider_name, sorted(prior), scopes,
                )
    except Exception as exc:
        logger.debug("oauth scope upgrade check skipped: %s", exc)

    registry = get_default_registry()
    provider = registry.get(oauth_provider_name)
    if provider is None:
        raise HTTPException(
            status_code=404,
            detail=(
                f"OAuth provider {oauth_provider_name!r} is not registered "
                f"in ~/.digitorn/oauth_providers.toml. Configured providers: "
                f"{registry.list_configured()}"
            ),
        )
    if not provider.is_configured():
        raise HTTPException(
            status_code=409,
            detail=(
                f"OAuth provider {oauth_provider_name!r} is known but has "
                f"no client_id / client_secret set. Edit "
                f"~/.digitorn/oauth_providers.toml or set the corresponding "
                f"env vars."
            ),
        )

    flow_store = get_default_flow_store()
    try:
        flow = await flow_store.start(
            provider=provider,
            user_id=user_id,
            app_id=app_id,
            scopes=scopes,
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc))

    auth_url = build_auth_url(flow)
    return AppResponse(
        success=True,
        data={
            "auth_url": auth_url,
            "state": flow.state,
            "provider": oauth_provider_name,
            "scopes": flow.scopes,
        },
    )


@router.get(
    "/users/me/credentials/{app_id}/{provider_name}/oauth/status",
    response_model=AppResponse,
)
async def oauth_status(
    request: Request, app_id: str, provider_name: str, state: str = "",
) -> AppResponse:
    """Poll the state of an in-progress OAuth flow.

    Returns one of:

        { status: "pending" }                     - still waiting
        { status: "connected", credential_id }    - done, token stored
        { status: "error", error }                - failed
        { status: "expired" }                     - flow TTL elapsed
    """
    if not state:
        raise HTTPException(status_code=400, detail="state query param required")

    from digitorn.core.credentials.oauth_flow import get_default_flow_store

    flow_store = get_default_flow_store()
    flow = await flow_store.get(state)
    if flow is None:
        return AppResponse(success=True, data={"status": "expired"})

    # Defensive: only the user who started the flow can read its status
    if flow.user_id != _get_user_id(request):
        raise HTTPException(status_code=403, detail="state does not belong to caller")

    data: dict[str, Any] = {"status": flow.status}
    if flow.status == "connected":
        data["credential_id"] = flow.resulting_credential_id
    elif flow.status == "error":
        data["error"] = flow.error
    return AppResponse(success=True, data=data)


@router.post(
    "/users/me/credentials/{app_id}/{provider_name}/oauth/refresh",
    response_model=AppResponse,
)
async def oauth_refresh(
    request: Request, app_id: str, provider_name: str,
) -> AppResponse:
    """Manually trigger a token refresh for an existing OAuth credential.

    Looks up the stored credential, calls the handler's ``refresh``
    (which hits the provider's /token endpoint with
    grant_type=refresh_token), and writes the new access_token back
    to the store.
    """
    from digitorn.core.credentials import default_registry as handler_registry

    store = _get_credential_store(request)
    user_id = _get_user_id(request)

    # Find the stored credential - OAuth is always per_user.
    stored = await store.get_credential(
        user_id=user_id,
        app_id=None,
        provider_name=provider_name,
        decrypt=True,
    )
    if stored is None:
        raise HTTPException(
            status_code=404,
            detail=f"No OAuth credential for provider {provider_name!r}",
        )

    # Pull the schema_provider declaration to know the oauth_provider name
    schema, _ = _get_app_credentials_schema(request, app_id)
    schema_provider: dict[str, Any] = {}
    if schema is not None:
        for p in (schema.get("providers") or []):
            if p.get("name") == provider_name:
                schema_provider = p
                break

    handler = handler_registry.get("oauth2")
    try:
        refreshed = await handler.refresh(stored, schema_provider)
    except Exception as exc:
        logger.exception("oauth refresh failed for %s", provider_name)
        raise HTTPException(
            status_code=500,
            detail=f"refresh failed: {exc}",
        )

    # Persist the refreshed credential. The handler returns
    # ``expires_at`` as an ISO 8601 string - the store accepts both
    # datetime and string and normalises internally.
    from digitorn.core.credentials import Scope, Status
    await store.upsert_credential(
        user_id=user_id,
        app_id=None,
        provider_name=provider_name,
        provider_type="oauth2",
        scope=Scope.PER_USER,
        fields=refreshed.get("fields") or {},
        status=refreshed.get("status") or Status.VALID,
        expires_at=refreshed.get("expires_at"),
        display_metadata=(stored.get("display_metadata") or {}),
    )
    return AppResponse(
        success=True,
        data={
            "refreshed": True,
            "expires_at": refreshed.get("expires_at"),
            "status": refreshed.get("status"),
        },
    )


# ────────────────────────────────────────────────────────────────────
# OAuth callback - PUBLIC endpoint, no auth required
# ────────────────────────────────────────────────────────────────────
#
# The provider redirects the user's browser to this URL with the
# authorization code + our state uuid. The state uuid IS the auth:
# only the client that started the flow knows it.
#
# This route is mounted on the same router but it is INTENTIONALLY
# at ``/api/oauth/callback`` (no user/app prefix) because the redirect
# URL must be a fixed value registered with the provider.


oauth_callback_router = APIRouter(prefix="/api/oauth", tags=["oauth"])


@oauth_callback_router.get("/callback")
async def oauth_callback(
    request: Request,
    code: str = "",
    state: str = "",
    error: str = "",
    error_description: str = "",
):
    """Provider → daemon redirect target. Completes the OAuth flow.

    Returns a minimal HTML page telling the user to close the tab
    and go back to their app. The real result is tracked in the
    in-memory flow store and consumed by the client polling
    ``oauth/status``.
    """
    from fastapi.responses import HTMLResponse
    from digitorn.core.credentials.oauth_flow import (
        TokenExchange,
        TokenExchangeError,
        get_default_flow_store,
        persist_oauth_credential,
    )

    flow_store = get_default_flow_store()
    flow = await flow_store.get(state)

    # Provider sent us an error instead of a code
    if error or not code:
        msg = error_description or error or "no code returned"
        if flow is not None:
            await flow_store.mark_error(state, msg)
        return HTMLResponse(
            _render_callback_html(
                title="Authorization failed",
                message=f"{msg}<br><br>You can close this tab.",
                success=False,
            ),
            status_code=400,
        )

    if flow is None:
        return HTMLResponse(
            _render_callback_html(
                title="Flow not found or expired",
                message="Please restart the connection flow from your app.",
                success=False,
            ),
            status_code=404,
        )

    # Exchange the code for a token
    try:
        token_response = await TokenExchange.exchange_code(flow.provider, code)
    except TokenExchangeError as exc:
        await flow_store.mark_error(state, str(exc))
        return HTMLResponse(
            _render_callback_html(
                title="Token exchange failed",
                message=str(exc),
                success=False,
            ),
            status_code=502,
        )

    # Persist the credential
    store: CredentialStore | None = getattr(
        request.app.state, "credential_store", None,
    )
    if store is None:
        await flow_store.mark_error(state, "credential store not initialized")
        return HTMLResponse(
            _render_callback_html(
                title="Server misconfigured",
                message="Credential store unavailable.",
                success=False,
            ),
            status_code=500,
        )

    try:
        stored = await persist_oauth_credential(
            store=store, flow=flow, token_response=token_response,
        )
    except Exception as exc:
        logger.exception("persist_oauth_credential failed")
        await flow_store.mark_error(state, f"persist failed: {exc}")
        return HTMLResponse(
            _render_callback_html(
                title="Could not save credential",
                message=str(exc),
                success=False,
            ),
            status_code=500,
        )

    await flow_store.mark_connected(state, stored["id"])

    return HTMLResponse(
        _render_callback_html(
            title="✅ Connected",
            message=(
                f"Successfully connected to <b>{flow.provider_name}</b>. "
                f"You can now close this tab and return to your app."
            ),
            success=True,
        )
    )


def _render_callback_html(*, title: str, message: str, success: bool) -> str:
    """Tiny self-contained HTML page shown in the OAuth popup/tab.

    No JS, no fonts, no external resources - has to work even when
    the user's browser is locked down.
    """
    color = "#10B981" if success else "#EF4444"
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><title>{title}</title>
<style>
  body {{
    font-family: -apple-system, Segoe UI, Roboto, sans-serif;
    display: flex; align-items: center; justify-content: center;
    min-height: 100vh; margin: 0;
    background: #0F172A; color: #E2E8F0;
  }}
  .card {{
    max-width: 440px; padding: 32px;
    background: #1E293B; border-radius: 12px;
    border-top: 4px solid {color};
    text-align: center;
  }}
  h1 {{ margin: 0 0 16px; font-size: 22px; }}
  p {{ margin: 0; line-height: 1.5; color: #CBD5E1; }}
</style></head>
<body>
  <div class="card">
    <h1>{title}</h1>
    <p>{message}</p>
  </div>
</body></html>"""


# ────────────────────────────────────────────────────────────────────
# MCP server control - stub delegating to the module's pool
# ────────────────────────────────────────────────────────────────────


@router.post(
    "/users/me/credentials/{app_id}/{provider_name}/mcp/start",
    response_model=AppResponse,
)
async def mcp_start(
    request: Request, app_id: str, provider_name: str,
) -> AppResponse:
    """Start (or restart) an MCP server from a filled credential.

    Looks up the credential (per_app_per_user → per_user), retrieves
    its schema declaration, and invokes the
    ``McpServerHandler.on_credential_filled`` hook which talks to the
    live ``MCPConnectionPool``.
    """
    from digitorn.core.credentials import default_registry

    user_id = _get_user_id(request)
    store = _get_credential_store(request)
    cred = await store.get_credential(
        user_id=user_id, app_id=app_id, provider_name=provider_name,
        decrypt=True,
    )
    if cred is None:
        cred = await store.get_credential(
            user_id=user_id, app_id=None, provider_name=provider_name,
            decrypt=True,
        )
    if cred is None:
        raise HTTPException(
            status_code=404,
            detail=f"No credential for {provider_name!r}",
        )

    schema, _ = _get_app_credentials_schema(request, app_id)
    schema_provider: dict[str, Any] = {}
    for p in (schema.get("providers") or []) if schema else []:
        if p.get("name") == provider_name:
            schema_provider = p
            break
    if not schema_provider:
        raise HTTPException(
            status_code=404,
            detail=f"Provider {provider_name!r} not declared in app schema",
        )

    handler = default_registry.get("mcp_server")
    ctx = _build_handler_ctx(request)
    try:
        await handler.on_credential_filled(cred, schema_provider, ctx)
    except Exception as exc:
        logger.exception("mcp_start failed for %s", provider_name)
        raise HTTPException(status_code=500, detail=f"mcp start failed: {exc}")

    return AppResponse(
        success=True,
        data={"started": True, "provider": provider_name},
    )


@router.post(
    "/users/me/credentials/{app_id}/{provider_name}/mcp/stop",
    response_model=AppResponse,
)
async def mcp_stop(
    request: Request, app_id: str, provider_name: str,
) -> AppResponse:
    """Stop an MCP server without deleting its credential.

    The credential row stays intact so the user can click "Start
    again" without re-filling the form.
    """
    ctx = _build_handler_ctx(request)
    pool = getattr(ctx, "mcp_pool", None)
    if pool is None:
        raise HTTPException(
            status_code=404, detail="MCP pool not initialized on this daemon",
        )
    try:
        await pool.disconnect(provider_name)
    except Exception as exc:
        logger.warning("mcp_stop failed for %s: %s", provider_name, exc)
        raise HTTPException(status_code=500, detail=f"stop failed: {exc}")
    return AppResponse(
        success=True,
        data={"stopped": True, "provider": provider_name},
    )


@router.get(
    "/users/me/credentials/{app_id}/{provider_name}/mcp/status",
    response_model=AppResponse,
)
async def mcp_status(
    request: Request, app_id: str, provider_name: str,
) -> AppResponse:
    """Return the current runtime status of an MCP server.

    Shape::

        { provider, running, status, tools_count, last_error }
    """
    ctx = _build_handler_ctx(request)
    pool = getattr(ctx, "mcp_pool", None)
    if pool is None:
        raise HTTPException(
            status_code=404, detail="MCP pool not initialized on this daemon",
        )
    entry = pool.get_server(provider_name)
    if entry is None:
        return AppResponse(
            success=True,
            data={
                "provider": provider_name,
                "running": False,
                "status": "not_registered",
                "tools_count": 0,
                "last_error": None,
            },
        )
    return AppResponse(
        success=True,
        data={
            "provider": provider_name,
            "running": getattr(entry, "status", "") == "connected",
            "status": getattr(entry, "status", "unknown"),
            "tools_count": len(getattr(entry, "tools", []) or []),
            "last_error": getattr(entry, "error", None),
            "transport_type": getattr(entry, "transport_type", ""),
            "created_at": getattr(entry, "created_at", None),
        },
    )


# ────────────────────────────────────────────────────────────────────
# Helpers
# ────────────────────────────────────────────────────────────────────


# ════════════════════════════════════════════════════════════════════
# Unified model routes - user-owned credentials + grants
# ════════════════════════════════════════════════════════════════════
#
# The routes above manage legacy per-app credentials for backwards
# compat. Everything below is the new grant-based model:
#
#     /api/credentials                             list/create user creds
#     /api/credentials/{id}                        get/update/delete one
#     /api/credentials/{id}/grants                 list/create grants
#     /api/credentials/{id}/grants/{app_id}        revoke one grant
#     /api/credentials/grants                      list every grant of caller
#     /api/admin/credentials                       admin CRUD for system creds
#
# The same ``router`` is used - the prefix is ``/api`` already.


# Catalog of well-known credential providers. Used by the
# "Add credential" modal in the Flutter client so the picker
# shows icons + correct field hints per provider.
_PROVIDER_CATALOG: list[dict[str, Any]] = [
    # LLM providers
    {"id": "anthropic", "display_name": "Anthropic", "category": "llm",
     "type": "api_key", "icon": "🧠", "fields": ["api_key"],
     "docs_url": "https://console.anthropic.com/settings/keys"},
    {"id": "openai", "display_name": "OpenAI", "category": "llm",
     "type": "api_key", "icon": "🤖", "fields": ["api_key"],
     "docs_url": "https://platform.openai.com/api-keys"},
    {"id": "deepseek", "display_name": "DeepSeek", "category": "llm",
     "type": "api_key", "icon": "🔬", "fields": ["api_key"],
     "docs_url": "https://platform.deepseek.com/api_keys"},
    {"id": "google-gemini", "display_name": "Google Gemini", "category": "llm",
     "type": "api_key", "icon": "✨", "fields": ["api_key"],
     "docs_url": "https://aistudio.google.com/apikey"},
    {"id": "mistral", "display_name": "Mistral", "category": "llm",
     "type": "api_key", "icon": "🌬", "fields": ["api_key"],
     "docs_url": "https://console.mistral.ai/api-keys/"},
    {"id": "groq", "display_name": "Groq", "category": "llm",
     "type": "api_key", "icon": "⚡", "fields": ["api_key"],
     "docs_url": "https://console.groq.com/keys"},
    {"id": "together", "display_name": "Together AI", "category": "llm",
     "type": "api_key", "icon": "🤝", "fields": ["api_key"],
     "docs_url": "https://api.together.xyz/settings/api-keys"},
    {"id": "ollama", "display_name": "Ollama", "category": "llm",
     "type": "api_key", "icon": "🦙", "fields": ["api_key", "base_url"],
     "docs_url": "https://ollama.com/"},
    # Dev tools
    {"id": "github", "display_name": "GitHub", "category": "developer-tools",
     "type": "oauth2", "icon": "🐙", "fields": ["token"],
     "docs_url": "https://github.com/settings/tokens"},
    {"id": "gitlab", "display_name": "GitLab", "category": "developer-tools",
     "type": "oauth2", "icon": "🦊", "fields": ["token"],
     "docs_url": "https://gitlab.com/-/user_settings/personal_access_tokens"},
    # Productivity
    {"id": "notion", "display_name": "Notion", "category": "productivity",
     "type": "oauth2", "icon": "📝", "fields": ["token"],
     "docs_url": "https://www.notion.so/my-integrations"},
    {"id": "linear", "display_name": "Linear", "category": "productivity",
     "type": "api_key", "icon": "📐", "fields": ["api_key"],
     "docs_url": "https://linear.app/settings/api"},
    {"id": "clickup", "display_name": "ClickUp", "category": "productivity",
     "type": "api_key", "icon": "✅", "fields": ["api_key"],
     "docs_url": "https://app.clickup.com/settings/apps"},
    {"id": "atlassian", "display_name": "Atlassian (Jira/Confluence)",
     "category": "productivity", "type": "oauth2", "icon": "📊",
     "fields": ["email", "api_token", "site_url"]},
    # Communication
    {"id": "slack", "display_name": "Slack", "category": "communication",
     "type": "multi_field", "icon": "💬",
     "fields": ["bot_token", "team_id", "channel_ids"],
     "docs_url": "https://api.slack.com/apps"},
    {"id": "discord", "display_name": "Discord", "category": "communication",
     "type": "api_key", "icon": "🎮", "fields": ["token"]},
    {"id": "telegram", "display_name": "Telegram", "category": "communication",
     "type": "api_key", "icon": "✈️", "fields": ["bot_token"]},
    # Google
    {"id": "google-drive", "display_name": "Google Drive",
     "category": "productivity", "type": "oauth2", "icon": "📁",
     "fields": []},
    {"id": "google-calendar", "display_name": "Google Calendar",
     "category": "productivity", "type": "oauth2", "icon": "📅",
     "fields": []},
    {"id": "gmail", "display_name": "Gmail", "category": "communication",
     "type": "oauth2", "icon": "📧", "fields": []},
    {"id": "google-maps", "display_name": "Google Maps", "category": "data",
     "type": "api_key", "icon": "🗺", "fields": ["api_key"]},
    # Finance
    {"id": "stripe", "display_name": "Stripe", "category": "finance",
     "type": "api_key", "icon": "💳", "fields": ["api_key"],
     "docs_url": "https://dashboard.stripe.com/apikeys"},
    {"id": "shopify", "display_name": "Shopify", "category": "finance",
     "type": "api_key", "icon": "🛍", "fields": ["access_token"]},
    {"id": "paypal", "display_name": "PayPal", "category": "finance",
     "type": "multi_field", "icon": "💰",
     "fields": ["client_id", "client_secret"]},
    # Email
    {"id": "mailgun", "display_name": "Mailgun", "category": "communication",
     "type": "multi_field", "icon": "📮",
     "fields": ["api_key", "domain"]},
    # MCP server - generic entry
    {"id": "mcp-server", "display_name": "MCP Server (generic)",
     "category": "developer-tools", "type": "mcp_server", "icon": "🧩",
     "fields": []},
]


@router.get("/credentials/providers", response_model=AppResponse)
async def list_credential_providers(request: Request) -> AppResponse:
    """Return the catalog of well-known credential providers.

    Used by the "Add credential" modal to render a picker with
    icons and display names instead of a flat text input. The list
    is static (shipped with the daemon) and stable - the Flutter
    client can cache it indefinitely.

    Shape per entry::

        {
          "id": "anthropic",           # provider_name to POST
          "display_name": "Anthropic",
          "category": "llm",            # llm | productivity | communication | ...
          "type": "api_key",            # credential handler type
          "icon": "🧠",                 # emoji for quick render
          "fields": ["api_key"],        # expected field names
          "docs_url": "https://..."     # where to get the key (optional)
        }
    """
    return AppResponse(
        success=True,
        data={
            "providers": _PROVIDER_CATALOG,
            "count": len(_PROVIDER_CATALOG),
        },
    )


@router.get("/credentials", response_model=AppResponse)
async def list_my_credentials(
    request: Request, provider: str = "",
) -> AppResponse:
    """List every credential owned by the authenticated user.

    Optional ``?provider=deepseek`` narrows to one provider - this
    is what the picker dialog uses to show candidates on first-use.
    """
    store = _get_credential_store(request)
    user_id = _get_user_id(request)
    creds = await store.list_user_credentials(
        user_id=user_id,
        provider_name=(provider or None),
    )
    return AppResponse(
        success=True,
        data={"credentials": creds, "count": len(creds)},
    )


@router.post("/credentials", response_model=AppResponse)
async def create_my_credential(
    request: Request, body: UserCredentialCreateRequest,
) -> AppResponse:
    """Create a new user-owned credential.

    Validates via the declared handler before storing. The credential
    is NOT attached to any app - the user authorizes specific apps
    later via ``POST /api/credentials/{id}/grants``.
    """
    store = _get_credential_store(request)
    user_id = _get_user_id(request)

    handler = default_registry.get(body.provider_type)
    try:
        handler.validate_fields(body.fields, [])
    except ValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    stored = await store.upsert_user_credential(
        user_id=user_id,
        provider_name=body.provider_name,
        provider_type=body.provider_type,
        label=body.label,
        fields=body.fields,
        status=Status.FILLED,
    )
    return AppResponse(success=True, data=stored)


@router.get("/credentials/{credential_id}", response_model=AppResponse)
async def get_my_credential_by_id(
    request: Request, credential_id: str,
) -> AppResponse:
    """Fetch one credential by id. Never returns plaintext fields."""
    store = _get_credential_store(request)
    user_id = _get_user_id(request)
    cred = await store.get_credential_by_id(credential_id)
    if cred is None:
        raise HTTPException(status_code=404, detail="Credential not found")
    if cred.get("user_id") != user_id and cred.get("owner_type") != OwnerType.SYSTEM:
        raise HTTPException(
            status_code=403, detail="You can only view your own credentials",
        )
    return AppResponse(success=True, data=cred)


@router.put("/credentials/{credential_id}", response_model=AppResponse)
async def update_my_credential(
    request: Request,
    credential_id: str,
    body: UserCredentialUpdateRequest,
) -> AppResponse:
    """Overwrite label and/or fields on an existing credential."""
    store = _get_credential_store(request)
    user_id = _get_user_id(request)

    cred = await store.get_credential_by_id(credential_id)
    if cred is None:
        raise HTTPException(status_code=404, detail="Credential not found")
    if cred.get("user_id") != user_id:
        raise HTTPException(
            status_code=403,
            detail="You can only update your own credentials",
        )

    if body.fields is not None:
        handler = default_registry.get(cred["provider_type"])
        try:
            handler.validate_fields(body.fields, [])
        except ValidationError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    # Read plaintext of the existing row if we need to keep fields
    existing_fields: dict[str, Any] = {}
    if body.fields is None:
        full = await store.get_credential_by_id(credential_id, decrypt=True)
        existing_fields = (full or {}).get("fields") or {}

    updated = await store.upsert_user_credential(
        user_id=user_id,
        provider_name=cred["provider_name"],
        provider_type=cred["provider_type"],
        label=body.label or cred.get("label", "default"),
        fields=body.fields if body.fields is not None else existing_fields,
        credential_id=credential_id,
    )
    return AppResponse(success=True, data=updated)


@router.delete("/credentials/{credential_id}", response_model=AppResponse)
async def delete_my_credential(
    request: Request, credential_id: str,
) -> AppResponse:
    """Delete a credential (and cascade-delete all its grants)."""
    store = _get_credential_store(request)
    user_id = _get_user_id(request)

    cred = await store.get_credential_by_id(credential_id)
    if cred is None:
        raise HTTPException(status_code=404, detail="Credential not found")
    if cred.get("user_id") != user_id:
        raise HTTPException(
            status_code=403,
            detail="You can only delete your own credentials",
        )
    ok = await store.delete_credential_by_id(credential_id)
    return AppResponse(success=True, data={"deleted": ok})


@router.get("/credentials/{credential_id}/grants", response_model=AppResponse)
async def list_credential_grants(
    request: Request, credential_id: str,
) -> AppResponse:
    """List all grants for a single credential."""
    store = _get_credential_store(request)
    user_id = _get_user_id(request)
    grants = await store.list_grants(
        user_id=user_id, credential_id=credential_id,
    )
    return AppResponse(
        success=True, data={"grants": grants, "count": len(grants)},
    )


@router.post("/credentials/{credential_id}/grant", response_model=AppResponse)
async def create_credential_grant_singular(
    request: Request, credential_id: str, body: CreateGrantRequest,
) -> AppResponse:
    """Alias for ``POST /credentials/{id}/grants`` - singular form."""
    return await create_credential_grant(request, credential_id, body)


@router.post("/credentials/{credential_id}/grants", response_model=AppResponse)
async def create_credential_grant(
    request: Request, credential_id: str, body: CreateGrantRequest,
) -> AppResponse:
    """Authorize an app to use this credential.

    This is the endpoint the picker dialog calls when the user clicks
    "Use this key" - it posts the credential_id + app_id, and from
    then on the app can read the credential without prompting.

    Side effect: when the credential is an ``mcp_server``, this also
    fires the handler's spawn hook so the subprocess is ready by the
    time the first agent turn hits the MCP tool.
    """
    store = _get_credential_store(request)
    user_id = _get_user_id(request)
    try:
        grant = await store.create_grant(
            credential_id=credential_id,
            user_id=user_id,
            app_id=body.app_id,
            scopes_granted=body.scopes_granted,
        )
    except CredentialNotFound:
        raise HTTPException(status_code=404, detail="Credential not found")
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    # Fire MCP spawn hook if applicable. The credential has the type
    # metadata; we look up the schema declaration on the target app
    # to pass to on_credential_filled (transport, command, env_template).
    try:
        cred_full = await store.get_credential_by_id(credential_id, decrypt=True)
        if cred_full and cred_full.get("provider_type") == "mcp_server":
            schema, _deployed = _get_app_credentials_schema(request, body.app_id)
            schema_provider: dict[str, Any] = {}
            for p in (schema.get("providers") or []) if schema else []:
                if p.get("name") == cred_full["provider_name"]:
                    schema_provider = p
                    break
            if schema_provider:
                handler = default_registry.get("mcp_server")
                ctx = _build_handler_ctx(request)
                await handler.on_credential_filled(
                    cred_full, schema_provider, ctx,
                )
    except Exception as exc:
        logger.warning(
            "create_credential_grant: MCP spawn hook failed for %s: %s",
            credential_id, exc,
        )

    return AppResponse(success=True, data=grant)


@router.delete(
    "/credentials/{credential_id}/grants/{app_id}",
    response_model=AppResponse,
)
async def revoke_credential_grant(
    request: Request, credential_id: str, app_id: str,
) -> AppResponse:
    """Revoke an app's access to a credential.

    Soft delete - the grant row stays with ``revoked_at`` set for
    audit. Use ``?hard=true`` to actually remove the row.
    """
    store = _get_credential_store(request)
    user_id = _get_user_id(request)
    hard = request.query_params.get("hard", "").lower() in ("1", "true", "yes")
    ok = await store.revoke_grant(
        credential_id=credential_id, app_id=app_id,
        user_id=user_id, hard=hard,
    )
    if not ok:
        raise HTTPException(status_code=404, detail="Grant not found")
    return AppResponse(success=True, data={"revoked": True, "hard": hard})


@router.get("/credentials-grants", response_model=AppResponse)
async def list_all_my_grants(request: Request) -> AppResponse:
    """List every active grant of the authenticated user.

    Settings screen calls this to show "Apps that can see my
    credentials" - grouped by app_id on the client side.
    """
    store = _get_credential_store(request)
    user_id = _get_user_id(request)
    grants = await store.list_grants(user_id=user_id)
    return AppResponse(
        success=True,
        data={"grants": grants, "count": len(grants)},
    )


# ── Admin: system credentials ────────────────────────────────────────


def _require_admin(request: Request) -> None:
    """Block non-admin callers from touching system credentials.

    Uses ``request.state.permissions`` populated by the auth
    middleware. In dev mode the middleware grants ``["*"]`` so this
    is effectively a no-op; in production it enforces real RBAC.
    """
    perms = getattr(request.state, "permissions", [])
    if "*" in perms or "credentials.admin" in perms:
        return
    raise HTTPException(
        status_code=403,
        detail="Admin permissions required to manage system credentials",
    )


@router.get("/admin/credentials", response_model=AppResponse)
async def admin_list_system_credentials(
    request: Request, provider: str = "", app_id: str = "",
) -> AppResponse:
    """List every admin-owned credential visible to the daemon."""
    _require_admin(request)
    store = _get_credential_store(request)
    creds = await store.list_system_credentials(
        provider_name=(provider or None),
        app_id=(app_id or None),
    )
    return AppResponse(
        success=True, data={"credentials": creds, "count": len(creds)},
    )


@router.post("/admin/credentials", response_model=AppResponse)
async def admin_create_system_credential(
    request: Request, body: SystemCredentialCreateRequest,
) -> AppResponse:
    """Create a system-owned credential.

    When ``app_id`` is set the credential is restricted to that app
    (enterprise "shared key for this one app" case). When ``app_id``
    is None the credential is visible to every app on the daemon.

    No grants are needed - system credentials are implicitly
    available to the apps they cover.
    """
    _require_admin(request)
    store = _get_credential_store(request)

    handler = default_registry.get(body.provider_type)
    try:
        handler.validate_fields(body.fields, [])
    except ValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    stored = await store.upsert_system_credential(
        provider_name=body.provider_name,
        provider_type=body.provider_type,
        label=body.label,
        app_id=body.app_id,
        fields=body.fields,
    )
    return AppResponse(success=True, data=stored)


@router.delete("/admin/credentials/{credential_id}", response_model=AppResponse)
async def admin_delete_system_credential(
    request: Request, credential_id: str,
) -> AppResponse:
    """Delete an admin/system credential by id."""
    _require_admin(request)
    store = _get_credential_store(request)
    cred = await store.get_credential_by_id(credential_id)
    if cred is None:
        raise HTTPException(status_code=404, detail="Credential not found")
    if cred.get("owner_type") != OwnerType.SYSTEM:
        raise HTTPException(
            status_code=403,
            detail="Not a system credential - use /api/credentials/{id} instead",
        )
    ok = await store.delete_credential_by_id(credential_id)
    return AppResponse(success=True, data={"deleted": ok})


# ════════════════════════════════════════════════════════════════════
# Legacy helpers below - kept untouched
# ════════════════════════════════════════════════════════════════════


def _build_handler_ctx(request: Request) -> Any:
    """Build the opaque context dict handlers receive in lifecycle hooks.

    Currently exposes ``mcp_pool`` if the MCP module is running on
    the daemon. Handlers dig into this duck-typed ctx; they're
    defensive about missing attributes so a minimal ctx is fine.
    """

    class _HandlerCtx:
        pass

    ctx = _HandlerCtx()
    manager = _get_manager(request)
    mcp_mod = None
    try:
        mcp_mod = manager.modules.get("mcp") if hasattr(manager, "modules") else None
    except Exception:
        mcp_mod = None
    ctx.mcp_pool = getattr(mcp_mod, "_pool", None) if mcp_mod else None
    ctx.request = request
    return ctx
