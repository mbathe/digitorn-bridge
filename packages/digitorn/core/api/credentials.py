"""Credentials API - the HTTP surface for the universal credentials system."""

from __future__ import annotations

import asyncio
import logging
import time
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


# Dependencies


def _get_user_id(request: Request) -> str:
    """Pull the authenticated user id off `request.state.user_id`."""
    return getattr(request.state, "user_id", None) or "anonymous"


def _get_credential_store(request: Request) -> CredentialStore:
    store: CredentialStore | None = getattr(
        request.app.state, "credential_store", None,
    )
    if store is None:
        raise HTTPException(
            status_code=503,
            detail="Credential store not initialized",
        )
    return store


async def _record_user_audit(
    request: Request,
    *,
    action: "AuditActionT",
    on: str,
    outcome: "AuditOutcomeT" = None,
    reason: str = "",
    extra: dict[str, Any] | None = None,
) -> None:
    """Append an audit row for a user-initiated credential operation."""
    audit = getattr(request.app.state, "credential_audit", None)
    if audit is None:
        return
    try:
        from digitorn.core.credentials.audit import AuditOutcome
        from digitorn.core.credentials.audit.log import make_record
        rec = make_record(
            who=_get_user_id(request),
            action=action,
            on=on,
            outcome=outcome or AuditOutcome.SUCCESS,
            reason=reason,
            where_ip=(request.client.host if request.client else "") or "",
            where_ua=request.headers.get("user-agent", "")[:255],
            extra=extra or {},
        )
        await audit.record(rec)
    except Exception as exc:
        logger.warning(
            "credential_audit_user_record_failed action=%s on=%s exc=%r",
            getattr(action, "value", action), on, exc,
        )


# Forward-declare for type hints without forcing the heavy imports at
# module load. The audit module is imported lazily inside helpers.
AuditActionT = Any
AuditOutcomeT = Any


def _get_app_credentials_schema(
    request: Request, app_id: str,
) -> tuple[dict[str, Any] | None, Any]:
    """Return `(schema_dict, compiled_app)` for a deployed app."""
    manager = _get_manager(request)
    deployed = manager.get(app_id)
    if deployed is None:
        raise HTTPException(
            status_code=404, detail=f"App '{app_id}' not deployed",
        )
    schema = getattr(deployed.compiled.execution, "credentials_schema", None)
    return schema, deployed


# Request bodies


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


class UserCredentialCreateRequest(BaseModel):
    """POST /api/credentials body for a user-owned credential."""

    provider_name: str = Field(..., description="Provider identifier (e.g. 'deepseek', 'notion').")
    provider_type: str = Field(default="api_key", description="Handler type: api_key, oauth2, mcp_server, ...")
    label: str = Field(default="default", description="Human-readable label for the picker UI.")
    fields: dict[str, Any] = Field(..., description="Plaintext fields to encrypt.")
    name: str | None = Field(
        default=None,
        description=(
            "Stable slug used by the new declarative `credential:` "
            "block in app YAMLs. Defaults to provider_name when "
            "omitted. Required for the new system to resolve refs."
        ),
    )
    scope: str | None = Field(
        default=None,
        description=(
            "Resolution scope: per_user (default), "
            "per_app_per_user, per_app_shared, system_wide."
        ),
    )


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


# Routes


