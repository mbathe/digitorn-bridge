"""End-to-end integration tests for the HTTP API surface.

Each test mounts the relevant router(s) into a minimal FastAPI
app, injects fake state (stores, manager, auth), and exercises
routes via ``TestClient``. This catches wiring bugs that
unit-level tests miss - routing, dependency injection, request
validation, response serialization, and the auth/scope guards.

Scope:
- Credentials CRUD + grants
- Inbox + events + approvals
- Usage + quotas + profile
- Packages + scope filtering
- MCP admin gate
- Discovery prompt-preview
- Per-user isolation (alice vs bob)
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from starlette.responses import JSONResponse

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "packages"))


# ════════════════════════════════════════════════════════════════════
# Shared helpers
# ════════════════════════════════════════════════════════════════════


async def _init_db_sqlite_memory():
    from digitorn.core.config import get_settings, override_settings
    from digitorn.core.database import Base, init_db, get_session_factory

    settings = get_settings()
    override_settings(settings.model_copy(update={
        "database": settings.database.model_copy(update={
            "url": "sqlite+aiosqlite:///:memory:",
        }),
    }))
    engine = await init_db(get_settings())
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return get_session_factory()


def _make_auth_middleware(app: FastAPI, user_id: str, permissions: list[str]):
    """Attach a minimal middleware that sets request.state.user_id
    and request.state.permissions so the routes can run without a
    real auth layer."""

    @app.middleware("http")
    async def _inject_auth(request: Request, call_next):
        request.state.user_id = user_id
        request.state.permissions = list(permissions)
        # Fake user object for /auth/me style routes
        request.state.user = SimpleNamespace(
            user_id=user_id,
            email=f"{user_id}@example.com",
            display_name=user_id.title(),
            roles=["admin"] if "*" in permissions else ["user"],
            permissions=permissions,
        )
        return await call_next(request)


def _build_app(router, state_attrs: dict):
    """Build a minimal FastAPI app with a router mounted and
    selected state attrs injected."""
    app = FastAPI()
    for k, v in state_attrs.items():
        setattr(app.state, k, v)
    app.include_router(router)
    return app


# ════════════════════════════════════════════════════════════════════
# Credentials integration
# ════════════════════════════════════════════════════════════════════


@pytest.fixture
def credentials_app():
    """Build an app with the credentials router + state wired.

    Returns (app, credential_store, cleanup) - the caller adds an
    auth middleware with the right user_id + permissions.
    """
    import asyncio

    async def _setup():
        factory = await _init_db_sqlite_memory()
        from digitorn.core.credentials import (
            CredentialStore, Cipher, load_or_create_master_key,
        )
        master_key = load_or_create_master_key()
        cipher = Cipher(master_key)
        store = CredentialStore(factory, cipher)
        return store

    store = asyncio.new_event_loop().run_until_complete(_setup())
    from digitorn.core.api.credentials import router

    app = FastAPI()
    app.state.credential_store = store
    # Some credential routes use _get_manager - inject a stub
    app.state.app_manager = SimpleNamespace(
        get=lambda _: None,
        modules={},
    )
    app.include_router(router)
    return app, store


def test_credentials_providers_catalog():
    """GET /api/credentials/providers returns the static catalog."""
    app, _ = credentials_app.__wrapped__() if hasattr(credentials_app, "__wrapped__") else (None, None)
    # Build fresh
    import asyncio
    factory = asyncio.new_event_loop().run_until_complete(_init_db_sqlite_memory())
    from digitorn.core.credentials import (
        CredentialStore, Cipher, load_or_create_master_key,
    )
    cipher = Cipher(load_or_create_master_key())
    store = CredentialStore(factory, cipher)

    from digitorn.core.api.credentials import router
    app = FastAPI()
    app.state.credential_store = store
    app.state.app_manager = SimpleNamespace(get=lambda _: None)
    _make_auth_middleware(app, "alice", ["user"])
    app.include_router(router)

    client = TestClient(app)
    resp = client.get("/api/credentials/providers")
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["count"] >= 20
    assert any(p["id"] == "anthropic" for p in data["providers"])
    assert any(p["id"] == "openai" for p in data["providers"])
    # Icon field exists
    assert all("icon" in p for p in data["providers"])


def test_credentials_crud_flow():
    """POST → GET → PUT → DELETE flow on /api/credentials."""
    import asyncio
    factory = asyncio.new_event_loop().run_until_complete(_init_db_sqlite_memory())
    from digitorn.core.credentials import (
        CredentialStore, Cipher, load_or_create_master_key,
    )
    cipher = Cipher(load_or_create_master_key())
    store = CredentialStore(factory, cipher)

    from digitorn.core.api.credentials import router
    app = FastAPI()
    app.state.credential_store = store
    app.state.app_manager = SimpleNamespace(get=lambda _: None)
    _make_auth_middleware(app, "alice", ["user"])
    app.include_router(router)

    client = TestClient(app)

    # Create
    resp = client.post("/api/credentials", json={
        "provider_name": "deepseek",
        "provider_type": "api_key",
        "label": "personal",
        "fields": {"api_key": "sk-alice-deepseek"},
    })
    assert resp.status_code == 200, resp.text
    cred = resp.json()["data"]
    assert cred["provider_name"] == "deepseek"
    assert cred["label"] == "personal"
    assert cred["owner_type"] == "user"
    cred_id = cred["id"]

    # List
    resp = client.get("/api/credentials")
    assert resp.status_code == 200
    listing = resp.json()["data"]
    assert listing["count"] == 1
    assert listing["credentials"][0]["id"] == cred_id

    # Filter by provider
    resp = client.get("/api/credentials?provider=deepseek")
    assert resp.json()["data"]["count"] == 1
    resp = client.get("/api/credentials?provider=openai")
    assert resp.json()["data"]["count"] == 0

    # Get by id
    resp = client.get(f"/api/credentials/{cred_id}")
    assert resp.status_code == 200
    assert resp.json()["data"]["id"] == cred_id

    # Update label
    resp = client.put(f"/api/credentials/{cred_id}", json={
        "label": "updated",
    })
    assert resp.status_code == 200
    assert resp.json()["data"]["label"] == "updated"

    # Delete
    resp = client.delete(f"/api/credentials/{cred_id}")
    assert resp.status_code == 200
    resp = client.get(f"/api/credentials/{cred_id}")
    assert resp.status_code == 404


def test_credentials_grants_flow():
    """Create cred → grant to app → revoke → verify isolation."""
    import asyncio
    factory = asyncio.new_event_loop().run_until_complete(_init_db_sqlite_memory())
    from digitorn.core.credentials import (
        CredentialStore, Cipher, load_or_create_master_key,
    )
    cipher = Cipher(load_or_create_master_key())
    store = CredentialStore(factory, cipher)

    from digitorn.core.api.credentials import router
    app = FastAPI()
    app.state.credential_store = store
    app.state.app_manager = SimpleNamespace(get=lambda _: None)
    _make_auth_middleware(app, "alice", ["user"])
    app.include_router(router)

    client = TestClient(app)

    # Create a credential
    resp = client.post("/api/credentials", json={
        "provider_name": "openai",
        "provider_type": "api_key",
        "fields": {"api_key": "sk-alice"},
    })
    cred_id = resp.json()["data"]["id"]

    # Grant it to an app
    resp = client.post(
        f"/api/credentials/{cred_id}/grants",
        json={"app_id": "digitorn-code"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["active"] is True

    # List grants for this credential
    resp = client.get(f"/api/credentials/{cred_id}/grants")
    assert resp.status_code == 200
    assert resp.json()["data"]["count"] == 1

    # List all my grants
    resp = client.get("/api/credentials-grants")
    assert resp.status_code == 200
    grants = resp.json()["data"]["grants"]
    assert len(grants) == 1
    assert grants[0]["app_id"] == "digitorn-code"

    # Revoke
    resp = client.delete(f"/api/credentials/{cred_id}/grants/digitorn-code")
    assert resp.status_code == 200

    # Active grants should be 0
    resp = client.get("/api/credentials-grants")
    assert resp.json()["data"]["count"] == 0


def test_credentials_admin_system_refused_for_non_admin():
    """Non-admin can't create system credentials."""
    import asyncio
    factory = asyncio.new_event_loop().run_until_complete(_init_db_sqlite_memory())
    from digitorn.core.credentials import (
        CredentialStore, Cipher, load_or_create_master_key,
    )
    cipher = Cipher(load_or_create_master_key())
    store = CredentialStore(factory, cipher)

    from digitorn.core.api.credentials import router
    app = FastAPI()
    app.state.credential_store = store
    app.state.app_manager = SimpleNamespace(get=lambda _: None)
    _make_auth_middleware(app, "alice", ["user"])  # non-admin
    app.include_router(router)

    client = TestClient(app)
    resp = client.post("/api/admin/credentials", json={
        "provider_name": "openai",
        "provider_type": "api_key",
        "fields": {"api_key": "sk-global"},
    })
    assert resp.status_code == 403


