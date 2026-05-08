"""Prometheus /metrics output format + content."""
from __future__ import annotations

import uuid

import pytest


def _seed_two_creds(cache):
    cache.upsert_provider(
        slug="anthropic", name="Anthropic",
        base_url="https://api.anthropic.com",
        compat="anthropic", env_var=None, auth_type="api_key",
        extra_metadata={},
    )
    cache.upsert_model(
        alias="claude-opus", provider_slug="anthropic",
        real_model_id="claude-opus-4-7",
        cost_per_1k_input=0.005, cost_per_1k_output=0.025,
        max_context=1_000_000, is_custom=False,
    )
    cid_a = uuid.uuid4(); cid_b = uuid.uuid4()
    rid_a = uuid.uuid4(); rid_b = uuid.uuid4()
    for cid, lbl in [(cid_a, "primary"), (cid_b, "fallback")]:
        cache.upsert_credential(
            cid, provider_slug="anthropic", label=lbl,
            secret_data={"value": f"sk-{lbl}"}, status="active",
            live_pool=False,
        )
    cache.set_route(
        rid_a, alias="claude-opus", credential_id=cid_a,
        priority=0, provider_slug="anthropic",
        real_model_id="claude-opus-4-7", compat="anthropic",
    )
    cache.set_route(
        rid_b, alias="claude-opus", credential_id=cid_b,
        priority=0, provider_slug="anthropic",
        real_model_id="claude-opus-4-7", compat="anthropic",
    )
    return cid_a, cid_b, rid_a, rid_b


def test_metrics_render_empty(fresh_cache):
    """Empty cache renders aggregates with zero values, no per-row
    metrics. The output is still well-formed Prometheus text."""
    from digitorn_gateway.metrics import render_metrics
    text = render_metrics()
    assert "gateway_credentials_total 0" in text
    assert "gateway_credentials_active 0" in text
    assert "gateway_credentials_429_blocked 0" in text
    assert "gateway_routes_total 0" in text
    assert text.endswith("\n")


def test_metrics_emits_per_credential_rows(fresh_cache):
    """One labeled row per credential after seeding."""
    from digitorn_gateway.metrics import render_metrics
    cid_a, cid_b, _, _ = _seed_two_creds(fresh_cache)
    fresh_cache.mark_dispatch_started(cid_a)
    fresh_cache.mark_dispatch_started(cid_a)
    fresh_cache.mark_dispatch_started(cid_b)
    text = render_metrics()
    assert f'cred_id="{cid_a}"' in text
    assert f'cred_id="{cid_b}"' in text
    assert 'label="primary"' in text
    assert 'label="fallback"' in text
    assert 'provider="anthropic"' in text
    # Inflight = 2 on cred_a (we incremented twice).
    assert any(
        f'cred_id="{cid_a}"' in line and "gateway_credential_inflight" in line
        and line.endswith(" 2")
        for line in text.splitlines()
    )


def test_metrics_emits_429_state(fresh_cache):
    """A 429-blocked credential surfaces as blocked=1 with a positive
    remaining cooldown."""
    from digitorn_gateway.metrics import render_metrics
    cid_a, cid_b, _, _ = _seed_two_creds(fresh_cache)
    fresh_cache.mark_credential_429(cid_a, retry_after_s=42.0)

    text = render_metrics()
    blocked_lines = [
        line for line in text.splitlines()
        if "gateway_credential_blocked{" in line
        and "remaining" not in line
    ]
    a_lines = [line for line in blocked_lines if str(cid_a) in line]
    assert any(line.endswith(" 1") for line in a_lines), (
        f"expected blocked=1 for cred A, got {a_lines}"
    )
    b_lines = [line for line in blocked_lines if str(cid_b) in line]
    assert all(line.endswith(" 0") for line in b_lines), (
        f"cred B should not be blocked, got {b_lines}"
    )
    # Aggregate count reflects 1 blocked.
    assert "gateway_credentials_429_blocked 1" in text


def test_metrics_aggregate_inflight_sum(fresh_cache):
    """The aggregate inflight gauge equals the sum of per-cred inflight."""
    from digitorn_gateway.metrics import render_metrics
    cid_a, cid_b, _, _ = _seed_two_creds(fresh_cache)
    for _ in range(3):
        fresh_cache.mark_dispatch_started(cid_a)
    for _ in range(5):
        fresh_cache.mark_dispatch_started(cid_b)
    text = render_metrics()
    assert "gateway_credentials_inflight_sum 8" in text


def test_metrics_label_value_escaping(fresh_cache):
    """A label containing quote / backslash / newline must be escaped."""
    from digitorn_gateway.metrics import render_metrics
    fresh_cache.upsert_provider(
        slug="anthropic", name="A",
        base_url="https://api.anthropic.com",
        compat="anthropic", env_var=None, auth_type="api_key",
        extra_metadata={},
    )
    cid = uuid.uuid4()
    fresh_cache.upsert_credential(
        cid, provider_slug="anthropic",
        label='weird"\\name\nfoo',
        secret_data={"value": "x"}, status="active", live_pool=False,
    )
    fresh_cache.mark_dispatch_started(cid)
    text = render_metrics()
    assert 'label="weird\\"\\\\name\\nfoo"' in text


def test_metrics_endpoint_returns_plaintext(fresh_cache, monkeypatch):
    """The /metrics route returns a text/plain Prometheus response."""
    from fastapi.testclient import TestClient
    from digitorn_gateway.metrics import router
    from fastapi import FastAPI
    app = FastAPI()
    app.include_router(router)
    _seed_two_creds(fresh_cache)
    client = TestClient(app)
    resp = client.get("/metrics")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/plain")
    assert "gateway_credentials_total" in resp.text


def test_metrics_endpoint_403_when_auth_required(fresh_cache, monkeypatch):
    """When GATEWAY_METRICS_REQUIRE_AUTH=1 the unauthenticated route 403s."""
    monkeypatch.setenv("GATEWAY_METRICS_REQUIRE_AUTH", "1")
    # Reload module to pick up env change.
    import importlib
    from digitorn_gateway import metrics as metrics_mod
    importlib.reload(metrics_mod)
    from fastapi.testclient import TestClient
    from fastapi import FastAPI
    app = FastAPI()
    app.include_router(metrics_mod.router)
    client = TestClient(app)
    resp = client.get("/metrics")
    assert resp.status_code == 403
    # Reset for downstream tests.
    monkeypatch.delenv("GATEWAY_METRICS_REQUIRE_AUTH")
    importlib.reload(metrics_mod)
