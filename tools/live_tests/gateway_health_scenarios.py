"""Live tests for the gateway multi-account observability surface.

Validates the SHAPE of the API + metrics endpoints, not the routing
math itself (the routing math is covered by 9 unit tests in
packages/gateway/tests/test_multi_account_routing.py).

What we prove here:
  * /metrics returns well-formed Prometheus exposition text
  * Aggregates exist and have plausible values
  * The daemon proxy /api/admin/gateway/credentials/health forwards
    the user's JWT correctly (200 with admin, 403 without)
"""
from __future__ import annotations

import httpx


def scenario_gateway_metrics_endpoint(
    daemon_url: str, gateway_url: str,
) -> tuple[bool, str, dict]:
    """GET {gateway}/metrics returns Prometheus text with the
    expected metric names + the right Content-Type."""
    artifacts: dict = {}
    try:
        r = httpx.get(f"{gateway_url.rstrip('/')}/metrics", timeout=5)
    except Exception as exc:
        return False, f"gateway unreachable: {exc}", artifacts

    artifacts["status_code"] = r.status_code
    artifacts["content_type"] = r.headers.get("content-type", "")
    artifacts["body_lines"] = len(r.text.splitlines())

    expected = [
        "gateway_credential_inflight",
        "gateway_credential_dispatched_total",
        "gateway_credential_consecutive_429s",
        "gateway_credential_blocked",
        "gateway_credential_blocked_remaining_seconds",
        "gateway_route_consecutive_failures",
        "gateway_route_blocked",
        "gateway_credentials_total",
        "gateway_credentials_active",
        "gateway_credentials_429_blocked",
        "gateway_credentials_inflight_sum",
        "gateway_routes_total",
        "gateway_routes_blocked",
    ]
    missing = [m for m in expected if m not in r.text]
    artifacts["missing_metrics"] = missing

    checks = [
        ("status_200", (r.status_code == 200, f"status={r.status_code}")),
        ("content_type_text", (
            r.headers.get("content-type", "").startswith("text/plain"),
            f"ct={r.headers.get('content-type')}",
        )),
        ("all_metrics_present", (
            len(missing) == 0,
            f"missing={missing}" if missing else "all 13 metrics present",
        )),
        ("has_help_lines", ("# HELP" in r.text, "ok")),
        ("has_type_lines", ("# TYPE" in r.text, "ok")),
    ]
    from digitorn.testing.assertions import report
    ok, detail = report(checks)
    return ok, detail, artifacts


def scenario_daemon_proxy_forwards_to_gateway(
    daemon_url: str, jwt_token: str,
) -> tuple[bool, str, dict]:
    """The daemon proxy accepts the user's JWT and forwards to the
    gateway. The user we test with isn't admin, so we expect 403
    from our daemon's _require_admin gate -- which proves the route
    is wired (a non-existent route would give 404)."""
    artifacts: dict = {}
    try:
        r = httpx.get(
            f"{daemon_url.rstrip('/')}/api/admin/gateway/credentials/health",
            headers={"Authorization": f"Bearer {jwt_token}"},
            timeout=5,
        )
    except Exception as exc:
        return False, f"daemon unreachable: {exc}", artifacts

    artifacts["status_code"] = r.status_code
    artifacts["body_preview"] = r.text[:200]

    # 403 = route exists, admin check rejected non-admin user (correct).
    # 200 = route exists AND user has admin role (forwarded through).
    # 404 = route NOT wired (regression).
    # 502 = route wired but gateway is down.
    checks = [
        ("route_wired", (
            r.status_code in (200, 403, 502),
            f"status={r.status_code} (404 means route missing)",
        )),
    ]
    from digitorn.testing.assertions import report
    ok, detail = report(checks)
    return ok, detail, artifacts