def test_credentials_admin_system_creates_for_admin():
    """Admin can create system credentials."""
    import asyncio
    factory = asyncio.new_event_loop().run_until_complete(_init_db_sqlite_memory())
    from digitorn.core.credentials import (
        CredentialStore, Cipher, load_or_create_master_key,
    )
    cipher = Cipher(load_or_create_master_key())
    store = CredentialStore(factory, cipher)

    from digitorn.core.api.credentials import router
    app = FastAPI()
    app.state.credential_store = store
    app.state.app_manager = SimpleNamespace(get=lambda _: None)
    _make_auth_middleware(app, "admin", ["*"])  # admin
    app.include_router(router)

    client = TestClient(app)
    resp = client.post("/api/admin/credentials", json={
        "provider_name": "openai",
        "provider_type": "api_key",
        "fields": {"api_key": "sk-global"},
    })
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["owner_type"] == "system"


# ════════════════════════════════════════════════════════════════════
# Inbox + usage integration
# ════════════════════════════════════════════════════════════════════


def test_inbox_routes_end_to_end():
    """Create item directly in store, list/mark-read/archive via API."""
    import asyncio
    from digitorn.core.inbox import InboxStore

    factory = asyncio.new_event_loop().run_until_complete(_init_db_sqlite_memory())
    inbox_store = InboxStore(factory)

    # Pre-populate 3 items
    async def _seed():
        await inbox_store.create_item(
            user_id="alice", kind="session.completed",
            title="Turn 1", subtitle="...", app_id="app1",
        )
        await inbox_store.create_item(
            user_id="alice", kind="session.failed",
            title="Turn 2", subtitle="oops", app_id="app1",
        )
        await inbox_store.create_item(
            user_id="bob", kind="session.completed",
            title="Bob's turn", subtitle="...", app_id="app2",
        )
    asyncio.new_event_loop().run_until_complete(_seed())

    from digitorn.core.api.user import router
    app = FastAPI()
    app.state.inbox_store = inbox_store
    app.state.app_manager = SimpleNamespace(
        get=lambda _: None,
        _deployed={},
    )
    _make_auth_middleware(app, "alice", ["user"])
    app.include_router(router)

    client = TestClient(app)

    # List - Alice sees her 2, not Bob's 1
    resp = client.get("/api/users/me/inbox")
    assert resp.status_code == 200
    items = resp.json()["data"]["items"]
    assert len(items) == 2
    assert all(i["kind"].startswith("session.") for i in items)

    # Unread count
    resp = client.get("/api/users/me/inbox/unread_count")
    assert resp.json()["data"]["unread_count"] == 2

    # Mark one as read
    first_id = items[0]["id"]
    resp = client.post(f"/api/users/me/inbox/{first_id}/read")
    assert resp.status_code == 200
    resp = client.get("/api/users/me/inbox/unread_count")
    assert resp.json()["data"]["unread_count"] == 1

    # Mark all as read
    resp = client.post("/api/users/me/inbox/read_all")
    assert resp.status_code == 200
    resp = client.get("/api/users/me/inbox/unread_count")
    assert resp.json()["data"]["unread_count"] == 0

    # Archive one
    resp = client.delete(f"/api/users/me/inbox/{first_id}")
    assert resp.status_code == 200
    # Default list excludes archived
    resp = client.get("/api/users/me/inbox")
    assert resp.json()["data"]["count"] == 1
    # include_archived shows it
    resp = client.get("/api/users/me/inbox?include_archived=true")
    assert resp.json()["data"]["count"] == 2


def test_notification_prefs_flutter_shape_roundtrip():
    """PUT → GET with the exact Flutter shape."""
    import asyncio
    from digitorn.core.inbox import InboxStore

    factory = asyncio.new_event_loop().run_until_complete(_init_db_sqlite_memory())
    inbox_store = InboxStore(factory)

    from digitorn.core.api.user import router
    app = FastAPI()
    app.state.inbox_store = inbox_store
    app.state.app_manager = SimpleNamespace(get=lambda _: None, _deployed={})
    _make_auth_middleware(app, "alice", ["user"])
    app.include_router(router)

    client = TestClient(app)

    # PUT with Flutter flat shape
    prefs = {
        "events": {"session.completed": ["desktop", "push"]},
        "quiet_hours": {"start": 22, "end": 7, "tz": "Europe/Paris"},
        "channels": {"email": "alice@example.com"},
    }
    resp = client.put("/api/users/me/notification-prefs", json=prefs)
    assert resp.status_code == 200, resp.text

    # GET
    resp = client.get("/api/users/me/notification-prefs")
    assert resp.status_code == 200
    got = resp.json()["data"]
    assert got["quiet_hours"]["start"] == 22
    assert got["channels"]["email"] == "alice@example.com"


