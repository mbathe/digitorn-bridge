"""In-memory configuration cache for the LLM dispatch hot path.

The dispatch path resolves ``(model_alias) -> (provider, real_model_id,
decrypted_api_key)`` thousands of times per second. Hitting Postgres
or AES-GCM on every call would dominate latency. So:

  * Boot loads everything from the DB once.
  * Decryption happens once per credential, NOT per request.
  * Dispatch reads pure Python dicts - sub-microsecond.
  * CRUD endpoints write THROUGH the cache: the DB commits first,
    then we mutate the in-memory dicts so the change is live in
    the same process tick.
  * A background coroutine reloads the cache every
    ``CONFIG_CACHE_REFRESH_S`` seconds (default 30 s) so a write
    that bypassed our routes (direct SQL, another instance) is
    eventually picked up.

The lookup APIs return frozen dataclasses by design: no caller can
accidentally mutate the cache. Atomic swaps use whole-dict
replacement (Python dict assignment is GIL-atomic).

Threat model: we keep PLAINTEXT API keys in the process heap. The
gateway's master key is also there, so this is a wash - any code
running in the gateway can already decrypt every credential. Use
container isolation + tight network controls; do NOT expose this
process to untrusted code paths.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from digitorn_gateway.cipher import GatewayCipherError, decrypt_dict
from digitorn_gateway.models_db import (
    GatewayCredential,
    GatewayModel,
    GatewayProvider,
    GatewayRoute,
)

logger = logging.getLogger(__name__)


CONFIG_CACHE_REFRESH_S = 30.0


@dataclass(frozen=True, slots=True)
class CachedProvider:
    slug: str
    name: str
    base_url: str | None
    compat: str
    env_var: str | None
    auth_type: str = "api_key"
    # Free-form per-provider config the dashboard owns (no code release
    # to update). Recognised keys today:
    #   ``dispatch_headers`` -- headers merged into every chat request
    #     for this provider (e.g. Copilot's Editor-Version).
    #   ``api_key_url`` -- "Get a key" link surfaced on the credential
    #     form.
    extra_metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class CachedCredential:
    id: uuid.UUID
    provider_slug: str
    label: str
    secret_data: dict[str, str]  # decrypted dict (multi-field auth)
    status: str
    # When True the connection_pool keeps an httpx.AsyncClient warm
    # for this credential. Default True for new credentials; opt-out
    # from the dashboard.
    live_pool: bool = True


@dataclass(frozen=True, slots=True)
class CachedModel:
    alias: str
    provider_slug: str
    real_model_id: str
    cost_per_1k_input: float
    cost_per_1k_output: float
    max_context: int | None
    is_custom: bool


@dataclass(frozen=True, slots=True)
class CachedRoute:
    """One row of the priority-ordered routing list for a model alias.

    Cross-provider routing: every route owns its dispatch identity
    (``provider_slug``, ``real_model_id``, ``compat``, ``base_url``,
    ``dispatch_headers``). The model row stays as a metadata anchor;
    a route can target a DIFFERENT provider than the alias's metadata
    advertises. Resolver reads these fields directly off the route, so
    the alias's primary can be Copilot and the fallback can be
    Anthropic direct without any additional alias plumbing."""

    id: uuid.UUID
    model_alias: str
    credential_id: uuid.UUID
    priority: int
    provider_slug: str
    real_model_id: str
    compat: str
    base_url: str | None = None
    dispatch_headers: dict[str, str] = field(default_factory=dict)


@dataclass(slots=True)
class RouteHealth:
    """Live health snapshot for a route. Pure-memory, lost on restart
    (the prober walks through everything in 60 s after boot)."""

    consecutive_failures: int = 0
    blocked_until: float = 0.0       # monotonic seconds; 0 = healthy
    last_success_at: float = 0.0     # monotonic seconds
    last_error: str = ""


@dataclass(slots=True)
class CredentialHealth:
    """Live load + rate-limit snapshot for a credential.

    Multi-account load balancing relies on this: when several routes
    share the same priority tier, the resolver picks the credential
    with the lowest ``inflight`` count, breaking ties by the lowest
    ``consecutive_429s``. A credential that just hit 429 gets a
    short, dedicated cooldown so the rest of the tier keeps serving.

    ``inflight`` is incremented at dispatch start and decremented in
    a ``finally`` block, so a crashing handler doesn't leak counts.
    """

    inflight: int = 0
    consecutive_429s: int = 0
    blocked_until_429: float = 0.0   # monotonic seconds; 0 = healthy
    last_429_at: float = 0.0
    total_dispatched: int = 0        # cumulative; useful for distribution checks


@dataclass(frozen=True, slots=True)
class ResolvedDispatch:
    """What the dispatch path needs to call LiteLLM. The auth_type +
    secret_data lets the dispatcher inject any credential shape (Bearer,
    custom header, basic, OAuth, claude-code OAuth, ...). The
    pre-computed ``api_key`` / ``extra_headers`` fields are the result
    of running the auth dispatcher; the dispatch path forwards them to
    LiteLLM unchanged."""

    alias: str
    provider_slug: str
    real_model_id: str
    api_key: str | None
    base_url: str | None
    compat: str
    is_custom: bool
    cost_per_1k_input: float
    cost_per_1k_output: float
    max_context: int | None
    auth_type: str = "api_key"
    extra_headers: dict[str, str] = field(default_factory=dict)
    extra_body: dict[str, Any] = field(default_factory=dict)
    # Route id used to mark health on success / failure. ``None`` for
    # the env-var fallback path (no row to track).
    route_id: uuid.UUID | None = None
    # Credential id + live_pool flag let the dispatch hot path look up
    # a warm httpx.AsyncClient in ``connection_pool``. ``None`` for
    # is_custom or env-var-fallback paths (no real credential row).
    credential_id: uuid.UUID | None = None
    live_pool: bool = False


class ConfigCache:
    """Process-wide cache. Single instance shared across all coroutines."""

    def __init__(self) -> None:
        self._providers: dict[str, CachedProvider] = {}
        self._credentials: dict[uuid.UUID, CachedCredential] = {}
        # Provider-level "default" credential picked when no route is set
        # for a model. We use the most recent ``status='active'`` row.
        self._provider_default_cred: dict[str, uuid.UUID] = {}
        self._models: dict[str, CachedModel] = {}
        # alias -> ordered list of CachedRoute (priority asc).
        self._routes: dict[str, list[CachedRoute]] = {}
        # route_id -> health snapshot (mutable, written by dispatch + prober).
        self._route_health: dict[uuid.UUID, RouteHealth] = {}
        # credential_id -> live load + rate-limit health. Multi-account
        # load balancing reads this; dispatch start / finish writes it.
        self._cred_health: dict[uuid.UUID, CredentialHealth] = {}
        self._loaded_at: float = 0.0
        # Pure-async lock: protects whole-dict reloads from interleaving
        # with write-through mutations. Reads are lock-free.
        self._reload_lock = asyncio.Lock()
        # Load-aware spill: when set >0, routes whose credential has
        # inflight>=cap are filtered out during resolution so the
        # dispatch loop falls through to the next tier instead of
        # queueing on a saturated provider. Set from main.py at lifespan
        # start using ``settings.failover_max_inflight_per_credential``.
        self._inflight_cap: int = 0

    # ── Reload (boot + periodic + after-mutation) ────────────────

    async def reload_from_db(
        self, factory: async_sessionmaker[AsyncSession],
    ) -> dict[str, int]:
        """Re-read all 4 tables, decrypt creds, swap dicts atomically."""
        async with self._reload_lock:
            providers: dict[str, CachedProvider] = {}
            credentials: dict[uuid.UUID, CachedCredential] = {}
            provider_default: dict[str, uuid.UUID] = {}
            models: dict[str, CachedModel] = {}
            routes: dict[str, uuid.UUID] = {}

            async with factory() as db:
                # Providers
                p_rows = (
                    await db.execute(
                        select(GatewayProvider).where(
                            GatewayProvider.archived_at.is_(None),
                        )
                    )
                ).scalars().all()
                for p in p_rows:
                    providers[p.slug] = CachedProvider(
                        slug=p.slug,
                        name=p.name,
                        base_url=p.base_url,
                        compat=p.compat,
                        env_var=p.env_var,
                        auth_type=getattr(p, "auth_type", None) or "api_key",
                        extra_metadata=dict(p.extra_metadata or {}),
                    )

                # Credentials (decrypt eagerly)
                c_rows = (
                    await db.execute(
                        select(GatewayCredential).order_by(
                            GatewayCredential.created_at,
                        )
                    )
                ).scalars().all()
                for c in c_rows:
                    try:
                        secret_data = decrypt_dict(c.encrypted_value)
                    except GatewayCipherError as exc:
                        logger.error(
                            "config_cache: skipping unreadable cred id=%s: %s",
                            c.id, exc,
                        )
                        continue
                    credentials[c.id] = CachedCredential(
                        id=c.id,
                        provider_slug=c.provider_slug,
                        label=c.label,
                        secret_data=secret_data,
                        status=c.status,
                        live_pool=getattr(c, "live_pool", True),
                    )
                    if c.status == "active":
                        # Last active wins (= most recent created_at).
                        provider_default[c.provider_slug] = c.id

                # Models
                m_rows = (
                    await db.execute(
                        select(GatewayModel).where(
                            GatewayModel.archived_at.is_(None),
                        )
                    )
                ).scalars().all()
                for m in m_rows:
                    models[m.alias] = CachedModel(
                        alias=m.alias,
                        provider_slug=m.provider_slug,
                        real_model_id=m.real_model_id,
                        cost_per_1k_input=float(m.cost_per_1k_input_tokens),
                        cost_per_1k_output=float(m.cost_per_1k_output_tokens),
                        max_context=m.max_context_tokens,
                        is_custom=m.is_custom,
                    )

                # Routes (priority asc - first one wins on dispatch).
                r_rows = (
                    await db.execute(
                        select(GatewayRoute).order_by(
                            GatewayRoute.model_alias,
                            GatewayRoute.priority,
                        )
                    )
                ).scalars().all()
                for r in r_rows:
                    raw_headers = getattr(r, "dispatch_headers", None) or {}
                    if not isinstance(raw_headers, dict):
                        raw_headers = {}
                    coerced_headers: dict[str, str] = {}
                    for k, v in raw_headers.items():
                        if isinstance(k, str) and v is not None:
                            coerced_headers[k] = str(v)
                    cached = CachedRoute(
                        id=r.id,
                        model_alias=r.model_alias,
                        credential_id=r.credential_id,
                        priority=r.priority,
                        provider_slug=r.provider_slug,
                        real_model_id=r.real_model_id,
                        compat=r.compat,
                        base_url=r.base_url,
                        dispatch_headers=coerced_headers,
                    )
                    routes.setdefault(r.model_alias, []).append(cached)

            # Atomic swap. Plain Python assignment is GIL-atomic, so
            # readers see either the old or the new dict, never a torn
            # intermediate.
            self._providers = providers
            self._credentials = credentials
            self._provider_default_cred = provider_default
            self._models = models
            self._routes = routes
            # Drop health entries for routes that no longer exist.
            live_route_ids = {
                r.id for routes_list in routes.values() for r in routes_list
            }
            self._route_health = {
                rid: h for rid, h in self._route_health.items()
                if rid in live_route_ids
            }
            # Drop credential health for credentials that no longer
            # exist. Preserve the inflight counter for live creds so an
            # in-flight call mid-reload doesn't lose its accounting.
            self._cred_health = {
                cid: h for cid, h in self._cred_health.items()
                if cid in credentials
            }
            self._loaded_at = time.monotonic()

        stats = {
            "providers": len(self._providers),
            "credentials": len(self._credentials),
            "models": len(self._models),
            "routes": len(self._routes),
        }
        logger.info("config_cache_reloaded %s", stats)
        return stats

    def set_inflight_cap(self, cap: int) -> None:
        """Configure the saturation filter. ``0`` disables it (legacy
        behaviour). Set from main.py at lifespan start using
        ``settings.failover_max_inflight_per_credential``."""
        self._inflight_cap = max(0, int(cap or 0))

    # ── Hot-path lookup ──────────────────────────────────────────

    def resolve_dispatch(self, alias: str) -> ResolvedDispatch | None:
        """Sub-microsecond resolution for the dispatch path.

        Returns ``None`` when the alias is unknown OR no credential is
        available (route + provider-default both empty). Caller turns
        ``None`` into a clean 404 ``model_not_provided_by_digitorn``.
        """
        from digitorn_gateway.auth_dispatchers import dispatch_auth as _dispatch

        m = self._models.get(alias)
        if m is None:
            # Daemon-style ``<provider_slug>/<real_model_id>`` synthesis.
            # Some callers (notably the daemon's gateway_resolver) build
            # the gateway model id by prefixing the brain's provider on
            # the way out. Treat that as an implicit alias: if the
            # prefix matches a known provider AND we have a way to
            # dispatch (route on a same-(provider,model) alias OR
            # provider-default credential), build a synthetic
            # ResolvedDispatch on the fly. Critically, this path NEVER
            # falls through to ``litellm.acompletion(model=alias, ...)``
            # which would activate LiteLLM's native github_copilot /
            # vertex_ai connectors and trigger their broken
            # interactive-OAuth side effects.
            m = self._synthesize_model(alias)
            if m is None:
                return None
        provider = self._providers.get(m.provider_slug)
        if provider is None:
            return None

        # Custom providers have their own auth - no key required from
        # the catalogue (the custom_router supplies it).
        if m.is_custom:
            return ResolvedDispatch(
                alias=alias,
                provider_slug=m.provider_slug,
                real_model_id=m.real_model_id,
                api_key=None,
                base_url=provider.base_url,
                compat=provider.compat,
                is_custom=True,
                cost_per_1k_input=m.cost_per_1k_input,
                cost_per_1k_output=m.cost_per_1k_output,
                max_context=m.max_context,
                auth_type=provider.auth_type,
                extra_headers=self._provider_dispatch_headers(provider),
                extra_body={},
            )

        # Walk the priority-ordered routes and pick the first that has
        # a usable credential AND isn't currently in the unhealthy
        # cooldown. Failover happens by retrying through this list at
        # dispatch time; this method just returns the FIRST candidate.
        return self._resolve_route_at(alias, m, provider, route_index=0)

    def _synthesize_model(self, alias: str) -> CachedModel | None:
        """Build an implicit CachedModel from a ``<provider>/<model>``
        string when no explicit alias matches.

        Returns ``None`` when the prefix isn't a known provider or
        when the suffix is empty. Callers MUST treat the returned
        model exactly like a real one (cost_per_1k=0 since we don't
        have catalogue numbers; the dispatch path tolerates that)."""
        if "/" not in alias:
            return None
        prefix, suffix = alias.split("/", 1)
        if not prefix or not suffix:
            return None
        if prefix not in self._providers:
            return None
        # Prefer an existing alias with the same (provider, real_model)
        # so we inherit costs / max_context. Fall back to a synthesised
        # entry with zeros.
        for cached in self._models.values():
            if (cached.provider_slug == prefix
                    and cached.real_model_id == suffix):
                return cached
        return CachedModel(
            alias=alias,
            provider_slug=prefix,
            real_model_id=suffix,
            cost_per_1k_input=0.0,
            cost_per_1k_output=0.0,
            max_context=None,
            is_custom=False,
        )

    def resolve_dispatch_at(
        self, alias: str, route_index: int,
    ) -> ResolvedDispatch | None:
        """Return the (route_index)-th healthy route for an alias, or
        ``None`` when there are no more candidates. Used by the dispatch
        failover loop to walk down priority order."""
        m = self._models.get(alias)
        if m is None:
            m = self._synthesize_model(alias)
            if m is None:
                return None
        provider = self._providers.get(m.provider_slug)
        if provider is None:
            return None
        if m.is_custom:
            return self.resolve_dispatch(alias) if route_index == 0 else None
        return self._resolve_route_at(alias, m, provider, route_index)

    def resolve_dispatch_excluding(
        self,
        alias: str,
        exclude_route_ids: set[uuid.UUID] | frozenset[uuid.UUID],
    ) -> ResolvedDispatch | None:
        """Return the best healthy route NOT in ``exclude_route_ids``.

        This is the failover-aware sibling of ``resolve_dispatch_at``:
        it lets the dispatch loop skip already-tried routes without
        relying on positional indices, which become stale when health
        state shifts mid-loop (e.g. a credential entering 429 cooldown
        between retries).
        """
        m = self._models.get(alias)
        if m is None:
            m = self._synthesize_model(alias)
            if m is None:
                return None
        provider = self._providers.get(m.provider_slug)
        if provider is None:
            return None
        if m.is_custom:
            if exclude_route_ids:
                return None
            return self.resolve_dispatch(alias)
        return self._resolve_route_at(
            alias, m, provider, route_index=0,
            exclude_ids=set(exclude_route_ids),
        )

    def _resolve_route_at(
        self,
        alias: str,
        m: CachedModel,
        provider: CachedProvider,
        route_index: int,
        exclude_ids: set[uuid.UUID] | None = None,
    ) -> ResolvedDispatch | None:
        from digitorn_gateway.auth_dispatchers import dispatch_auth as _dispatch

        now = time.monotonic()
        candidates: list[CachedRoute] = [
            r for r in self._routes.get(alias, [])
            if exclude_ids is None or r.id not in exclude_ids
        ]

        # Synthetic fallback when no explicit route is set: build a
        # virtual one from the provider-default credential. Index 0 only.
        # The synthetic route inherits the alias's metadata identity
        # (model.provider_slug + model.real_model_id + provider.compat).
        if not candidates and route_index == 0:
            cred_id = self._provider_default_cred.get(m.provider_slug)
            if cred_id is not None:
                candidates = [CachedRoute(
                    id=cred_id, model_alias=alias,
                    credential_id=cred_id, priority=0,
                    provider_slug=m.provider_slug,
                    real_model_id=m.real_model_id,
                    compat=provider.compat,
                    base_url=provider.base_url,
                    dispatch_headers=self._provider_dispatch_headers(provider),
                )]

        # Filter unhealthy routes (route-level cooldown) AND credentials
        # in 429 cooldown BEFORE indexing - the caller's ``route_index``
        # walks the HEALTHY list, otherwise a failed primary would shift
        # fallbacks one slot every retry.
        # Saturation filter: when ``_inflight_cap`` is set, drop any
        # route whose credential is at or above the cap so the dispatch
        # falls through to the next route (often a different tier /
        # provider) instead of queueing.
        cap = self._inflight_cap
        healthy: list[CachedRoute] = []
        for r in candidates:
            health = self._route_health.get(r.id)
            if health is not None and health.blocked_until > now:
                continue
            ch = self._cred_health.get(r.credential_id)
            if ch is not None and ch.blocked_until_429 > now:
                continue
            if cap > 0 and ch is not None and ch.inflight >= cap:
                continue
            healthy.append(r)

        # Multi-account load balance: within a priority tier, pick the
        # credential with the lowest in-flight count first. This turns a
        # static priority-strict failover into a true round-robin under
        # uniform load while preserving cross-tier failover semantics.
        # Tiebreak by ``consecutive_429s`` (prefer the cred that's been
        # behaving) then by route id (deterministic) so two replicas of
        # the gateway pick the same order on the same cache snapshot.
        def _sort_key(r: CachedRoute) -> tuple[int, int, int, str]:
            ch = self._cred_health.get(r.credential_id)
            return (
                r.priority,
                ch.inflight if ch is not None else 0,
                ch.consecutive_429s if ch is not None else 0,
                str(r.id),
            )
        healthy.sort(key=_sort_key)

        if route_index >= len(healthy):
            # Out of candidates - try env-var fallback only on the last
            # call (route_index == 0 with empty healthy list). The env
            # fallback always uses the alias's primary provider since
            # there's no route row to override.
            if route_index == 0 and (
                provider.env_var and provider.auth_type == "api_key"
            ):
                v = os.environ.get(provider.env_var)
                if v:
                    secret_data = {"value": v}
                    injected = _dispatch(provider.auth_type, secret_data)
                    return ResolvedDispatch(
                        alias=alias,
                        provider_slug=m.provider_slug,
                        real_model_id=m.real_model_id,
                        api_key=injected.api_key,
                        base_url=injected.api_base or provider.base_url,
                        compat=provider.compat, is_custom=False,
                        cost_per_1k_input=m.cost_per_1k_input,
                        cost_per_1k_output=m.cost_per_1k_output,
                        max_context=m.max_context,
                        auth_type=provider.auth_type,
                        extra_headers={
                            **self._provider_dispatch_headers(provider),
                            **injected.extra_headers,
                        },
                        extra_body=dict(injected.extra_body),
                    )
            return None

        route = healthy[route_index]
        cred = self._credentials.get(route.credential_id)
        if cred is None or cred.status != "active":
            return None

        # Cross-provider routing: the route owns the dispatch identity.
        # Pull the route's provider for auth_type (each provider has its
        # own auth scheme - api_key, oauth, claude_code, ...). If the
        # route's provider was archived since the cache last reloaded,
        # fall back to the alias's provider so we don't 500 mid-request.
        route_provider = self._providers.get(route.provider_slug) or provider
        if cred.provider_slug != route.provider_slug:
            # Sanity guard: the API enforces this at write time but a
            # rotated credential or a hand-edited row could break the
            # invariant. Skip the route rather than dispatch with a
            # mismatched bearer.
            return None

        # Provider-level dispatch_headers come from the ROUTE's provider
        # row (for cross-provider routes that's not the alias's
        # provider). The route can additionally override / add headers
        # via its own dispatch_headers JSONB.
        provider_headers = self._provider_dispatch_headers(route_provider)
        merged_headers = {**provider_headers, **route.dispatch_headers}

        injected = _dispatch(route_provider.auth_type, cred.secret_data)
        if (injected.api_key in (None, "")
                and not injected.extra_headers
                and not injected.extra_body
                and not merged_headers):
            return None

        # base_url precedence: dispatcher's > route's > route_provider's.
        # The dispatcher (e.g. claude_code OAuth) sometimes mints an
        # api_base dynamically; that wins. Otherwise the route can pin
        # an explicit endpoint; otherwise we fall back to the provider's
        # default (e.g. https://api.openai.com/v1).
        return ResolvedDispatch(
            alias=alias,
            provider_slug=route.provider_slug,
            real_model_id=route.real_model_id,
            api_key=injected.api_key,
            base_url=(
                injected.api_base
                or route.base_url
                or route_provider.base_url
            ),
            compat=route.compat,
            is_custom=False,
            cost_per_1k_input=m.cost_per_1k_input,
            cost_per_1k_output=m.cost_per_1k_output,
            max_context=m.max_context,
            auth_type=route_provider.auth_type,
            extra_headers={**merged_headers, **injected.extra_headers},
            extra_body=dict(injected.extra_body),
            route_id=route.id,
            credential_id=cred.id,
            live_pool=cred.live_pool,
        )

    @staticmethod
    def _provider_dispatch_headers(provider: CachedProvider) -> dict[str, str]:
        """Pull dashboard-owned per-provider headers out of metadata,
        defensively coercing every value to a string so a malformed
        edit can't crash the dispatch path."""
        raw = (provider.extra_metadata or {}).get("dispatch_headers") or {}
        if not isinstance(raw, dict):
            return {}
        out: dict[str, str] = {}
        for k, v in raw.items():
            if isinstance(k, str) and v is not None:
                out[k] = str(v)
        return out

    # ── Health bookkeeping ───────────────────────────────────────

    def mark_route_success(self, route_id: uuid.UUID) -> None:
        h = self._route_health.get(route_id)
        if h is None:
            h = RouteHealth()
            self._route_health[route_id] = h
        h.consecutive_failures = 0
        h.blocked_until = 0.0
        h.last_success_at = time.monotonic()
        h.last_error = ""

    def mark_route_failure(
        self, route_id: uuid.UUID, error: str, *, cooldown_s: float = 30.0,
    ) -> None:
        h = self._route_health.get(route_id)
        if h is None:
            h = RouteHealth()
            self._route_health[route_id] = h
        h.consecutive_failures += 1
        h.last_error = error[:200]
        # Block after 3 consecutive failures for cooldown_s, exponential
        # backoff up to 5 minutes.
        if h.consecutive_failures >= 3:
            backoff = min(cooldown_s * (2 ** (h.consecutive_failures - 3)), 300)
            h.blocked_until = time.monotonic() + backoff

    def route_health_snapshot(self) -> dict[uuid.UUID, dict[str, Any]]:
        now = time.monotonic()
        out: dict[uuid.UUID, dict[str, Any]] = {}
        for rid, h in self._route_health.items():
            out[rid] = {
                "consecutive_failures": h.consecutive_failures,
                "is_blocked": h.blocked_until > now,
                "blocked_for_s": max(0.0, h.blocked_until - now),
                "last_error": h.last_error,
            }
        return out

    # ── Credential health (multi-account load balance + 429 cooldown) ──

    def mark_dispatch_started(self, credential_id: uuid.UUID) -> None:
        """Increment the in-flight counter for ``credential_id``.

        Called BEFORE the LiteLLM call so the resolver sees the load
        as soon as it lands. Pair every call with
        ``mark_dispatch_finished`` in a ``finally`` block; otherwise
        a crashing handler leaks counts and starves the credential.
        """
        ch = self._cred_health.get(credential_id)
        if ch is None:
            ch = CredentialHealth()
            self._cred_health[credential_id] = ch
        ch.inflight += 1
        ch.total_dispatched += 1

    def mark_dispatch_finished(self, credential_id: uuid.UUID) -> None:
        """Decrement the in-flight counter. Floors at zero so a stray
        finished-without-started doesn't underflow into negatives."""
        ch = self._cred_health.get(credential_id)
        if ch is None:
            return
        if ch.inflight > 0:
            ch.inflight -= 1

    def mark_credential_429(
        self,
        credential_id: uuid.UUID,
        *,
        retry_after_s: float | None = None,
    ) -> None:
        """Apply a per-credential 429 cooldown. Honors ``retry_after_s``
        when the upstream sent ``Retry-After`` (Anthropic + OpenAI).
        Falls back to 60 s × 2^(consecutive-1) capped at 5 min when
        the header was absent.

        The credential is filtered out of dispatch candidates while
        ``blocked_until_429`` is in the future. Other credentials in
        the same priority tier keep serving normally.
        """
        ch = self._cred_health.get(credential_id)
        if ch is None:
            ch = CredentialHealth()
            self._cred_health[credential_id] = ch
        ch.consecutive_429s += 1
        ch.last_429_at = time.monotonic()
        if retry_after_s is not None and retry_after_s > 0:
            cooldown = min(float(retry_after_s), 300.0)
        else:
            cooldown = min(60.0 * (2 ** (ch.consecutive_429s - 1)), 300.0)
        ch.blocked_until_429 = time.monotonic() + cooldown

    def mark_credential_success(self, credential_id: uuid.UUID) -> None:
        """Reset the 429 counter on a successful dispatch. Lets a
        credential recover quickly after a transient throttle."""
        ch = self._cred_health.get(credential_id)
        if ch is None:
            return
        ch.consecutive_429s = 0
        ch.blocked_until_429 = 0.0

    def credential_health_snapshot(
        self,
    ) -> dict[uuid.UUID, dict[str, Any]]:
        now = time.monotonic()
        out: dict[uuid.UUID, dict[str, Any]] = {}
        for cid, ch in self._cred_health.items():
            out[cid] = {
                "inflight": ch.inflight,
                "consecutive_429s": ch.consecutive_429s,
                "is_429_blocked": ch.blocked_until_429 > now,
                "blocked_for_s": max(0.0, ch.blocked_until_429 - now),
                "total_dispatched": ch.total_dispatched,
            }
        return out

    def all_routes(self) -> list[CachedRoute]:
        return [r for routes in self._routes.values() for r in routes]

    def has_provider(self, slug: str) -> bool:
        return slug in self._providers

    def is_provider_configured(self, slug: str) -> bool:
        """True when a provider has at least one active credential OR an
        env var fallback. The pre-flight gating uses this to fail fast
        with ``model_not_provided_by_digitorn`` BEFORE we hit LiteLLM.
        """
        if slug in self._provider_default_cred:
            return True
        p = self._providers.get(slug)
        if p is None:
            return False
        if p.env_var and os.environ.get(p.env_var):
            return True
        return False

    def env_var_for(self, slug: str) -> str | None:
        p = self._providers.get(slug)
        return p.env_var if p else None

    def provider(self, slug: str) -> CachedProvider | None:
        return self._providers.get(slug)

    def model(self, alias: str) -> CachedModel | None:
        return self._models.get(alias)

    def all_providers(self) -> list[CachedProvider]:
        return list(self._providers.values())

    def all_models(self) -> list[CachedModel]:
        return list(self._models.values())

    def loaded_at_monotonic(self) -> float:
        return self._loaded_at

    # ── Write-through (called from CRUD route handlers) ──────────

    def upsert_provider(
        self,
        slug: str,
        *,
        name: str,
        base_url: str | None,
        compat: str,
        env_var: str | None,
        auth_type: str = "api_key",
        extra_metadata: dict[str, Any] | None = None,
    ) -> None:
        self._providers[slug] = CachedProvider(
            slug=slug, name=name, base_url=base_url,
            compat=compat, env_var=env_var, auth_type=auth_type,
            extra_metadata=dict(extra_metadata or {}),
        )

    def remove_provider(self, slug: str) -> None:
        self._providers.pop(slug, None)
        # Drop credentials + provider_default + routes that referenced it.
        self._provider_default_cred.pop(slug, None)
        # Snapshot which credentials belong to this provider BEFORE we
        # drop them, so the route filter below still recognises them
        # as "owned by this provider".
        provider_cred_ids = {
            cid for cid, c in self._credentials.items()
            if c.provider_slug == slug
        }
        for cid in list(provider_cred_ids):
            self._credentials.pop(cid, None)
        # ``self._routes`` is ``dict[alias, list[CachedRoute]]``. Drop
        # any route whose ``provider_slug`` matches OR whose credential
        # was just removed (orphan). Walk per-alias and rebuild the
        # list - if it becomes empty, drop the alias entry entirely.
        for alias in list(self._routes.keys()):
            kept = [
                r for r in self._routes[alias]
                if r.provider_slug != slug
                and r.credential_id not in provider_cred_ids
            ]
            if kept:
                self._routes[alias] = kept
            else:
                self._routes.pop(alias, None)

    def upsert_credential(
        self,
        cred_id: uuid.UUID,
        *,
        provider_slug: str,
        label: str,
        secret_data: dict[str, str],
        status: str,
        live_pool: bool = True,
    ) -> None:
        cred = CachedCredential(
            id=cred_id,
            provider_slug=provider_slug,
            label=label,
            secret_data=secret_data,
            status=status,
            live_pool=live_pool,
        )
        self._credentials[cred_id] = cred
        if status == "active":
            self._provider_default_cred[provider_slug] = cred_id
        else:
            # If the just-disabled cred was the default, pick another active.
            if self._provider_default_cred.get(provider_slug) == cred_id:
                self._recompute_default(provider_slug)

    def remove_credential(self, cred_id: uuid.UUID) -> None:
        cred = self._credentials.pop(cred_id, None)
        # Drop routes pointing at it.
        for alias, cid in list(self._routes.items()):
            if cid == cred_id:
                self._routes.pop(alias, None)
        # Drop the credential's health snapshot too; otherwise stale
        # 429 cooldowns survive a recreate-with-same-id.
        self._cred_health.pop(cred_id, None)
        if cred is not None:
            if self._provider_default_cred.get(cred.provider_slug) == cred_id:
                self._recompute_default(cred.provider_slug)

    def _recompute_default(self, provider_slug: str) -> None:
        # Pick the most recent active cred for the provider.
        best_id: uuid.UUID | None = None
        for c in self._credentials.values():
            if c.provider_slug == provider_slug and c.status == "active":
                # Order is undefined in dict; keep the last one we see.
                # The reload_from_db keeps proper ordering by created_at,
                # which is good enough.
                best_id = c.id
        if best_id is None:
            self._provider_default_cred.pop(provider_slug, None)
        else:
            self._provider_default_cred[provider_slug] = best_id

    def upsert_model(
        self,
        alias: str,
        *,
        provider_slug: str,
        real_model_id: str,
        cost_per_1k_input: float,
        cost_per_1k_output: float,
        max_context: int | None,
        is_custom: bool,
    ) -> None:
        self._models[alias] = CachedModel(
            alias=alias,
            provider_slug=provider_slug,
            real_model_id=real_model_id,
            cost_per_1k_input=cost_per_1k_input,
            cost_per_1k_output=cost_per_1k_output,
            max_context=max_context,
            is_custom=is_custom,
        )

    def remove_model(self, alias: str) -> None:
        self._models.pop(alias, None)
        self._routes.pop(alias, None)

    def set_route(
        self,
        route_id: uuid.UUID,
        *,
        alias: str,
        credential_id: uuid.UUID,
        priority: int,
        provider_slug: str,
        real_model_id: str,
        compat: str,
        base_url: str | None = None,
        dispatch_headers: dict[str, str] | None = None,
    ) -> None:
        """Insert / update one CachedRoute. The cache is re-sorted by
        priority so the dispatch walk picks the right order."""
        existing = self._routes.get(alias) or []
        new = CachedRoute(
            id=route_id, model_alias=alias,
            credential_id=credential_id, priority=priority,
            provider_slug=provider_slug,
            real_model_id=real_model_id,
            compat=compat,
            base_url=base_url,
            dispatch_headers=dict(dispatch_headers or {}),
        )
        # Replace existing same-id entry, else append.
        replaced = False
        for i, r in enumerate(existing):
            if r.id == route_id:
                existing[i] = new
                replaced = True
                break
        if not replaced:
            existing.append(new)
        existing.sort(key=lambda r: r.priority)
        self._routes[alias] = existing

    def remove_route(self, route_id: uuid.UUID, *, alias: str | None = None) -> None:
        """Drop one route by id. Walks all aliases when ``alias`` is None."""
        targets = [alias] if alias else list(self._routes.keys())
        for a in targets:
            routes = self._routes.get(a) or []
            kept = [r for r in routes if r.id != route_id]
            if kept:
                self._routes[a] = kept
            else:
                self._routes.pop(a, None)
        self._route_health.pop(route_id, None)