@router.get("/apps/{app_id}/credentials/manifest", response_model=AppResponse)
async def get_app_credentials_manifest(
    request: Request, app_id: str,
) -> AppResponse:
    """Return the unified credentials manifest for an app."""
    from digitorn.core.credentials.compile_credentials import (
        validate_app_credentials,
    )

    manager = _get_manager(request)
    deployed = manager.get(app_id)
    if deployed is None:
        raise HTTPException(
            status_code=404, detail=f"App '{app_id}' not deployed",
        )

    from digitorn.core.credentials.slot import collect_slots_from_modules
    modules_list: list[Any] = []
    for mid, mod in (getattr(deployed, "modules", None) or {}).items():
        if mod is not None:
            # Stamp module_id on the instance for collect_slots.
            try:
                setattr(mod, "module_id", mid)
            except Exception as exc:
                logger.debug("credentials best-effort block failed: %s", exc)
            modules_list.append(mod)
    slot_pairs = collect_slots_from_modules(modules_list)
    module_slots: dict[str, list[Any]] = {}
    for mid, slot in slot_pairs:
        module_slots.setdefault(mid, []).append(slot)

    app_def = (
        getattr(deployed, "definition", None)
        or getattr(deployed, "app_def", None)
        or getattr(deployed, "compiled", None)
    )
    if app_def is None:
        return AppResponse(
            success=True,
            data={
                "app_id": app_id,
                "user_id": _get_user_id(request),
                "entries": [],
                "all_required_resolved": True,
                "missing_required": [],
            },
        )
    try:
        manifest = validate_app_credentials(
            app_def, module_slots=module_slots,
        )
    except Exception as exc:
        raise HTTPException(
            status_code=400,
            detail=f"credential manifest compile failed: {exc}",
        ) from exc

    store = _get_credential_store(request)
    user_id = _get_user_id(request)
    manifest.user_id = user_id

    async def _resolve_one(entry: Any) -> None:
        row_coro = (
            store.get_credential_by_name(
                name=entry.ref,
                scope=entry.ref_scope,
                user_id=user_id,
                app_id=app_id,
            )
            if entry.ref and entry.ref_scope
            else None
        )
        alts_coro = store.list_credentials(
            user_id=user_id,
            provider_types=entry.handler_types or None,
            provider_names=entry.providers_accepted or None,
        )
        if row_coro is not None:
            row_res, alts_res = await asyncio.gather(
                row_coro, alts_coro, return_exceptions=True,
            )
        else:
            row_res = None
            alts_res = await asyncio.gather(
                alts_coro, return_exceptions=True,
            )
            alts_res = alts_res[0]
        if isinstance(row_res, Exception):
            entry.resolution_error = str(row_res)
        elif row_res is not None:
            entry.resolved = True
            entry.resolved_credential_id = row_res.get("id")
            entry.resolved_status = row_res.get("status")
        if isinstance(alts_res, Exception) or alts_res is None:
            entry.available = []
        else:
            entry.available = [
                {
                    "id": c.get("id"),
                    "name": c.get("name") or c.get("provider_name"),
                    "provider_name": c.get("provider_name"),
                    "scope": c.get("scope"),
                    "status": c.get("status"),
                    "preview": (c.get("display_metadata") or {}).get("masked_fields"),
                }
                for c in alts_res
            ]

    await asyncio.gather(*(_resolve_one(e) for e in manifest.entries))

    return AppResponse(success=True, data=manifest.model_dump())


@router.get("/apps/{app_id}/credentials/schema", response_model=AppResponse)
async def get_app_credentials_schema(
    request: Request, app_id: str,
) -> AppResponse:
    """Return the app's declared credentials schema + current fill state."""
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
    """Return one credential's metadata for the authenticated user."""
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
    """Create or overwrite a credential for the authenticated user + app."""
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
    """List every credential owned by the authenticated user."""
    user_id = _get_user_id(request)
    store = _get_credential_store(request)
    creds = await store.list_credentials(user_id=user_id)
    return AppResponse(
        success=True,
        data={"credentials": creds, "count": len(creds)},
    )