def test_usage_endpoint_returns_expected_shape():
    """GET /api/users/me/usage returns all expected fields."""
    import asyncio
    from digitorn.core.usage import QuotaStore, UsageStore

    factory = asyncio.new_event_loop().run_until_complete(_init_db_sqlite_memory())
    usage_store = UsageStore(factory)
    quota_store = QuotaStore(factory, usage_store=usage_store)

    # Pre-seed some usage
    async def _seed():
        await usage_store.record(
            user_id="alice", app_id="digitorn-code", session_id="s1",
            provider="anthropic", model="claude-sonnet-4-5",
            prompt_tokens=1000, completion_tokens=500,
        )
        await usage_store.record(
            user_id="alice", app_id="digitorn-chat", session_id="s2",
            provider="openai", model="gpt-4o",
            prompt_tokens=500, completion_tokens=200,
        )
    asyncio.new_event_loop().run_until_complete(_seed())

    from digitorn.core.api.user import router
    app = FastAPI()
    app.state.usage_store = usage_store
    app.state.quota_store = quota_store
    app.state.inbox_store = None
    app.state.app_manager = SimpleNamespace(get=lambda _: None, _deployed={})
    _make_auth_middleware(app, "alice", ["user"])
    app.include_router(router)

    client = TestClient(app)
    resp = client.get("/api/users/me/usage")
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert "cost" in data
    assert data["cost"]["currency"] == "USD"
    assert data["cost"]["this_month"] > 0
    assert "by_model" in data["cost"]
    assert "tokens_this_month" in data
    assert data["tokens_this_month"]["total"] == 2200
    assert "tokens_timeseries_24h" in data
    assert len(data["tokens_timeseries_24h"]) == 24
    assert "tokens_timeseries_30d" in data
    assert len(data["tokens_timeseries_30d"]) == 30
    assert "by_app" in data
    assert len(data["by_app"]) == 2
    # Sorted by cost desc
    assert data["by_app"][0]["cost_usd"] >= data["by_app"][-1]["cost_usd"]
    # Quota is None by default
    assert data["quota"] is None


def test_admin_quotas_crud_gated():
    """POST/GET/DELETE /api/admin/quotas with admin gate."""
    import asyncio
    from digitorn.core.usage import QuotaStore, UsageStore

    factory = asyncio.new_event_loop().run_until_complete(_init_db_sqlite_memory())
    usage_store = UsageStore(factory)
    quota_store = QuotaStore(factory, usage_store=usage_store)

    from digitorn.core.api.user import admin_router
    app = FastAPI()
    app.state.quota_store = quota_store
    app.state.usage_store = usage_store
    app.state.inbox_store = None
    app.state.app_manager = SimpleNamespace(get=lambda _: None, _deployed={})
    _make_auth_middleware(app, "admin", ["*"])
    app.include_router(admin_router)

    client = TestClient(app)
    # Create
    resp = client.post("/api/admin/quotas", json={
        "scope_type": "user",
        "scope_id": "alice",
        "period": "month",
        "tokens_limit": 1_000_000,
    })
    assert resp.status_code == 200, resp.text
    quota_id = resp.json()["data"]["id"]

    # List
    resp = client.get("/api/admin/quotas")
    assert resp.status_code == 200
    assert resp.json()["data"]["count"] == 1

    # Delete
    resp = client.delete(f"/api/admin/quotas/{quota_id}")
    assert resp.status_code == 200


def test_admin_quotas_refused_for_non_admin():
    """Non-admin can't POST to /api/admin/quotas."""
    import asyncio
    from digitorn.core.usage import QuotaStore, UsageStore

    factory = asyncio.new_event_loop().run_until_complete(_init_db_sqlite_memory())
    usage_store = UsageStore(factory)
    quota_store = QuotaStore(factory, usage_store=usage_store)

    from digitorn.core.api.user import admin_router
    app = FastAPI()
    app.state.quota_store = quota_store
    app.state.usage_store = usage_store
    app.state.app_manager = SimpleNamespace(get=lambda _: None, _deployed={})
    _make_auth_middleware(app, "alice", ["user"])  # non-admin
    app.include_router(admin_router)

    client = TestClient(app)
    resp = client.post("/api/admin/quotas", json={
        "scope_type": "user",
        "scope_id": "alice",
        "period": "month",
        "tokens_limit": 1_000_000,
    })
    assert resp.status_code == 403


# ════════════════════════════════════════════════════════════════════
# MCP admin gate
# ════════════════════════════════════════════════════════════════════


def test_mcp_install_refused_for_non_admin():
    """POST /api/mcp/servers requires admin."""
    from digitorn.core.api.mcp import router
    app = FastAPI()
    _make_auth_middleware(app, "alice", ["user"])
    app.include_router(router)

    client = TestClient(app)
    resp = client.post("/api/mcp/servers", json={
        "server_id": "github",
        "config": {"token": "ghp_test"},
    })
    assert resp.status_code == 403


def test_mcp_catalog_browse_public():
    """GET /api/mcp/catalog works for regular users."""
    from digitorn.core.api.mcp import router
    app = FastAPI()
    _make_auth_middleware(app, "alice", ["user"])
    app.include_router(router)

    client = TestClient(app)
    resp = client.get("/api/mcp/catalog")
    assert resp.status_code == 200
    data = resp.json()
    assert data["count"] >= 20
    entries = data["entries"]
    # Every entry has icon + category from the new fallback map
    for e in entries[:5]:
        assert "icon" in e
        assert "category" in e


def test_mcp_catalog_get_entry():
    """GET /api/mcp/catalog/{server_id} returns full metadata."""
    from digitorn.core.api.mcp import router
    app = FastAPI()
    _make_auth_middleware(app, "alice", ["user"])
    app.include_router(router)

    client = TestClient(app)
    resp = client.get("/api/mcp/catalog/github")
    assert resp.status_code == 200
    data = resp.json()
    assert data["server_id"] == "github"
    assert "env_mapping" in data
    assert "key_descriptions" in data
    assert "required_fields" in data
    assert "has_oauth" in data


