"""Prometheus metrics endpoint.

Exposes ``GET /metrics`` in the standard Prometheus text format so an
external Prometheus / Grafana / Alertmanager stack can scrape live
multi-account routing health without ever calling the JSON admin
endpoints.

Metrics surfaced (one row per credential except where noted):

    # Per-credential
    gateway_credential_inflight{cred_id, label, provider}
    gateway_credential_dispatched_total{cred_id, label, provider}
    gateway_credential_consecutive_429s{cred_id, label, provider}
    gateway_credential_blocked{cred_id, label, provider}     # 0 / 1
    gateway_credential_blocked_remaining_seconds{...}

    # Per-route
    gateway_route_consecutive_failures{route_id, alias, provider}
    gateway_route_blocked{route_id, alias, provider}         # 0 / 1
    gateway_route_blocked_remaining_seconds{...}

    # Aggregates (one row each)
    gateway_credentials_total
    gateway_credentials_active
    gateway_credentials_429_blocked
    gateway_credentials_inflight_sum
    gateway_routes_total
    gateway_routes_blocked

The endpoint is unauthenticated by default (Prometheus scrapers run
inside the cluster). When ``GATEWAY_METRICS_REQUIRE_AUTH=1`` is set the
endpoint requires the standard ``require_principal`` admin role; use
this when the gateway is exposed publicly without an internal mesh.
"""
from __future__ import annotations

import os
from typing import Iterable

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import PlainTextResponse

from digitorn_gateway.auth import GatewayPrincipal, require_principal
from digitorn_gateway.config_cache import get_cache

router = APIRouter()

_METRICS_AUTH = os.environ.get("GATEWAY_METRICS_REQUIRE_AUTH") == "1"


def _escape_label_value(s: str | None) -> str:
    """Prometheus label values must escape backslash, double-quote, newline."""
    if s is None:
        return ""
    return (
        s.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
    )


def _labels(pairs: Iterable[tuple[str, str | None]]) -> str:
    parts = [f'{k}="{_escape_label_value(v)}"' for k, v in pairs]
    return "{" + ",".join(parts) + "}"