@router.post(
    "/users/me/credentials/{app_id}/{provider_name}/oauth/start",
    response_model=AppResponse,
)
async def oauth_start(
    request: Request, app_id: str, provider_name: str,
) -> AppResponse:
    """Begin the OAuth flow - returns `auth_url` and a `state` uuid."""
    from digitorn.core.credentials.oauth_flow import (
        build_auth_url,
        get_default_flow_store,
    )
    from digitorn.core.credentials.oauth_providers import (
        DEFAULT_REDIRECT_URI,
        OAuthProviderConfig,
        get_default_registry,
    )

    user_id = _get_user_id(request)

    try:
        body = await request.json()
    except Exception:
        body = None
    inline_cfg: dict[str, Any] = {}
    if isinstance(body, dict) and isinstance(body.get("oauth_config"), dict):
        inline_cfg = body["oauth_config"]

    schema_provider: dict[str, Any] = {}
    if app_id != "*":
        schema, _ = _get_app_credentials_schema(request, app_id)
        if schema is not None:
            for p in (schema.get("providers") or []):
                if p.get("name") == provider_name:
                    schema_provider = p
                    break

    oauth_provider_name = schema_provider.get("oauth_provider") or provider_name
    scopes = schema_provider.get("oauth_scopes")
    if not scopes and inline_cfg.get("scopes"):
        raw = inline_cfg["scopes"]
        if isinstance(raw, str):
            scopes = [s for s in raw.replace(",", " ").split() if s]
        elif isinstance(raw, list):
            scopes = [str(s) for s in raw if s]

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
    if provider is None and inline_cfg:
        auth_url_in = str(inline_cfg.get("auth_url") or "").strip()
        token_url_in = str(inline_cfg.get("token_url") or "").strip()
        client_id_in = str(inline_cfg.get("client_id") or "").strip()
        client_secret_in = str(inline_cfg.get("client_secret") or "").strip()
        extra_params_in = inline_cfg.get("extra_auth_params") or {}
        if not isinstance(extra_params_in, dict):
            extra_params_in = {}
        revoke_url_in = str(inline_cfg.get("revoke_url") or "").strip()
        if auth_url_in and token_url_in and client_id_in and client_secret_in:
            provider = OAuthProviderConfig(
                name=oauth_provider_name,
                auth_url=auth_url_in,
                token_url=token_url_in,
                client_id=client_id_in,
                client_secret=client_secret_in,
                default_scopes=scopes or [],
                redirect_uri=str(
                    inline_cfg.get("redirect_uri") or DEFAULT_REDIRECT_URI
                ),
                auth_style=str(inline_cfg.get("auth_style") or "post"),
                extra_auth_params={str(k): str(v) for k, v in extra_params_in.items()},
                revoke_url=revoke_url_in,
            )
    if provider is not None and not provider.is_configured() and inline_cfg:
        client_id_in = str(inline_cfg.get("client_id") or "").strip()
        client_secret_in = str(inline_cfg.get("client_secret") or "").strip()
        if client_id_in and client_secret_in:
            provider = OAuthProviderConfig(
                name=provider.name,
                auth_url=provider.auth_url,
                token_url=provider.token_url,
                client_id=client_id_in,
                client_secret=client_secret_in,
                default_scopes=provider.default_scopes,
                redirect_uri=provider.redirect_uri,
                auth_style=provider.auth_style,
                extra_auth_params=dict(provider.extra_auth_params),
                revoke_url=provider.revoke_url,
            )
    if provider is None:
        raise HTTPException(
            status_code=404,
            detail=(
                f"OAuth provider {oauth_provider_name!r} is not registered "
                f"in ~/.digitorn/oauth_providers.toml. Configured providers: "
                f"{registry.list_configured()}. Pass `oauth_config` inline "
                f"in the request body for ad-hoc custom OAuth2 flows."
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

    if isinstance(body, dict):
        tgt_name = body.get("target_name")
        if isinstance(tgt_name, str) and tgt_name.strip():
            flow.target_name = tgt_name.strip()
        tgt_scope = body.get("target_scope")
        if isinstance(tgt_scope, str) and tgt_scope.strip():
            flow.target_scope = tgt_scope.strip()

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
    """Poll the state of an in-progress OAuth flow."""
    if not state:
        raise HTTPException(status_code=400, detail="state query param required")

    from digitorn.core.credentials.oauth_flow import get_default_flow_store

    flow_store = get_default_flow_store()
    flow = await flow_store.get(state)
    if flow is None:
        return AppResponse(success=True, data={"status": "expired"})

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
    """Manually trigger a token refresh for an existing OAuth credential."""
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


oauth_callback_router = APIRouter(prefix="/api/oauth", tags=["oauth"])


@oauth_callback_router.get("/callback")
async def oauth_callback(
    request: Request,
    code: str = "",
    state: str = "",
    error: str = "",
    error_description: str = "",
):
    """Provider → daemon redirect target. Completes the OAuth flow."""
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
    """Tiny self-contained HTML page shown in the OAuth popup/tab."""
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


# MCP server control - stub delegating to the module's pool


@router.post(
    "/users/me/credentials/{app_id}/{provider_name}/mcp/start",
    response_model=AppResponse,
)
async def mcp_start(
    request: Request, app_id: str, provider_name: str,
) -> AppResponse:
    """Start (or restart) an MCP server from a filled credential."""
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
    """Stop an MCP server without deleting its credential."""
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
    """Return the current runtime status of an MCP server."""
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


# Helpers


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


class CopilotDeviceStartRequest(BaseModel):
    """POST /credentials/copilot/device/start body."""

    target_name: str | None = None
    target_scope: str | None = None


@router.post("/credentials/copilot/device/start", response_model=AppResponse)
async def copilot_device_start(
    request: Request, body: CopilotDeviceStartRequest,
) -> AppResponse:
    """Initiate a GitHub Copilot OAuth device flow."""
    from digitorn.core.credentials.copilot_device_flow import (
        get_default_store as _get_copilot_store,
    )
    user_id = _get_user_id(request)
    store = _get_copilot_store()
    try:
        flow = await store.start(
            user_id=user_id,
            target_name=body.target_name or "github_copilot",
            target_scope=body.target_scope or "per_user",
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    return AppResponse(success=True, data=flow.public_dict())


@router.get(
    "/credentials/copilot/device/status",
    response_model=AppResponse,
)
async def copilot_device_status(
    request: Request, state: str,
) -> AppResponse:
    """Poll the device flow identified by `state`."""
    from digitorn.core.credentials.copilot_device_flow import (
        get_default_store as _get_copilot_store,
    )
    user_id = _get_user_id(request)
    store = _get_copilot_store()
    flow = await store.get(state)
    if flow is None:
        raise HTTPException(
            status_code=404,
            detail="Unknown device-flow state. The flow may have expired "
                   "after 1 hour - call /copilot/device/start again.",
        )
    if flow.user_id != user_id:
        raise HTTPException(
            status_code=403,
            detail="This device-flow belongs to a different user.",
        )

    cred_store = _get_credential_store(request)

    async def _persist(flow_obj: Any, access_token: str) -> str:
        existing = await cred_store.list_user_credentials(
            user_id=user_id, provider_name="github_copilot",
        )
        match = next(
            (c for c in existing
             if (c.get("name") or "") == flow_obj.target_name
             and (c.get("scope") or "per_user") == flow_obj.target_scope),
            None,
        )
        fields = {"api_key": access_token}
        masked = (
            access_token[:6] + "…" + access_token[-4:]
            if len(access_token) > 10 else "ghu_…"
        )
        upserted = await cred_store.upsert_user_credential(
            user_id=user_id,
            credential_id=(match.get("id") if match else None),
            name=flow_obj.target_name,
            scope=flow_obj.target_scope,
            provider_name="github_copilot",
            provider_type="api_key",
            label=flow_obj.target_name,
            fields=fields,
            status="filled",
            display_metadata={
                "masked_fields": {"api_key": masked},
                "auth_method": "device_flow",
            },
        )
        return upserted["id"] if isinstance(upserted, dict) else str(upserted)

    flow = await store.poll(state, on_success=_persist)
    return AppResponse(
        success=True,
        data={
            "status": flow.status,
            "credential_id": flow.credential_id,
            "error": flow.error,
            "user_code": flow.user_code,
            "verification_uri": flow.verification_uri,
            "expires_in": max(0, int(flow.expires_at - time.time())),
            "interval": flow.interval,
        },
    )


@router.get(
    "/credentials/copilot/models",
    response_model=AppResponse,
)
async def copilot_models(
    request: Request, credential_id: str | None = None,
) -> AppResponse:
    """List the LLM models the user's Copilot subscription gives access to."""
    user_id = _get_user_id(request)
    cred_store = _get_credential_store(request)

    # Pick the credential.
    creds = await cred_store.list_user_credentials(
        user_id=user_id, provider_name="github_copilot",
    )
    if not creds:
        raise HTTPException(
            status_code=404,
            detail="No github_copilot credential found in your vault. "
                   "Run the device flow first via "
                   "POST /credentials/copilot/device/start.",
        )
    if credential_id:
        cred = next((c for c in creds if c.get("id") == credential_id), None)
        if cred is None:
            raise HTTPException(
                status_code=404,
                detail=f"credential_id {credential_id!r} not found among "
                       f"your github_copilot credentials.",
            )
    else:
        cred = creds[0]

    # Decrypt to get the api_key (GitHub OAuth token).
    full = await cred_store.get_credential_by_id(
        credential_id=cred["id"], decrypt=True,
    )
    if full is None or not full.get("fields", {}).get("api_key"):
        raise HTTPException(
            status_code=409,
            detail="Credential has no api_key field, cannot list models.",
        )
    gh_token = str(full["fields"]["api_key"])

    from digitorn.modules.llm_provider.providers.github_copilot import (
        GitHubCopilotProvider,
    )
    probe = GitHubCopilotProvider(
        provider_id="copilot_models_probe",
        model="gpt-4o",
        api_key=gh_token,
    )
    try:
        models = await probe.list_models()
    except Exception as exc:
        logger.warning(
            "copilot_models_probe_failed credential_id=%s: %s",
            cred["id"], exc,
        )
        raise HTTPException(
            status_code=502,
            detail=f"Could not reach api.githubcopilot.com: {type(exc).__name__}: {exc}",
        )

    # Normalize - the daemon returns whatever GitHub serves. We keep
    # only the chat-capable ids and surface a few useful fields.
    out: list[dict[str, Any]] = []
    for m in models:
        if not isinstance(m, dict):
            continue
        mid = m.get("id") or m.get("name") or ""
        if not mid:
            continue
        capabilities = m.get("capabilities", {}) if isinstance(
            m.get("capabilities"), dict) else {}
        # Only surface chat models by default - completion-only models
        # are not what an agent will use.
        cap_type = capabilities.get("type") if isinstance(
            capabilities, dict) else None
        if cap_type and cap_type not in ("chat",):
            continue
        out.append({
            "id": mid,
            "name": m.get("name") or mid,
            "vendor": m.get("vendor") or "",
            "version": m.get("version") or "",
            "preview": bool(m.get("preview", False)),
            "context_window": (
                capabilities.get("limits", {}).get("max_context_window_tokens")
                if isinstance(capabilities.get("limits"), dict) else None
            ),
            "max_output_tokens": (
                capabilities.get("limits", {}).get("max_output_tokens")
                if isinstance(capabilities.get("limits"), dict) else None
            ),
            "supports_tool_calls": bool(
                capabilities.get("supports", {}).get("tool_calls")
                if isinstance(capabilities.get("supports"), dict) else False
            ),
            "supports_vision": bool(
                capabilities.get("supports", {}).get("vision")
                if isinstance(capabilities.get("supports"), dict) else False
            ),
        })

    out.sort(key=lambda x: (x.get("vendor", ""), x.get("id", "")))
    return AppResponse(
        success=True,
        data={
            "credential_id": cred["id"],
            "models": out,
            "count": len(out),
        },
    )


@router.get("/credentials/providers", response_model=AppResponse)
async def list_credential_providers(request: Request) -> AppResponse:
    """Return the catalog of well-known credential providers."""
    # Combine: new TOML catalog (preferred) + legacy static entries
    # for any provider id not yet covered by a TOML template.
    try:
        from digitorn.core.credentials.catalog import default_catalog
    except Exception:
        default_catalog = None  # type: ignore

    merged: dict[str, dict[str, Any]] = {}
    # Legacy entries first (lowest precedence).
    for legacy in _PROVIDER_CATALOG:
        pid = str(legacy.get("id", ""))
        if pid:
            merged[pid] = dict(legacy)

    # TOML templates override (with handler defaults merged).
    if default_catalog is not None:
        try:
            from dataclasses import asdict, is_dataclass
            from digitorn.core.credentials.handler import default_registry
            for tpl in default_catalog.list_all():
                pid = tpl.name
                # Pull handler defaults to merge field specs.
                try:
                    handler = default_registry.get(tpl.handler_type)
                    handler_defaults = handler.schema_fields()
                    fields = tpl.effective_fields(handler_defaults)
                except Exception:
                    fields = []
                merged[pid] = {
                    "id": pid,
                    "display_name": tpl.display_name or pid,
                    "category": tpl.category or "general",
                    "type": tpl.handler_type,
                    "icon": tpl.icon or pid,
                    "fields": [
                        f.to_dict() if hasattr(f, "to_dict")
                        else (asdict(f) if is_dataclass(f) else dict(f))
                        for f in fields
                    ],
                    "verify": (asdict(tpl.verify) if tpl.verify is not None else None),
                    "oauth":  (asdict(tpl.oauth)  if tpl.oauth  is not None else None),
                    "docs_url": tpl.homepage or "",
                    "description": tpl.description or "",
                }
        except Exception as exc:
            # Soft-fail: legacy list still serves.
            logger.debug("provider_catalog_toml_merge_failed: %s", exc)

    providers = list(merged.values())
    return AppResponse(
        success=True,
        data={
            "providers": providers,
            "count": len(providers),
        },
    )


@router.get("/credentials", response_model=AppResponse)
async def list_my_credentials(
    request: Request, provider: str = "",
) -> AppResponse:
    """List every credential owned by the authenticated user."""
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
    """Create a new user-owned credential."""
    from digitorn.core.credentials.audit import AuditAction, AuditOutcome

    store = _get_credential_store(request)
    user_id = _get_user_id(request)

    handler = default_registry.get(body.provider_type)
    try:
        handler.validate_fields(body.fields, [])
    except ValidationError as exc:
        await _record_user_audit(
            request, action=AuditAction.CREATE, on="*",
            outcome=AuditOutcome.FAILURE,
            reason=f"validation_failed: {exc}",
            extra={
                "provider_name": body.provider_name,
                "provider_type": body.provider_type,
            },
        )
        raise HTTPException(status_code=400, detail=str(exc))

    stored = await store.upsert_user_credential(
        user_id=user_id,
        provider_name=body.provider_name,
        provider_type=body.provider_type,
        label=body.label,
        fields=body.fields,
        status=Status.FILLED,
        name=body.name,
        scope=body.scope,
    )
    await _record_user_audit(
        request, action=AuditAction.CREATE,
        on=stored.get("id", "*"),
        extra={
            "provider_name": body.provider_name,
            "provider_type": body.provider_type,
            "scope": stored.get("scope"),
            "name": stored.get("name"),
        },
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
    from digitorn.core.credentials.audit import AuditAction
    await _record_user_audit(
        request,
        action=(
            AuditAction.UPDATE_FIELDS if body.fields is not None
            else AuditAction.UPDATE_METADATA
        ),
        on=credential_id,
        extra={
            "fields_changed": body.fields is not None,
            "label_changed": body.label is not None,
            "provider_name": cred.get("provider_name"),
        },
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
    from digitorn.core.credentials.audit import AuditAction, AuditOutcome
    await _record_user_audit(
        request,
        action=AuditAction.DELETE,
        on=credential_id,
        outcome=AuditOutcome.SUCCESS if ok else AuditOutcome.FAILURE,
        extra={
            "provider_name": cred.get("provider_name"),
            "scope": cred.get("scope"),
            "name": cred.get("name"),
        },
    )
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


@router.post("/credentials/{credential_id}/refresh", response_model=AppResponse)
async def refresh_credential_by_id(
    request: Request, credential_id: str,
) -> AppResponse:
    """Refresh a credential by id, regardless of provider type."""
    from digitorn.core.credentials import default_registry as handler_registry

    store = _get_credential_store(request)
    user_id = _get_user_id(request)

    cred = await store.get_credential_by_id(credential_id, decrypt=True)
    if cred is None:
        raise HTTPException(status_code=404, detail="Credential not found")
    if cred.get("user_id") != user_id and cred.get("owner_type") != OwnerType.SYSTEM:
        raise HTTPException(
            status_code=403,
            detail="You can only refresh your own credentials",
        )

    handler_type = cred.get("provider_type") or "api_key"
    handler = handler_registry.get(handler_type)

    schema_provider: dict[str, Any] = {}
    provider_name = cred.get("provider_name") or ""
    if provider_name:
        try:
            from dataclasses import asdict, is_dataclass
            from digitorn.core.credentials.catalog import default_catalog
            tpl = default_catalog.get(provider_name)
            if tpl is not None:
                if tpl.verify is not None:
                    v = asdict(tpl.verify) if is_dataclass(tpl.verify) else dict(tpl.verify)
                    auth = v.get("auth_template") or v.get("auth_header") or ""
                    if auth and "{{field." not in auth:
                        import re as _re
                        auth = _re.sub(r"\{([a-zA-Z0-9_]+)\}", r"{{field.\1}}", auth)
                    success = v.get("success_codes") or [200]
                    schema_provider["test"] = {
                        "url": v.get("endpoint") or v.get("url") or "",
                        "method": (v.get("method") or "GET").upper(),
                        "auth_header": auth,
                        "expected_status": success[0] if success else 200,
                    }
                if tpl.oauth is not None:
                    o = asdict(tpl.oauth) if is_dataclass(tpl.oauth) else dict(tpl.oauth)
                    schema_provider["oauth_provider"] = provider_name
                    schema_provider["oauth_token_url"] = o.get("token_url") or ""
                    schema_provider["oauth_scopes"] = o.get("scopes") or []
        except Exception as exc:
            logger.debug("refresh: catalogue lookup failed: %s", exc)

    try:
        refreshed = await handler.refresh(cred, schema_provider)
    except Exception as exc:
        logger.exception("credential refresh failed: id=%s", credential_id)
        raise HTTPException(
            status_code=500, detail=f"refresh failed: {exc}",
        )

    # Persist the refreshed fields/status/expiry.
    fields = refreshed.get("fields") if isinstance(refreshed, dict) else None
    status = refreshed.get("status") if isinstance(refreshed, dict) else None
    expires_at = refreshed.get("expires_at") if isinstance(refreshed, dict) else None
    try:
        stored = await store.upsert_user_credential(
            user_id=user_id,
            provider_name=provider_name,
            provider_type=handler_type,
            label=cred.get("label") or "default",
            fields=fields if isinstance(fields, dict) else (cred.get("fields") or {}),
            status=status or cred.get("status") or Status.VALID,
            expires_at=expires_at,
            display_metadata=cred.get("display_metadata") or {},
            name=cred.get("name") or provider_name,
            scope=cred.get("scope"),
        )
    except Exception as exc:
        logger.exception("credential refresh upsert failed: id=%s", credential_id)
        raise HTTPException(
            status_code=500, detail=f"refresh persist failed: {exc}",
        )

    return AppResponse(success=True, data=stored)


@router.post("/credentials/test", response_model=AppResponse)
async def test_credential_fields(
    request: Request,
    body: UserCredentialCreateRequest,
) -> AppResponse:
    """Run the provider's `verify` recipe against the supplied"""
    import time as _time
    from dataclasses import asdict, is_dataclass
    from digitorn.core.credentials.handler import default_registry
    try:
        from digitorn.core.credentials.catalog import default_catalog
    except Exception:
        default_catalog = None  # type: ignore

    handler_type = (body.provider_type or "").strip()
    if not handler_type:
        return AppResponse(
            success=False,
            error="provider_type is required",
            data={"ok": False, "detail": "provider_type is required"},
        )
    try:
        handler = default_registry.get(handler_type)
    except Exception as exc:
        return AppResponse(
            success=False,
            error=f"unknown handler_type: {handler_type}",
            data={"ok": False, "detail": str(exc)},
        )

    schema_provider: dict[str, Any] = {}
    if default_catalog is not None and body.provider_name:
        try:
            tpl = default_catalog.get(body.provider_name)
        except Exception:
            tpl = None
        if tpl is not None and tpl.verify is not None:
            v = asdict(tpl.verify) if is_dataclass(tpl.verify) else dict(tpl.verify)
            endpoint = v.get("endpoint") or v.get("url") or ""
            auth = v.get("auth_template") or v.get("auth_header") or ""
            # The handler reads `{{field.X}}` tokens; the catalogue
            # recipe uses `{X}` shorthand. Normalise so both work.
            if auth and "{{field." not in auth:
                import re as _re
                auth = _re.sub(r"\{([a-zA-Z0-9_]+)\}", r"{{field.\1}}", auth)
            success_codes = v.get("success_codes") or [200]
            schema_provider = {
                "test": {
                    "url": endpoint,
                    "method": (v.get("method") or "GET").upper(),
                    "auth_header": auth,
                    "expected_status": success_codes[0]
                        if success_codes else 200,
                },
            }

    started = _time.monotonic()
    try:
        ok, err = await handler.test_live_connection(
            body.fields or {}, schema_provider,
        )
    except Exception as exc:
        elapsed_ms = int((_time.monotonic() - started) * 1000)
        return AppResponse(
            success=True,
            data={
                "ok": False,
                "detail": f"{type(exc).__name__}: {exc}",
                "latency_ms": elapsed_ms,
            },
        )
    elapsed_ms = int((_time.monotonic() - started) * 1000)
    return AppResponse(
        success=True,
        data={
            "ok": bool(ok),
            "detail": err or ("Connected" if ok else "Failed"),
            "latency_ms": elapsed_ms,
        },
    )


@router.post("/credentials/{credential_id}/grant", response_model=AppResponse)
async def create_credential_grant_singular(
    request: Request, credential_id: str, body: CreateGrantRequest,
) -> AppResponse:
    """Alias for `POST /credentials/{id}/grants` - singular form."""
    return await create_credential_grant(request, credential_id, body)


@router.post("/credentials/{credential_id}/grants", response_model=AppResponse)
async def create_credential_grant(
    request: Request, credential_id: str, body: CreateGrantRequest,
) -> AppResponse:
    """Authorize an app to use this credential."""
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
    """Revoke an app's access to a credential."""
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
    """List every active grant of the authenticated user."""
    store = _get_credential_store(request)
    user_id = _get_user_id(request)
    grants = await store.list_grants(user_id=user_id)
    return AppResponse(
        success=True,
        data={"grants": grants, "count": len(grants)},
    )


def _require_admin(request: Request) -> None:
    """Block non-admin callers from touching system credentials."""
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
    """Create a system-owned credential."""
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


@router.get("/admin/credentials/audit", response_model=AppResponse)
async def admin_credential_audit_list(
    request: Request,
    credential_id: str = "",
    user_id: str = "",
    limit: int = 200,
) -> AppResponse:
    """Return recent credential audit events."""
    _require_admin(request)
    audit = getattr(request.app.state, "credential_audit", None)
    if audit is None:
        return AppResponse(
            success=True,
            data={"events": [], "count": 0, "audit_unavailable": True},
        )

    try:
        if credential_id:
            records = await audit.list_for_credential(
                credential_id, limit=limit,
            )
        elif user_id:
            records = await audit.list_for_user(user_id, limit=limit)
        else:
            # Generic recent: try list_recent if available, else
            # fall back to a wildcard list_for_user("").
            recent_fn = getattr(audit, "list_recent", None)
            if callable(recent_fn):
                records = await recent_fn(limit=limit)
            else:
                records = []
    except Exception as exc:
        logger.warning("credential_audit_list_failed: %s", exc)
        records = []

    events = []
    for rec in records:
        # AuditRecord is a dataclass-ish object; expose a JSON
        # projection with no plaintext.
        events.append({
            "ts":          getattr(rec, "ts", None),
            "who":         getattr(rec, "who", ""),
            "action":      str(getattr(rec, "action", "")),
            "outcome":     str(getattr(rec, "outcome", "")),
            "target":      getattr(rec, "target", ""),
            "reason":      getattr(rec, "reason", ""),
            "app_id":      getattr(rec, "app_id", "") or "",
            "extra":       getattr(rec, "extra", None) or {},
        })

    return AppResponse(
        success=True,
        data={"events": events, "count": len(events)},
    )


@router.get("/credentials-health", response_model=AppResponse)
async def credentials_subsystem_health(request: Request) -> AppResponse:
    """Report the health of the credential subsystem."""
    out: dict[str, Any] = {}

    # KMS provider
    kms = getattr(request.app.state, "kms_provider", None)
    if kms is None:
        out["master_key"] = {"ok": False, "reason": "not_initialized"}
    else:
        try:
            ok = await kms.healthcheck()
            out["master_key"] = {
                "ok": bool(ok),
                "backend": getattr(getattr(kms, "backend", None), "value", "?"),
            }
        except Exception as exc:
            out["master_key"] = {"ok": False, "reason": str(exc)[:200]}

    # Cipher round-trip
    store = getattr(request.app.state, "credential_store", None)
    if store is None:
        out["cipher"] = {"ok": False, "reason": "store_unavailable"}
    else:
        try:
            cipher = getattr(store, "_cipher", None)
            if cipher is None:
                out["cipher"] = {"ok": False, "reason": "no_cipher"}
            else:
                test_dict = {"k": "v"}
                ct, nonce = cipher.encrypt(test_dict)
                roundtrip = cipher.decrypt(ct, nonce)
                out["cipher"] = {
                    "ok": roundtrip == test_dict,
                }
        except Exception as exc:
            out["cipher"] = {"ok": False, "reason": str(exc)[:200]}

    # Audit log
    audit = getattr(request.app.state, "credential_audit", None)
    if audit is None:
        out["audit"] = {"ok": False, "reason": "not_initialized"}
    else:
        out["audit"] = {"ok": True}

    # OAuth registry + refresh loop
    oauth_reg = getattr(request.app.state, "oauth_registry", None)
    if oauth_reg is None:
        out["oauth_registry"] = {"ok": False, "reason": "not_initialized"}
    else:
        out["oauth_registry"] = {
            "ok": True,
            "configured": oauth_reg.list_configured(),
            "all": oauth_reg.list_all(),
        }
    refresh = getattr(request.app.state, "oauth_refresh_loop", None)
    if refresh is None:
        out["oauth_refresh_loop"] = {"ok": False, "reason": "not_initialized"}
    else:
        out["oauth_refresh_loop"] = {
            "ok": refresh._task is not None and not refresh._task.done(),
        }

    overall_ok = all(
        bool(v.get("ok"))
        for v in out.values() if isinstance(v, dict)
    )
    return AppResponse(success=True, data={"healthy": overall_ok, "components": out})


@router.post("/admin/credentials/audit/verify", response_model=AppResponse)
async def admin_credential_audit_verify(request: Request) -> AppResponse:
    """Walk the credential audit hash chain from genesis and report"""
    _require_admin(request)
    audit = getattr(request.app.state, "credential_audit", None)
    if audit is None:
        return AppResponse(
            success=True,
            data={"intact": None, "audit_unavailable": True},
        )
    try:
        ok, broken_at = await audit.verify_chain()
    except Exception as exc:
        logger.warning("credential_audit_verify_failed: %s", exc)
        return AppResponse(
            success=False, error=str(exc),
            data={"intact": False, "broken_at": str(exc)},
        )
    return AppResponse(
        success=True,
        data={"intact": ok, "broken_at": broken_at},
    )


# Legacy helpers below - kept untouched


def _build_handler_ctx(request: Request) -> Any:
    """Build the opaque context dict handlers receive in lifecycle hooks."""

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