# ════════════════════════════════════════════════════════════════════
# Discovery prompt-preview
# ════════════════════════════════════════════════════════════════════


def test_prompt_preview_inline_content(tmp_path: Path):
    """POST /api/discovery/prompt-preview with inline content."""
    from digitorn.core.api.discovery import router
    app = FastAPI()
    # Discovery router needs a module registry in state
    from digitorn.modules.registry import ModuleRegistry
    app.state.registry = ModuleRegistry()
    _make_auth_middleware(app, "alice", ["user"])
    app.include_router(router)

    client = TestClient(app)
    resp = client.post("/api/discovery/prompt-preview", json={
        "content": "Hello {{app.name}}!",
        "variables": {"_app_name": "MyApp"},
    })
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert "MyApp" in data["compiled_text"]
    assert data["token_estimate"] > 0


def test_prompt_preview_from_bundle(tmp_path: Path):
    """POST /api/discovery/prompt-preview with bundle_dir + prompt_name."""
    # Build a minimal bundle
    bundle = tmp_path / "my-app"
    bundle.mkdir()
    (bundle / "prompts").mkdir()
    (bundle / "prompts" / "system.md").write_text(
        "---\nversion: 1\n---\nYou are a helpful assistant.",
        encoding="utf-8",
    )

    from digitorn.core.api.discovery import router
    app = FastAPI()
    from digitorn.modules.registry import ModuleRegistry
    app.state.registry = ModuleRegistry()
    _make_auth_middleware(app, "alice", ["user"])
    app.include_router(router)

    client = TestClient(app)
    resp = client.post("/api/discovery/prompt-preview", json={
        "bundle_dir": str(bundle),
        "prompt_name": "system",
    })
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert "helpful assistant" in data["compiled_text"]
    assert data["frontmatter"].get("prompt.system", {}).get("version") == 1


def test_prompt_preview_missing_bundle_dir_404():
    """POST /api/discovery/prompt-preview with invalid bundle_dir."""
    from digitorn.core.api.discovery import router
    app = FastAPI()
    from digitorn.modules.registry import ModuleRegistry
    app.state.registry = ModuleRegistry()
    _make_auth_middleware(app, "alice", ["user"])
    app.include_router(router)

    client = TestClient(app)
    resp = client.post("/api/discovery/prompt-preview", json={
        "bundle_dir": "/nonexistent/path",
        "prompt_name": "system",
    })
    assert resp.status_code == 404


def test_prompt_preview_rejects_empty_body():
    """Neither bundle nor content → 400."""
    from digitorn.core.api.discovery import router
    app = FastAPI()
    from digitorn.modules.registry import ModuleRegistry
    app.state.registry = ModuleRegistry()
    _make_auth_middleware(app, "alice", ["user"])
    app.include_router(router)

    client = TestClient(app)
    resp = client.post("/api/discovery/prompt-preview", json={})
    assert resp.status_code == 400


# ════════════════════════════════════════════════════════════════════
# Packages + per-user scoping integration
# ════════════════════════════════════════════════════════════════════


def _build_packages_app(user_id: str, permissions: list[str], tmp_path: Path):
    """Build a packages router app with a real registry + install
    flow backed by SQLite :memory:."""
    import asyncio
    from digitorn.core.packages.registry import PackageRegistry, Scope
    from digitorn.core.packages.install import InstallFlow
    from digitorn.core.packages.sources.local import LocalSource

    factory = asyncio.new_event_loop().run_until_complete(_init_db_sqlite_memory())
    registry = PackageRegistry(factory)
    flow = InstallFlow(
        registry=registry,
        source_map={"local": LocalSource()},
        install_root=tmp_path / "system_packages",
        user_install_root=tmp_path / "user_homes",
    )

    from digitorn.core.api.packages import router
    app = FastAPI()
    app.state.package_registry = registry
    app.state.package_install_flow = flow
    app.state.app_manager = SimpleNamespace(
        get=lambda _: None,
        undeploy=lambda _: None,
        _deployed={},
    )
    _make_auth_middleware(app, user_id, permissions)
    app.include_router(router)
    return app, registry, flow


def _make_source_package(src_dir: Path, package_id: str, version: str = "1.0.0"):
    """Create a minimal valid source package directory."""
    src_dir.mkdir(parents=True, exist_ok=True)
    (src_dir / "app.yaml").write_text(
        f"""
app:
  app_id: {package_id}
  name: {package_id.replace("-", " ").title()}
  version: "{version}"
  description: "Test package {package_id}"

modules:
  filesystem:
    config: {{}}
""".strip(),
        encoding="utf-8",
    )
    (src_dir / "package.toml").write_text(
        f"""
[package]
id = "{package_id}"
name = "{package_id}"
version = "{version}"
description = "Test package {package_id}"
license = "MIT"

[package.permissions]
risk_level = "low"

[package.requirements]
modules = ["filesystem"]
""".strip(),
        encoding="utf-8",
    )


def test_packages_list_filters_by_caller(tmp_path: Path):
    """Alice sees her user + system; Bob only sees system."""
    import asyncio
    from digitorn.core.packages.registry import Scope

    # Alice's app (backed by her own registry+flow)
    app_alice, registry, flow = _build_packages_app("alice", ["user"], tmp_path)

    # Pre-seed: 1 system package + 1 user package for alice + 1 user package for bob
    async def _seed():
        await registry.create(
            package_id="digitorn-chat",
            source_type="builtin",
            source_uri="bundle://digitorn/digitorn-chat",
            version="1.0.0",
            hash="h_sys",
            install_dir=str(tmp_path / "system_packages" / "digitorn-chat"),
            manifest={"package": {"id": "digitorn-chat", "name": "Digitorn Chat"}},
            scope=Scope.SYSTEM,
        )
        await registry.create(
            package_id="alice-app",
            source_type="local",
            source_uri="/tmp/alice-app",
            version="1.0.0",
            hash="h_alice",
            install_dir=str(tmp_path / "user_homes" / "alice" / "packages" / "alice-app"),
            manifest={"package": {"id": "alice-app", "name": "Alice App"}},
            scope=Scope.USER,
            owner_user_id="alice",
        )
        await registry.create(
            package_id="bob-app",
            source_type="local",
            source_uri="/tmp/bob-app",
            version="1.0.0",
            hash="h_bob",
            install_dir=str(tmp_path / "user_homes" / "bob" / "packages" / "bob-app"),
            manifest={"package": {"id": "bob-app", "name": "Bob App"}},
            scope=Scope.USER,
            owner_user_id="bob",
        )
    asyncio.new_event_loop().run_until_complete(_seed())

    client = TestClient(app_alice)
    resp = client.get("/api/packages")
    assert resp.status_code == 200
    pkgs = resp.json()["data"]["packages"]
    ids = {p["package_id"] for p in pkgs}
    # Alice sees digitorn-chat (system) + alice-app (her own)
    # She does NOT see bob-app
    assert "digitorn-chat" in ids
    assert "alice-app" in ids
    assert "bob-app" not in ids