# ── Module-level singleton + lifecycle helpers ──────────────────────


_cache: Optional[ConfigCache] = None
_refresh_task: Optional[asyncio.Task] = None


def get_cache() -> ConfigCache:
    """Return the process-wide cache. Auto-create when missing."""
    global _cache
    if _cache is None:
        _cache = ConfigCache()
    return _cache


def reset_cache_for_tests() -> None:
    global _cache
    _cache = ConfigCache()


async def start_refresh_loop(
    factory: async_sessionmaker[AsyncSession],
    interval_s: float = CONFIG_CACHE_REFRESH_S,
) -> asyncio.Task:
    """Spawn the background coroutine that re-reads the DB every
    ``interval_s`` seconds. Safe to call multiple times - subsequent
    calls cancel the previous task before starting a new one."""
    global _refresh_task
    if _refresh_task is not None and not _refresh_task.done():
        _refresh_task.cancel()

    cache = get_cache()

    async def _loop() -> None:
        while True:
            try:
                await asyncio.sleep(interval_s)
                await cache.reload_from_db(factory)
            except asyncio.CancelledError:
                return
            except Exception as exc:
                logger.warning(
                    "config_cache refresh failed (continuing): %s", exc,
                )

    _refresh_task = asyncio.create_task(_loop(), name="config-cache-refresh")
    return _refresh_task


async def stop_refresh_loop() -> None:
    global _refresh_task
    if _refresh_task is not None:
        _refresh_task.cancel()
        try:
            await _refresh_task
        except (asyncio.CancelledError, Exception):
            pass
        _refresh_task = None