def render_metrics() -> str:
    """Build the Prometheus exposition string from the live cache.

    Pure read against in-memory dicts; no DB I/O. The render itself
    is microsecond-scale even with thousands of credentials, so the
    scrape interval can safely run at 5-10 s.
    """
    cache = get_cache()
    cred_health = cache.credential_health_snapshot()
    route_health = cache.route_health_snapshot()
    creds = cache._credentials  # noqa: SLF001 - read-only sibling access
    routes = cache.all_routes()

    lines: list[str] = []

    # ── Per-credential metrics ──────────────────────────────────────
    lines.append(
        "# HELP gateway_credential_inflight In-flight dispatch count per credential."
    )
    lines.append("# TYPE gateway_credential_inflight gauge")
    lines.append(
        "# HELP gateway_credential_dispatched_total "
        "Cumulative successful dispatches accepted for a credential."
    )
    lines.append("# TYPE gateway_credential_dispatched_total counter")
    lines.append(
        "# HELP gateway_credential_consecutive_429s Consecutive 429s "
        "since the last successful dispatch."
    )
    lines.append("# TYPE gateway_credential_consecutive_429s gauge")
    lines.append(
        "# HELP gateway_credential_blocked 1 when the credential "
        "is in 429 cooldown, 0 otherwise."
    )
    lines.append("# TYPE gateway_credential_blocked gauge")
    lines.append(
        "# HELP gateway_credential_blocked_remaining_seconds "
        "Remaining 429 cooldown for the credential. 0 when healthy."
    )
    lines.append("# TYPE gateway_credential_blocked_remaining_seconds gauge")

    inflight_sum = 0
    blocked_count = 0
    active_count = 0
    for cid, h in cred_health.items():
        cred = creds.get(cid)
        labels = _labels([
            ("cred_id", str(cid)),
            ("label", cred.label if cred else None),
            ("provider", cred.provider_slug if cred else None),
        ])
        inflight_sum += h["inflight"]
        if h["is_429_blocked"]:
            blocked_count += 1
        if cred is not None and cred.status == "active":
            active_count += 1
        lines.append(f"gateway_credential_inflight{labels} {h['inflight']}")
        lines.append(
            f"gateway_credential_dispatched_total{labels} "
            f"{h['total_dispatched']}"
        )
        lines.append(
            f"gateway_credential_consecutive_429s{labels} "
            f"{h['consecutive_429s']}"
        )
        lines.append(
            f"gateway_credential_blocked{labels} "
            f"{1 if h['is_429_blocked'] else 0}"
        )
        lines.append(
            f"gateway_credential_blocked_remaining_seconds{labels} "
            f"{h['blocked_for_s']:.3f}"
        )

    # ── Per-route metrics ───────────────────────────────────────────
    lines.append(
        "# HELP gateway_route_consecutive_failures "
        "Consecutive failed dispatches per route."
    )
    lines.append("# TYPE gateway_route_consecutive_failures gauge")
    lines.append(
        "# HELP gateway_route_blocked 1 when the route is in cooldown."
    )
    lines.append("# TYPE gateway_route_blocked gauge")
    lines.append(
        "# HELP gateway_route_blocked_remaining_seconds "
        "Remaining route cooldown."
    )
    lines.append("# TYPE gateway_route_blocked_remaining_seconds gauge")

    routes_blocked_count = 0
    routes_by_id = {r.id: r for r in routes}
    for rid, h in route_health.items():
        r = routes_by_id.get(rid)
        labels = _labels([
            ("route_id", str(rid)),
            ("alias", r.model_alias if r else None),
            ("provider", r.provider_slug if r else None),
        ])
        if h["is_blocked"]:
            routes_blocked_count += 1
        lines.append(
            f"gateway_route_consecutive_failures{labels} "
            f"{h['consecutive_failures']}"
        )
        lines.append(
            f"gateway_route_blocked{labels} "
            f"{1 if h['is_blocked'] else 0}"
        )
        lines.append(
            f"gateway_route_blocked_remaining_seconds{labels} "
            f"{h['blocked_for_s']:.3f}"
        )

    # ── Aggregates ──────────────────────────────────────────────────
    lines.append(
        "# HELP gateway_credentials_total Total credentials known to the gateway."
    )
    lines.append("# TYPE gateway_credentials_total gauge")
    lines.append(f"gateway_credentials_total {len(creds)}")

    lines.append(
        "# HELP gateway_credentials_active "
        "Credentials with status='active'."
    )
    lines.append("# TYPE gateway_credentials_active gauge")
    lines.append(f"gateway_credentials_active {active_count}")

    lines.append(
        "# HELP gateway_credentials_429_blocked "
        "Credentials currently inside a 429 cooldown."
    )
    lines.append("# TYPE gateway_credentials_429_blocked gauge")
    lines.append(f"gateway_credentials_429_blocked {blocked_count}")

    lines.append(
        "# HELP gateway_credentials_inflight_sum "
        "Aggregate in-flight dispatches across every credential."
    )
    lines.append("# TYPE gateway_credentials_inflight_sum gauge")
    lines.append(f"gateway_credentials_inflight_sum {inflight_sum}")

    lines.append(
        "# HELP gateway_routes_total Total routes configured."
    )
    lines.append("# TYPE gateway_routes_total gauge")
    lines.append(f"gateway_routes_total {len(routes)}")

    lines.append(
        "# HELP gateway_routes_blocked Routes currently inside the failure cooldown."
    )
    lines.append("# TYPE gateway_routes_blocked gauge")
    lines.append(f"gateway_routes_blocked {routes_blocked_count}")

    return "\n".join(lines) + "\n"


@router.get("/metrics", response_class=PlainTextResponse)
async def metrics() -> PlainTextResponse:
    """Prometheus text exposition. Unauthenticated by default; set
    ``GATEWAY_METRICS_REQUIRE_AUTH=1`` to require the admin role."""
    if _METRICS_AUTH:
        # Defer the dependency check to runtime so the unprotected
        # path stays the simple branch (no auth machinery on the hot
        # scrape path).
        raise HTTPException(
            403,
            detail=(
                "metrics require auth (GATEWAY_METRICS_REQUIRE_AUTH=1); "
                "use /admin/metrics instead"
            ),
        )
    return PlainTextResponse(
        render_metrics(),
        media_type="text/plain; version=0.0.4; charset=utf-8",
    )


@router.get("/admin/metrics", response_class=PlainTextResponse)
async def admin_metrics(
    principal: GatewayPrincipal = Depends(require_principal),
) -> PlainTextResponse:
    """Same content as ``/metrics`` but admin-gated. Use this when the
    gateway is internet-exposed without a private metrics network."""
    if "admin" not in principal.roles:
        raise HTTPException(403, detail="admin_role_required")
    return PlainTextResponse(
        render_metrics(),
        media_type="text/plain; version=0.0.4; charset=utf-8",
    )