def test_packages_list_shadow_collapse(tmp_path: Path):
    """If a user installs their own version of a system package,
    the API collapses to the user version."""
    import asyncio
    from digitorn.core.packages.registry import Scope

    app_alice, registry, _ = _build_packages_app("alice", ["user"], tmp_path)

    async def _seed():
        await registry.create(
            package_id="digitorn-chat",
            source_type="builtin",
            source_uri="bundle://digitorn/digitorn-chat",
            version="1.0.0",
            hash="h_sys",
            install_dir=str(tmp_path / "sys" / "digitorn-chat"),
            manifest={"package": {"id": "digitorn-chat"}},
            scope=Scope.SYSTEM,
        )
        await registry.create(
            package_id="digitorn-chat",
            source_type="local",
            source_uri="/tmp/alice-chat",
            version="2.0.0-alice",
            hash="h_alice",
            install_dir=str(tmp_path / "users" / "alice" / "packages" / "digitorn-chat"),
            manifest={"package": {"id": "digitorn-chat"}},
            scope=Scope.USER,
            owner_user_id="alice",
        )
    asyncio.new_event_loop().run_until_complete(_seed())

    client = TestClient(app_alice)
    resp = client.get("/api/packages")
    pkgs = resp.json()["data"]["packages"]
    # Exactly one row for digitorn-chat
    chat_rows = [p for p in pkgs if p["package_id"] == "digitorn-chat"]
    assert len(chat_rows) == 1
    # And it's the user version
    assert chat_rows[0]["scope"] == "user"
    assert chat_rows[0]["version"] == "2.0.0-alice"


def test_packages_install_scope_system_refused_for_non_admin(tmp_path: Path):
    """Non-admin installing with scope=system → 403."""
    app, _, _ = _build_packages_app("alice", ["user"], tmp_path)

    # Make a source package
    src = tmp_path / "src" / "my-app"
    _make_source_package(src, "my-app")

    client = TestClient(app)
    resp = client.post("/api/packages/install", json={
        "source_type": "local",
        "source_uri": str(src),
        "accept_permissions": True,
        "scope": "system",
    })
    assert resp.status_code == 403
    assert "admin" in resp.json().get("detail", "").lower()


def test_packages_install_user_scope_succeeds(tmp_path: Path):
    """Non-admin install with scope=user succeeds, writes to user dir."""
    app, registry, _ = _build_packages_app("alice", ["user"], tmp_path)

    src = tmp_path / "src" / "my-app"
    _make_source_package(src, "my-app")

    client = TestClient(app)
    # First call - without accept_permissions → 409 perms required
    resp = client.post("/api/packages/install", json={
        "source_type": "local",
        "source_uri": str(src),
        "accept_permissions": False,
        "scope": "user",
    })
    assert resp.status_code == 409
    detail = resp.json().get("detail", {})
    assert detail.get("error") == "permissions_required"

    # Second call - accept → success
    resp = client.post("/api/packages/install", json={
        "source_type": "local",
        "source_uri": str(src),
        "accept_permissions": True,
        "scope": "user",
    })
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["scope"] == "user"
    assert data["owner_user_id"] == "alice"
    assert "alice" in data["install_dir"]  # User dir path


def test_packages_install_admin_scope_system(tmp_path: Path):
    """Admin can install with scope=system."""
    app, registry, _ = _build_packages_app("admin", ["*"], tmp_path)

    src = tmp_path / "src" / "system-app"
    _make_source_package(src, "system-app")

    client = TestClient(app)
    resp = client.post("/api/packages/install", json={
        "source_type": "local",
        "source_uri": str(src),
        "accept_permissions": True,
        "scope": "system",
    })
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["scope"] == "system"
    assert data["owner_user_id"] is None


def test_packages_uninstall_ownership_check(tmp_path: Path):
    """Alice can't uninstall bob's package."""
    import asyncio
    from digitorn.core.packages.registry import Scope

    # Bob installs a package
    app_bob, registry, _ = _build_packages_app("bob", ["user"], tmp_path)
    src = tmp_path / "src" / "bob-secret"
    _make_source_package(src, "bob-secret")

    client_bob = TestClient(app_bob)
    resp = client_bob.post("/api/packages/install", json={
        "source_type": "local",
        "source_uri": str(src),
        "accept_permissions": True,
        "scope": "user",
    })
    assert resp.status_code == 200

    # Now switch to Alice (same registry)
    from digitorn.core.api.packages import router
    app_alice = FastAPI()
    app_alice.state.package_registry = registry
    app_alice.state.package_install_flow = app_bob.state.package_install_flow
    app_alice.state.app_manager = SimpleNamespace(
        get=lambda _: None,
        undeploy=lambda _: None,
        _deployed={},
    )
    _make_auth_middleware(app_alice, "alice", ["user"])
    app_alice.include_router(router)

    client_alice = TestClient(app_alice)
    # Alice can't even SEE bob's package
    resp = client_alice.get("/api/packages/bob-secret")
    assert resp.status_code == 404


def test_packages_admin_can_see_all_with_flag(tmp_path: Path):
    """GET /api/packages?all=true returns everyone's installs
    when caller is admin."""
    import asyncio
    from digitorn.core.packages.registry import Scope

    app, registry, _ = _build_packages_app("admin", ["*"], tmp_path)

    async def _seed():
        await registry.create(
            package_id="alice-only", source_type="local",
            source_uri="/tmp/a", version="1.0", hash="",
            install_dir="/tmp/a", manifest={"package": {"id": "alice-only"}},
            scope=Scope.USER, owner_user_id="alice",
        )
        await registry.create(
            package_id="bob-only", source_type="local",
            source_uri="/tmp/b", version="1.0", hash="",
            install_dir="/tmp/b", manifest={"package": {"id": "bob-only"}},
            scope=Scope.USER, owner_user_id="bob",
        )
    asyncio.new_event_loop().run_until_complete(_seed())

    client = TestClient(app)
    # Without all=true → admin sees only system (empty here)
    resp = client.get("/api/packages")
    ids = {p["package_id"] for p in resp.json()["data"]["packages"]}
    assert "alice-only" not in ids
    assert "bob-only" not in ids

    # With all=true → admin sees everything
    resp = client.get("/api/packages?all=true")
    ids = {p["package_id"] for p in resp.json()["data"]["packages"]}
    assert "alice-only" in ids
    assert "bob-only" in ids


def test_packages_install_collision_detection(tmp_path: Path):
    """Installing the same package twice at the same scope → 409
    package_already_installed."""
    app, _, _ = _build_packages_app("alice", ["user"], tmp_path)

    src = tmp_path / "src" / "twice"
    _make_source_package(src, "twice")

    client = TestClient(app)
    # First install
    resp = client.post("/api/packages/install", json={
        "source_type": "local",
        "source_uri": str(src),
        "accept_permissions": True,
        "scope": "user",
    })
    assert resp.status_code == 200

    # Second install at the same scope → collision
    resp = client.post("/api/packages/install", json={
        "source_type": "local",
        "source_uri": str(src),
        "accept_permissions": True,
        "scope": "user",
    })
    assert resp.status_code == 409
    assert "already_installed" in resp.json().get("detail", {}).get("error", "")


def test_packages_bob_cant_see_alice_inbox(tmp_path: Path):
    """Isolation check: Bob's inbox request doesn't leak Alice's items."""
    import asyncio
    from digitorn.core.inbox import InboxStore

    factory = asyncio.new_event_loop().run_until_complete(_init_db_sqlite_memory())
    inbox_store = InboxStore(factory)

    # Alice has 3 items, Bob has 1
    async def _seed():
        for i in range(3):
            await inbox_store.create_item(
                user_id="alice", kind="session.completed",
                title=f"Alice {i}", app_id="my-app",
            )
        await inbox_store.create_item(
            user_id="bob", kind="session.completed",
            title="Bob only", app_id="my-app",
        )
    asyncio.new_event_loop().run_until_complete(_seed())

    from digitorn.core.api.user import router
    # Build Alice's app
    app_alice = FastAPI()
    app_alice.state.inbox_store = inbox_store
    app_alice.state.app_manager = SimpleNamespace(get=lambda _: None, _deployed={})
    _make_auth_middleware(app_alice, "alice", ["user"])
    app_alice.include_router(router)

    client_alice = TestClient(app_alice)
    resp = client_alice.get("/api/users/me/inbox")
    items = resp.json()["data"]["items"]
    assert len(items) == 3
    assert all("Alice" in i["title"] for i in items)

    # Build Bob's app
    app_bob = FastAPI()
    app_bob.state.inbox_store = inbox_store
    app_bob.state.app_manager = SimpleNamespace(get=lambda _: None, _deployed={})
    _make_auth_middleware(app_bob, "bob", ["user"])
    app_bob.include_router(router)

    client_bob = TestClient(app_bob)
    resp = client_bob.get("/api/users/me/inbox")
    items = resp.json()["data"]["items"]
    assert len(items) == 1
    assert items[0]["title"] == "Bob only"


def test_usage_tracks_per_user_isolation(tmp_path: Path):
    """Alice's usage is separate from Bob's."""
    import asyncio
    from digitorn.core.usage import UsageStore, QuotaStore

    factory = asyncio.new_event_loop().run_until_complete(_init_db_sqlite_memory())
    usage_store = UsageStore(factory)
    quota_store = QuotaStore(factory, usage_store=usage_store)

    async def _seed():
        for _ in range(5):
            await usage_store.record(
                user_id="alice", app_id="app1", session_id="s",
                provider="anthropic", model="claude-sonnet-4-5",
                prompt_tokens=1000, completion_tokens=500,
            )
        await usage_store.record(
            user_id="bob", app_id="app1", session_id="s",
            provider="anthropic", model="claude-sonnet-4-5",
            prompt_tokens=100, completion_tokens=50,
        )
    asyncio.new_event_loop().run_until_complete(_seed())

    from digitorn.core.api.user import router
    app_alice = FastAPI()
    app_alice.state.usage_store = usage_store
    app_alice.state.quota_store = quota_store
    app_alice.state.app_manager = SimpleNamespace(get=lambda _: None, _deployed={})
    _make_auth_middleware(app_alice, "alice", ["user"])
    app_alice.include_router(router)
    client = TestClient(app_alice)
    resp = client.get("/api/users/me/usage")
    data = resp.json()["data"]
    # Alice: 5 × (1000+500) = 7500 tokens
    assert data["tokens_this_month"]["total"] == 7500

    app_bob = FastAPI()
    app_bob.state.usage_store = usage_store
    app_bob.state.quota_store = quota_store
    app_bob.state.app_manager = SimpleNamespace(get=lambda _: None, _deployed={})
    _make_auth_middleware(app_bob, "bob", ["user"])
    app_bob.include_router(router)
    client = TestClient(app_bob)
    resp = client.get("/api/users/me/usage")
    data = resp.json()["data"]
    # Bob: 1 × (100+50) = 150 tokens
    assert data["tokens_this_month"]["total"] == 150


def test_quota_enforcement_via_api(tmp_path: Path):
    """Set a tight quota for Alice via admin → Alice sees it in /usage."""
    import asyncio
    from digitorn.core.usage import UsageStore, QuotaStore

    factory = asyncio.new_event_loop().run_until_complete(_init_db_sqlite_memory())
    usage_store = UsageStore(factory)
    quota_store = QuotaStore(factory, usage_store=usage_store)

    async def _seed():
        await quota_store.upsert_quota(
            scope_type="user", scope_id="alice",
            period="month", tokens_limit=10000,
            set_by="admin",
        )
        await usage_store.record(
            user_id="alice", app_id="app1", session_id="s",
            provider="anthropic", model="claude-sonnet-4-5",
            prompt_tokens=2000, completion_tokens=1000,
        )
    asyncio.new_event_loop().run_until_complete(_seed())

    from digitorn.core.api.user import router
    app = FastAPI()
    app.state.usage_store = usage_store
    app.state.quota_store = quota_store
    app.state.app_manager = SimpleNamespace(get=lambda _: None, _deployed={})
    _make_auth_middleware(app, "alice", ["user"])
    app.include_router(router)

    client = TestClient(app)
    resp = client.get("/api/users/me/usage")
    data = resp.json()["data"]
    assert data["quota"] is not None
    assert data["quota"]["tokens_per_month"] == 10000
    assert data["quota"]["tokens_used_this_month"] == 3000
    assert data["quota"]["tokens_remaining"] == 7000


def test_profile_routes_roundtrip(tmp_path: Path):
    """GET profile → PUT display_name → GET again → see update."""
    import asyncio

    factory = asyncio.new_event_loop().run_until_complete(_init_db_sqlite_memory())

    # Seed a User row directly
    async def _seed():
        from digitorn.core.models import User
        async with factory() as db:
            user = User(
                id="alice",
                external_id="alice",
                provider="local",
                email="alice@example.com",
                display_name="Alice Original",
            )
            db.add(user)
            await db.commit()
    asyncio.new_event_loop().run_until_complete(_seed())

    from digitorn.core.api.user import router
    app = FastAPI()
    app.state.app_manager = SimpleNamespace(get=lambda _: None, _deployed={})
    app.state.inbox_store = None
    _make_auth_middleware(app, "alice", ["user"])
    app.include_router(router)

    client = TestClient(app)
    resp = client.get("/api/users/me/profile")
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["display_name"] == "Alice Original"
    assert resp.json()["data"]["email"] == "alice@example.com"

    resp = client.put("/api/users/me/profile", json={
        "display_name": "Alice Updated",
    })
    assert resp.status_code == 200
    assert resp.json()["data"]["display_name"] == "Alice Updated"

    resp = client.get("/api/users/me/profile")
    assert resp.json()["data"]["display_name"] == "Alice Updated"
    # Email unchanged
    assert resp.json()["data"]["email"] == "alice@example.com"


def test_apps_secondary_routes_respect_user_scoping(tmp_path: Path):
    """The secondary app routes (status, triggers, quota, etc.)
    now thread user_id through manager.get() so user-scoped
    apps are visible to their owners AND invisible to others.
    This proves the Option-2 sweep worked end-to-end."""

    # Build one user-scoped deployed app for Alice
    class _FakeIndex:
        total_tools = 0
        total_categories = 0

    class _FakeCompiled:
        class meta:
            app_id = "alice-app"
            name = "Alice App"
            version = "1.0"
            description = "Alice's personal app"
            icon = ""
            color = ""
            category = "personal"
            author = "alice"
            tags = []
            quick_prompts = []
        module_ids = []
        class execution:
            mode = "interactive"
            triggers = []
            session_mode = "mono"
            max_sessions_per_user = 10
            payload_schema = None
            greeting = None
            workspace_mode = "auto"
            workspace = ""

    class _FakeSummary:
        def __call__(self):
            return {"app_id": "alice-app", "scope": "user"}

    deployed_alice = SimpleNamespace(
        app_id="alice-app",
        compiled=_FakeCompiled(),
        contexts={},
        modules={},
        context_builder=None,
        bootstrap_result=None,
        scope="user",
        owner_user_id="alice",
        builtin=False,
        entry_context=SimpleNamespace(provider=SimpleNamespace(model="")),
        summary=lambda: {
            "app_id": "alice-app",
            "name": "Alice App",
            "scope": "user",
            "owner_user_id": "alice",
        },
        index=_FakeIndex(),
    )

    # Fake manager that stores the app at the scoped key
    _deployed_map = {"user:alice:alice-app": deployed_alice}

    def _get(app_id, *, user_id=None):
        if user_id:
            key = f"user:{user_id}:{app_id}"
            if key in _deployed_map:
                return _deployed_map[key]
        key = f"system::{app_id}"
        if key in _deployed_map:
            return _deployed_map[key]
        return _deployed_map.get(app_id)

    def _is_deployed(app_id, *, user_id=None):
        return _get(app_id, user_id=user_id) is not None

    manager = SimpleNamespace(
        get=_get,
        is_deployed=_is_deployed,
        _deployed=_deployed_map,
        list_apps=lambda user_id=None: [deployed_alice.summary()]
            if user_id == "alice" else [],
        count_sessions=lambda *a, **k: 0,
        list_sessions=lambda *a, **k: [],
        is_session_active=lambda *a, **k: False,
    )

    from digitorn.core.api.apps import router
    # Alice's view
    app_alice = FastAPI()
    app_alice.state.app_manager = manager
    _make_auth_middleware(app_alice, "alice", ["user"])
    app_alice.include_router(router)

    client_alice = TestClient(app_alice)

    # GET /api/apps - alice sees her app
    resp = client_alice.get("/api/apps")
    assert resp.status_code == 200
    apps = resp.json()["data"]
    assert len(apps) == 1
    assert apps[0]["app_id"] == "alice-app"

    # GET /api/apps/alice-app → 200 (visible)
    resp = client_alice.get("/api/apps/alice-app")
    assert resp.status_code == 200

    # Bob's view - same manager, different user
    app_bob = FastAPI()
    app_bob.state.app_manager = manager
    _make_auth_middleware(app_bob, "bob", ["user"])
    app_bob.include_router(router)
    client_bob = TestClient(app_bob)

    # GET /api/apps - bob sees nothing (alice's app is user-scoped)
    resp = client_bob.get("/api/apps")
    assert resp.json()["data"] == []

    # GET /api/apps/alice-app → 404 (bob can't see alice's app)
    resp = client_bob.get("/api/apps/alice-app")
    assert resp.status_code == 404

    # Secondary routes: bob trying to read alice's triggers/sessions
    # also 404 because the internal _is_deployed() check fails for him.
    resp = client_bob.get("/api/apps/alice-app/triggers")
    assert resp.status_code == 404, (
        f"bob should not see alice's triggers, got {resp.status_code}"
    )

    resp = client_bob.get("/api/apps/alice-app/sessions")
    assert resp.status_code == 404, (
        f"bob should not see alice's sessions, got {resp.status_code}"
    )


def test_apps_file_listing_guards_against_traversal(tmp_path: Path):
    """GET /api/apps/{id}/files?subdir=../../etc → 400 (path escape)."""
    # Build a minimal deployed app with a bundle dir
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    (bundle / "prompts").mkdir()
    (bundle / "prompts" / "system.md").write_text("hello", encoding="utf-8")
    (bundle / "app.yaml").write_text(
        "app:\n  app_id: my-app\n  name: My App\n", encoding="utf-8",
    )

    # Build a fake DeployedApp
    class _FakeCompiled:
        class meta:
            app_id = "my-app"
            name = "My App"
            icon = ""
            color = ""
    deployed_app = SimpleNamespace(
        app_id="my-app",
        compiled=SimpleNamespace(
            meta=SimpleNamespace(
                app_id="my-app", name="My App",
                icon="", color="",
            ),
            source_path=str(bundle / "app.yaml"),
        ),
    )

    class _BundleStore:
        def app_dir(self, app_id): return str(bundle)

    manager = SimpleNamespace(
        get=lambda app_id, **kw: deployed_app if app_id == "my-app" else None,
        is_deployed=lambda app_id, **kw: app_id == "my-app",
        _bundle_store=_BundleStore(),
        _deployed={"system::my-app": deployed_app},
    )

    from digitorn.core.api.apps import router
    app = FastAPI()
    app.state.app_manager = manager
    _make_auth_middleware(app, "alice", ["user"])
    app.include_router(router)

    client = TestClient(app)

    # Valid subdir
    resp = client.get("/api/apps/my-app/files?subdir=prompts")
    assert resp.status_code == 200, resp.text
    entries = resp.json()["data"]["entries"]
    assert any(e["name"] == "system.md" for e in entries)

    # Path traversal attempt
    resp = client.get("/api/apps/my-app/files?subdir=../../../etc")
    assert resp.status_code in (400, 404)  # rejected

    # Asset fetch
    resp = client.get("/api/apps/my-app/assets/prompts/system.md")
    assert resp.status_code == 200
    assert resp.text == "hello"

    # Path traversal on asset fetch
    resp = client.get("/api/apps/my-app/assets/..%2F..%2Fetc%2Fpasswd")
    assert resp.status_code in (400, 404)


def test_cross_app_sessions_list_empty(tmp_path: Path):
    """GET /api/users/me/sessions returns an empty list when no sessions."""
    from digitorn.core.api.user import router
    app = FastAPI()
    app.state.app_manager = SimpleNamespace(
        _deployed={},
        get=lambda *a, **k: None,
        is_session_active=lambda *a, **k: False,
        list_sessions=lambda *a, **k: [],
    )
    app.state.inbox_store = None
    _make_auth_middleware(app, "alice", ["user"])
    app.include_router(router)

    client = TestClient(app)
    resp = client.get("/api/users/me/sessions")
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["total"] == 0
    assert data["sessions"] == []


def test_credential_auth_required_flow_via_store(tmp_path: Path):
    """End-to-end: user creates credential, tries to use from an
    app without grant → CredentialAuthRequired with candidates,
    then creates grant → runtime resolves correctly."""
    import asyncio
    from digitorn.core.credentials import (
        CredentialStore, CredentialAuthRequired, Cipher,
        load_or_create_master_key, resolve_runtime_secrets_in_value,
    )

    factory = asyncio.new_event_loop().run_until_complete(_init_db_sqlite_memory())
    cipher = Cipher(load_or_create_master_key())
    store = CredentialStore(factory, cipher)

    async def _run():
        # Alice creates a credential
        cred = await store.upsert_user_credential(
            user_id="alice",
            provider_name="DEEPSEEK_API_KEY",
            provider_type="api_key",
            fields={"DEEPSEEK_API_KEY": "sk-real"},
        )
        # Try to use it without grant → auth required
        try:
            await resolve_runtime_secrets_in_value(
                "{{env.DEEPSEEK_API_KEY}}",
                store=store,
                user_id="alice",
                app_id="digitorn-code",
            )
            assert False, "should have raised"
        except CredentialAuthRequired as exc:
            assert exc.provider == "DEEPSEEK_API_KEY"
            assert exc.app_id == "digitorn-code"
            assert len(exc.candidates) == 1

        # Grant
        await store.create_grant(
            credential_id=cred["id"],
            user_id="alice",
            app_id="digitorn-code",
        )

        # Retry → resolves cleanly
        resolved = await resolve_runtime_secrets_in_value(
            "{{env.DEEPSEEK_API_KEY}}",
            store=store,
            user_id="alice",
            app_id="digitorn-code",
        )
        assert resolved == "sk-real"

    asyncio.new_event_loop().run_until_complete(_run())


def test_session_resolver_rejects_other_users(tmp_path: Path):
    """Bob can't resolve alice's credentials by spoofing user_id."""
    import asyncio
    from digitorn.core.credentials import (
        CredentialStore, CredentialAuthRequired, Cipher,
        load_or_create_master_key, resolve_runtime_secrets_in_value,
    )

    factory = asyncio.new_event_loop().run_until_complete(_init_db_sqlite_memory())
    cipher = Cipher(load_or_create_master_key())
    store = CredentialStore(factory, cipher)

    async def _run():
        # Alice creates + grants
        cred = await store.upsert_user_credential(
            user_id="alice",
            provider_name="OPENAI",
            provider_type="api_key",
            fields={"OPENAI": "sk-alice"},
        )
        await store.create_grant(
            credential_id=cred["id"],
            user_id="alice",
            app_id="myapp",
        )

        # Bob tries to access his credentials in the same app -
        # he has NO credentials → Missing silently (passthrough)
        resolved = await resolve_runtime_secrets_in_value(
            "{{env.OPENAI}}",
            store=store,
            user_id="bob",
            app_id="myapp",
        )
        # Bob gets the passthrough template, NOT alice's key
        assert resolved == "{{env.OPENAI}}"
        assert "sk-alice" not in resolved

    asyncio.new_event_loop().run_until_complete(_run())


def test_packages_user_can_shadow_system(tmp_path: Path):
    """Alice can install her own 'my-app' even if admin has
    already installed 'my-app' at scope=system."""
    app_admin, registry, _ = _build_packages_app("admin", ["*"], tmp_path)

    src_system = tmp_path / "src" / "sys" / "my-app"
    _make_source_package(src_system, "my-app", version="1.0.0")

    # Admin installs system version
    client_admin = TestClient(app_admin)
    resp = client_admin.post("/api/packages/install", json={
        "source_type": "local",
        "source_uri": str(src_system),
        "accept_permissions": True,
        "scope": "system",
    })
    assert resp.status_code == 200

    # Alice installs her own version (same package_id)
    from digitorn.core.api.packages import router
    app_alice = FastAPI()
    app_alice.state.package_registry = registry
    app_alice.state.package_install_flow = app_admin.state.package_install_flow
    app_alice.state.app_manager = SimpleNamespace(
        get=lambda _: None,
        undeploy=lambda _: None,
        _deployed={},
    )
    _make_auth_middleware(app_alice, "alice", ["user"])
    app_alice.include_router(router)

    src_alice = tmp_path / "src" / "alice" / "my-app"
    _make_source_package(src_alice, "my-app", version="1.0.0-alice")

    client_alice = TestClient(app_alice)
    resp = client_alice.post("/api/packages/install", json={
        "source_type": "local",
        "source_uri": str(src_alice),
        "accept_permissions": True,
        "scope": "user",
    })
    # Should succeed - different scope
    assert resp.status_code == 200, resp.text

    # Alice sees her version via GET
    resp = client_alice.get("/api/packages/my-app")
    assert resp.status_code == 200
    assert resp.json()["data"]["scope"] == "user"
    assert resp.json()["data"]["version"] == "1.0.0-alice"

