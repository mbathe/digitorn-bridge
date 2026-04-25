"""End-to-end foundation test for the universal credentials system.

Walks every layer of the stack in a single process with an
in-memory SQLite database, without needing a running daemon:

1. Encryption round-trip
2. CredentialStore CRUD with encryption
3. 4-scope resolver order (per_app_per_user → per_user → per_app_shared → system_wide)
4. Handler validation (ApiKeyHandler regex)
5. YAML compile with credentials_schema — valid + invalid cases
6. Compile-time secret resolution via build_compile_secrets
7. Bootstrap env var import (one-shot, non-overwriting)

If any of these fails, the whole credentials foundation is broken and
nothing downstream can work — so this file is the single "does it
still work?" smoke test to run after any credentials-touching change.

Run with::

    py -3.12 tests/test_credentials_foundation.py
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import tempfile
from pathlib import Path

# Silence the verbose module loader output
logging.basicConfig(level=logging.ERROR)
logging.getLogger("digitorn").setLevel(logging.ERROR)

# Force UTF-8 for Windows consoles
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:
        pass

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "packages"))


# ────────────────────────────────────────────────────────────────────
# Test helpers
# ────────────────────────────────────────────────────────────────────


def _header(title: str) -> None:
    print(f"\n{'═' * 60}")
    print(f"  {title}")
    print(f"{'═' * 60}")


def _ok(label: str) -> None:
    print(f"  ✓ {label}")


def _fail(label: str, detail: str = "") -> None:
    print(f"  ✗ {label}")
    if detail:
        print(f"    → {detail}")
    raise AssertionError(f"{label}: {detail}")


# ────────────────────────────────────────────────────────────────────
# Setup — in-memory DB + ephemeral master key
# ────────────────────────────────────────────────────────────────────


async def setup_in_memory_store():
    """Build a CredentialStore backed by an in-memory SQLite."""
    from digitorn.core.config import get_settings
    from digitorn.core.database import Base, get_session_factory, init_db
    from digitorn.core.credentials import (
        Cipher,
        CredentialStore,
        load_or_create_master_key,
    )

    s = get_settings()
    s.database.url = "sqlite+aiosqlite:///:memory:"
    engine = await init_db(s)

    # Force create all tables — in-memory DB needs them bootstrapped
    from digitorn.core.models import Credential  # noqa: F401 — register the table

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    # Ephemeral master key in a tmp dir so we don't touch ~/.digitorn
    tmp = Path(tempfile.mkdtemp()) / "master.key"
    master = load_or_create_master_key(tmp)
    cipher = Cipher(master)
    store = CredentialStore(get_session_factory(), cipher)
    return store, master, tmp


# ────────────────────────────────────────────────────────────────────
# Tests
# ────────────────────────────────────────────────────────────────────


async def test_1_encryption() -> None:
    _header("1. Encryption round-trip")
    from digitorn.core.credentials import Cipher, load_or_create_master_key

    tmp = Path(tempfile.mkdtemp()) / "master.key"
    key = load_or_create_master_key(tmp)
    assert len(key) == 32, f"master key should be 32 bytes, got {len(key)}"
    _ok("master key generated (32 bytes)")

    cipher = Cipher(key)
    original = {"api_key": "sk-ant-1234567890abcdef", "org": "org-xyz"}
    ct, nonce = cipher.encrypt(original)
    assert len(nonce) == 12
    assert ct != b""
    _ok(f"encrypted {len(ct)} bytes, nonce {len(nonce)} bytes")

    decrypted = cipher.decrypt(ct, nonce)
    assert decrypted == original, f"roundtrip failed: {decrypted} != {original}"
    _ok("round-trip decryption matches original")

    # Persistence: reload the key from disk
    key2 = load_or_create_master_key(tmp)
    assert key2 == key
    _ok("master key persisted and reloads identically")


async def test_2_store_crud() -> None:
    _header("2. CredentialStore CRUD")
    from digitorn.core.credentials import Scope, Status

    store, _, _ = await setup_in_memory_store()

    # Create per_user credential
    stored = await store.upsert_credential(
        user_id="alice",
        app_id=None,
        provider_name="anthropic",
        provider_type="api_key",
        scope=Scope.PER_USER,
        fields={"api_key": "sk-ant-alice-key-abcdef1234"},
    )
    assert stored["provider_name"] == "anthropic"
    assert stored["scope"] == Scope.PER_USER
    assert stored["status"] == Status.FILLED
    assert "fields" not in stored  # no plaintext in non-decrypt response
    assert "api_key" in stored["display_metadata"]["masked_fields"]
    _ok(f"created per_user credential (masked: {stored['display_metadata']['masked_fields']['api_key']})")

    # Get without decrypt — should NOT leak plaintext
    fetched = await store.get_credential(
        user_id="alice", app_id=None, provider_name="anthropic",
    )
    assert fetched is not None
    assert "fields" not in fetched
    _ok("get(decrypt=False) returns no plaintext")

    # Get with decrypt — should return plaintext
    decrypted = await store.get_credential(
        user_id="alice", app_id=None, provider_name="anthropic",
        decrypt=True,
    )
    assert decrypted["fields"]["api_key"] == "sk-ant-alice-key-abcdef1234"
    _ok("get(decrypt=True) returns plaintext")

    # List for user
    creds = await store.list_credentials(user_id="alice")
    assert len(creds) == 1
    _ok(f"list_credentials returned {len(creds)} row(s)")

    # Delete
    deleted = await store.delete_credential(
        user_id="alice", app_id=None, provider_name="anthropic",
    )
    assert deleted is True
    gone = await store.get_credential(
        user_id="alice", app_id=None, provider_name="anthropic",
    )
    assert gone is None
    _ok("delete worked")


async def test_3_resolver_order() -> None:
    _header("3. 4-scope resolver order")
    from digitorn.core.credentials import Scope

    store, _, _ = await setup_in_memory_store()

    # Populate all 4 scopes with a distinguishable value for openai.api_key
    await store.upsert_credential(
        user_id=None, app_id=None,
        provider_name="openai", provider_type="api_key",
        scope=Scope.SYSTEM_WIDE,
        fields={"api_key": "sk-SYSTEM"},
    )
    await store.upsert_credential(
        user_id=None, app_id="myapp",
        provider_name="openai", provider_type="api_key",
        scope=Scope.PER_APP_SHARED,
        fields={"api_key": "sk-APP_SHARED"},
    )
    await store.upsert_credential(
        user_id="bob", app_id=None,
        provider_name="openai", provider_type="api_key",
        scope=Scope.PER_USER,
        fields={"api_key": "sk-BOB_USER"},
    )
    await store.upsert_credential(
        user_id="bob", app_id="myapp",
        provider_name="openai", provider_type="api_key",
        scope=Scope.PER_APP_PER_USER,
        fields={"api_key": "sk-BOB_OVERRIDE"},
    )

    # Resolution cases — walks the 4-scope order
    #
    # 1. bob + myapp → should hit per_app_per_user
    val = await store.resolve_field(
        provider_or_field="openai.api_key", user_id="bob", app_id="myapp",
    )
    assert val == "sk-BOB_OVERRIDE", f"expected sk-BOB_OVERRIDE, got {val}"
    _ok("(bob, myapp) → per_app_per_user wins")

    # 2. bob + otherapp → per_user
    val = await store.resolve_field(
        provider_or_field="openai.api_key", user_id="bob", app_id="otherapp",
    )
    assert val == "sk-BOB_USER", f"expected sk-BOB_USER, got {val}"
    _ok("(bob, otherapp) → per_user wins")

    # 3. alice (no per_user for her) + myapp → per_app_shared
    val = await store.resolve_field(
        provider_or_field="openai.api_key", user_id="alice", app_id="myapp",
    )
    assert val == "sk-APP_SHARED", f"expected sk-APP_SHARED, got {val}"
    _ok("(alice, myapp) → per_app_shared wins")

    # 4. alice + otherapp → system_wide
    val = await store.resolve_field(
        provider_or_field="openai.api_key", user_id="alice", app_id="otherapp",
    )
    assert val == "sk-SYSTEM", f"expected sk-SYSTEM, got {val}"
    _ok("(alice, otherapp) → system_wide wins")

    # 5. Legacy flat lookup: "openai" (single-field shortcut)
    val = await store.resolve_field(
        provider_or_field="openai", user_id="bob", app_id="myapp",
    )
    assert val == "sk-BOB_OVERRIDE"
    _ok("legacy flat lookup resolves the single field")


async def test_4_handler_validation() -> None:
    _header("4. Handler validation")
    from digitorn.core.credentials import ValidationError, default_registry

    handler = default_registry.get("api_key")
    assert handler.provider_type == "api_key"
    _ok("ApiKeyHandler registered")

    # Accept valid
    handler.validate_fields(
        {"api_key": "sk-abcdef1234567890"},
        [{"name": "api_key", "required": True,
          "validation_regex": r"^sk-[a-zA-Z0-9_-]{10,}$"}],
    )
    _ok("valid field accepted")

    # Reject missing required
    try:
        handler.validate_fields(
            {"api_key": ""},
            [{"name": "api_key", "required": True}],
        )
        _fail("empty required field should have raised")
    except ValidationError as exc:
        assert exc.field == "api_key"
        _ok(f"empty required → rejected ({exc.reason})")

    # Reject regex mismatch
    try:
        handler.validate_fields(
            {"api_key": "not-a-valid-key"},
            [{"name": "api_key", "required": True,
              "validation_regex": r"^sk-"}],
        )
        _fail("regex mismatch should have raised")
    except ValidationError as exc:
        _ok(f"regex mismatch → rejected ({exc.reason})")

    # OAuth scope enforcement is validated by the compiler, not the
    # handler — tested in test_5_yaml_compile.


async def test_5_yaml_compile() -> None:
    _header("5. YAML compile with credentials_schema")
    from digitorn.core.loader import load_modules
    from digitorn.core.app.compiler import AppYAMLCompiler
    from digitorn.core.app.errors import AppCompilationError
    from digitorn.modules.registry import ModuleRegistry

    reg = ModuleRegistry()
    load_modules(reg, load_all=True)

    # Happy path — 4 provider types all valid
    valid_yaml = """
app:
  app_id: t1
  name: T1
  description: x
modules:
  filesystem: {}
agents:
  - id: w
    role: worker
    brain: {provider: anthropic, model: claude-sonnet-4-5, config: {api_key: claude-code}}
execution:
  mode: one_shot
  entry_agent: w
  credentials_schema:
    required: true
    providers:
      - name: openai
        type: api_key
        scope: per_user
        fields:
          - name: api_key
            type: secret
            required: true
      - name: notion
        type: oauth2
        scope: per_user
        oauth_provider: notion
      - name: slack
        type: multi_field
        scope: per_user
        fields:
          - name: bot_token
            type: secret
            required: true
          - name: signing_secret
            type: secret
            required: true
      - name: main-db
        type: connection_string
        scope: per_app_shared
        fields:
          - name: url
            type: connection_string
            required: true
capabilities:
  grant: [{module: filesystem, actions: [read]}]
"""
    compiled = AppYAMLCompiler(reg).compile_string(valid_yaml, source="t1.yaml")
    cs = compiled.execution.credentials_schema
    assert cs is not None
    assert len(cs["providers"]) == 4
    _ok("4-provider valid schema compiles")

    # Invalid: OAuth with wrong scope
    invalid_yaml = valid_yaml.replace("scope: per_user\n        oauth_provider", "scope: per_app_shared\n        oauth_provider")
    try:
        AppYAMLCompiler(reg).compile_string(invalid_yaml, source="t2.yaml")
        _fail("OAuth per_app_shared should have been rejected")
    except AppCompilationError as exc:
        assert "oauth2 providers MUST use scope='per_user'" in str(exc)
        _ok("OAuth per_app_shared rejected by compiler")


async def test_6_compile_secret_resolver() -> None:
    _header("6. build_compile_secrets walks the store")
    from digitorn.core.credentials import Scope
    from digitorn.core.credentials.compile_resolver import build_compile_secrets

    store, _, _ = await setup_in_memory_store()

    # A system_wide anthropic key
    await store.upsert_credential(
        user_id=None, app_id=None,
        provider_name="anthropic", provider_type="api_key",
        scope=Scope.SYSTEM_WIDE,
        fields={"api_key": "sk-ant-SYSTEM"},
    )
    # A per_app_shared webhook secret for app 'myapp'
    await store.upsert_credential(
        user_id=None, app_id="myapp",
        provider_name="webhook", provider_type="api_key",
        scope=Scope.PER_APP_SHARED,
        fields={"secret": "wh-APP"},
    )
    # A per_user credential (should NOT show up at compile time)
    await store.upsert_credential(
        user_id="bob", app_id=None,
        provider_name="openai", provider_type="api_key",
        scope=Scope.PER_USER,
        fields={"api_key": "sk-BOB"},
    )

    flat = await build_compile_secrets(store, app_id="myapp")
    # anthropic single-field → exposed as both "anthropic" and "anthropic.api_key"
    assert flat.get("anthropic") == "sk-ant-SYSTEM"
    assert flat.get("anthropic.api_key") == "sk-ant-SYSTEM"
    # per_app_shared webhook exposed
    assert flat.get("webhook") == "wh-APP"
    # per_user NOT exposed at compile time
    assert "openai" not in flat
    assert "openai.api_key" not in flat
    _ok(f"system_wide + per_app_shared exposed at compile time ({len(flat)} keys)")
    _ok("per_user scope correctly hidden at compile time")

    # Merge with legacy secrets (takes precedence)
    flat2 = await build_compile_secrets(
        store, app_id="myapp",
        legacy_secrets={"LEGACY_KEY": "legacy-val", "anthropic": "sk-OVERRIDE"},
    )
    assert flat2.get("LEGACY_KEY") == "legacy-val"
    assert flat2.get("anthropic") == "sk-OVERRIDE"  # legacy wins
    _ok("legacy secrets merge with precedence")


async def test_7_bootstrap_env_import() -> None:
    _header("7. Bootstrap env var import (one-shot)")
    from digitorn.core.credentials import Scope
    from digitorn.core.credentials.bootstrap import import_env_vars_into_store

    store, _, _ = await setup_in_memory_store()

    # Pre-set some env vars
    os.environ["ANTHROPIC_API_KEY"] = "sk-ant-ENV-test"
    os.environ["SLACK_BOT_TOKEN"] = "xoxb-ENV-test"
    os.environ["SLACK_SIGNING_SECRET"] = "shh-ENV-test"

    try:
        summary = await import_env_vars_into_store(store)
        assert "anthropic.api_key" in summary["imported"]
        assert "slack.bot_token" in summary["imported"]
        assert "slack.signing_secret" in summary["imported"]
        _ok(f"imported {len(summary['imported'])} credential(s) from env")

        # Verify they actually landed in the store
        resolved = await store.resolve_field(
            provider_or_field="anthropic.api_key",
            user_id="anyuser", app_id="anyapp",
        )
        assert resolved == "sk-ant-ENV-test"
        _ok("imported credential is resolvable")

        # Slack → multi_field with 2 correlated fields on the SAME row
        slack_cred = await store.get_credential(
            user_id=None, app_id=None, provider_name="slack", decrypt=True,
        )
        assert slack_cred is not None
        assert slack_cred["provider_type"] == "multi_field"
        assert slack_cred["fields"]["bot_token"] == "xoxb-ENV-test"
        assert slack_cred["fields"]["signing_secret"] == "shh-ENV-test"
        _ok("multi-field imported into one row")

        # Second run should NOT overwrite (one-shot semantics)
        os.environ["ANTHROPIC_API_KEY"] = "sk-ant-DIFFERENT"
        summary2 = await import_env_vars_into_store(store)
        assert "anthropic.api_key" in summary2["skipped_already_present"]
        still = await store.resolve_field(
            provider_or_field="anthropic.api_key",
            user_id="anyuser", app_id="anyapp",
        )
        assert still == "sk-ant-ENV-test"  # unchanged
        _ok("second run skips existing credentials (never overwrites)")
    finally:
        for k in ("ANTHROPIC_API_KEY", "SLACK_BOT_TOKEN", "SLACK_SIGNING_SECRET"):
            os.environ.pop(k, None)


# ────────────────────────────────────────────────────────────────────
# Driver
# ────────────────────────────────────────────────────────────────────


async def test_8_oauth_flow() -> None:
    _header("8. OAuth flow (mock provider, end-to-end)")
    from digitorn.core.credentials.oauth_flow import (
        PendingFlowStore,
        build_auth_url,
        persist_oauth_credential,
    )
    from digitorn.core.credentials.oauth_providers import OAuthProviderConfig

    store, _, _ = await setup_in_memory_store()

    # Fake provider pointing nowhere (we never actually POST to it)
    provider = OAuthProviderConfig(
        name="fakenotion",
        auth_url="https://fake.example.com/authorize",
        token_url="https://fake.example.com/token",
        client_id="fake-client-id",
        client_secret="fake-client-secret",
        default_scopes=["read", "write"],
        redirect_uri="http://localhost:8000/api/oauth/callback",
        auth_style="basic",
        extra_auth_params={"owner": "user"},
    )
    assert provider.is_configured()
    _ok("provider configured")

    flow_store = PendingFlowStore()
    flow = await flow_store.start(
        provider=provider, user_id="alice", app_id="myapp",
    )
    assert flow.state and len(flow.state) > 20
    assert flow.status == "pending"
    _ok(f"pending flow created (state={flow.state[:16]}...)")

    # The auth URL should contain all the right params
    url = build_auth_url(flow)
    assert "client_id=fake-client-id" in url
    assert "response_type=code" in url
    assert f"state={flow.state}" in url
    assert "scope=read+write" in url or "scope=read%20write" in url
    assert "owner=user" in url
    _ok("auth URL includes client_id, state, scope, owner")

    # Simulate the callback → token exchange → credential persist
    # (we skip the actual HTTP exchange since there's no fake server
    # running; we call persist_oauth_credential with a fake response)
    token_response = {
        "access_token": "ntn_xxxxxxxxxxxxxxxxxxxx",
        "refresh_token": "refr_yyyyyyyyyyyyyyyyy",
        "token_type": "Bearer",
        "scope": "read write",
        "expires_in": 3600,
        "bot_user_id": "U_alice",
    }
    stored_cred = await persist_oauth_credential(
        store=store, flow=flow, token_response=token_response,
    )
    assert stored_cred["provider_name"] == "fakenotion"
    assert stored_cred["provider_type"] == "oauth2"
    assert stored_cred["scope"] == "per_user"
    assert stored_cred["status"] == "valid"
    assert stored_cred["expires_at"] is not None
    _ok(f"credential persisted (expires_at={stored_cred['expires_at'][:19]})")

    # Resolver walks the 4-scope lookup and finds the stored access_token
    resolved = await store.resolve_field(
        provider_or_field="fakenotion.access_token",
        user_id="alice", app_id="myapp",
    )
    assert resolved == "ntn_xxxxxxxxxxxxxxxxxxxx"
    _ok("access_token resolved via per_user scope")

    # The stored credential carries display metadata for the UI
    full = await store.get_credential(
        user_id="alice", app_id=None, provider_name="fakenotion",
    )
    assert full["display_metadata"].get("oauth_provider") == "fakenotion"
    assert "bot_user_id" in full["display_metadata"]
    _ok("display_metadata carries oauth_provider + bot_user_id")

    # mark_connected + mark_error round-trip
    await flow_store.mark_connected(flow.state, stored_cred["id"])
    updated = await flow_store.get(flow.state)
    assert updated.status == "connected"
    assert updated.resulting_credential_id == stored_cred["id"]
    _ok("flow state transition to 'connected'")


async def test_9_mcp_env_template() -> None:
    _header("9. MCP env_template substitution")
    from digitorn.core.credentials.handlers.mcp_server import (
        _render_env_template,
    )

    # Single-field substitution
    out = _render_env_template(
        {"NOTION_API_KEY": "{{field.api_key}}"},
        {"api_key": "secret_abc123"},
    )
    assert out == {"NOTION_API_KEY": "secret_abc123"}
    _ok("single-field substitution")

    # Multi-field substitution
    out = _render_env_template(
        {
            "FOO_USER": "{{field.username}}",
            "FOO_TOKEN": "{{field.token}}",
            "FOO_STATIC": "constant-value",
        },
        {"username": "alice", "token": "tok_xyz"},
    )
    assert out == {
        "FOO_USER": "alice",
        "FOO_TOKEN": "tok_xyz",
        "FOO_STATIC": "constant-value",
    }
    _ok("multi-field substitution + literal pass-through")

    # Missing field → empty string (not the literal template)
    out = _render_env_template(
        {"MISSING": "{{field.nope}}"},
        {},
    )
    assert out == {"MISSING": ""}
    _ok("missing field → empty string")

    # Composite value (field inside a longer string)
    out = _render_env_template(
        {"DB_URL": "postgres://user:{{field.password}}@host/db"},
        {"password": "s3cret"},
    )
    assert out == {"DB_URL": "postgres://user:s3cret@host/db"}
    _ok("composite value with inline substitution")


class _FakePool:
    """Minimal in-process pool stub that mimics MCPConnectionPool.

    Records calls so the test can assert the right methods were
    invoked with the right kwargs. Never spawns a real subprocess.
    """

    def __init__(self) -> None:
        self.servers: dict[str, dict] = {}
        self.calls: list[tuple[str, dict]] = []

    async def connect(
        self, server_id: str, transport_type: str, **kwargs,
    ) -> None:
        self.calls.append(("connect", {
            "server_id": server_id,
            "transport_type": transport_type,
            **kwargs,
        }))
        self.servers[server_id] = {
            "status": "connected",
            "transport_type": transport_type,
            "kwargs": kwargs,
        }

    async def disconnect(self, server_id: str) -> None:
        self.calls.append(("disconnect", {"server_id": server_id}))
        self.servers.pop(server_id, None)

    def get_server(self, server_id: str):
        entry = self.servers.get(server_id)
        if entry is None:
            return None

        class _E:
            pass
        e = _E()
        e.status = entry["status"]
        e.transport_type = entry["transport_type"]
        e.tools = []
        e.error = None
        e.created_at = 0.0
        return e


async def test_10_mcp_credential_lifecycle() -> None:
    _header("10. MCP credential lifecycle (fake pool)")
    from digitorn.core.credentials.handlers.mcp_server import McpServerHandler

    handler = McpServerHandler()
    pool = _FakePool()

    class _Ctx:
        pass
    ctx = _Ctx()
    ctx.mcp_pool = pool

    credential = {
        "provider_name": "notion-mcp",
        "fields": {"api_key": "secret_notion_xyz"},
    }
    schema_provider = {
        "name": "notion-mcp",
        "type": "mcp_server",
        "transport": "stdio",
        "command": ["npx", "-y", "@modelcontextprotocol/server-notion"],
        "env_template": {
            "NOTION_API_KEY": "{{field.api_key}}",
            "NODE_ENV": "production",
        },
        "fields": [{"name": "api_key", "type": "secret", "required": True}],
    }

    # on_credential_filled → should call pool.connect with stdio kwargs
    await handler.on_credential_filled(credential, schema_provider, ctx)
    assert pool.calls, "connect was not called"
    method, args = pool.calls[-1]
    assert method == "connect"
    assert args["server_id"] == "notion-mcp"
    assert args["transport_type"] == "stdio"
    assert args["command"] == "npx"
    assert args["args"] == ["-y", "@modelcontextprotocol/server-notion"]
    assert args["env"]["NOTION_API_KEY"] == "secret_notion_xyz"
    assert args["env"]["NODE_ENV"] == "production"
    _ok("stdio connect called with rendered env template")

    entry = pool.get_server("notion-mcp")
    assert entry is not None
    assert entry.status == "connected"
    _ok("fake pool reports server connected")

    # on_credential_removed → should disconnect
    await handler.on_credential_removed(credential, schema_provider, ctx)
    assert pool.get_server("notion-mcp") is None
    disconnect_calls = [c for c in pool.calls if c[0] == "disconnect"]
    assert len(disconnect_calls) >= 1
    _ok("disconnect called, server removed")


async def test_11_mcp_http_transport() -> None:
    _header("11. MCP http transport mapping → streamable_http")
    from digitorn.core.credentials.handlers.mcp_server import McpServerHandler

    handler = McpServerHandler()
    pool = _FakePool()

    class _Ctx:
        pass
    ctx = _Ctx()
    ctx.mcp_pool = pool

    credential = {
        "provider_name": "remote-mcp",
        "fields": {"token": "bearer_xyz"},
    }
    schema_provider = {
        "name": "remote-mcp",
        "type": "mcp_server",
        "transport": "http",
        "url": "https://mcp.example.com/v1",
        "env_template": {
            "HEADER_Authorization": "Bearer {{field.token}}",
        },
        "fields": [{"name": "token", "type": "secret", "required": True}],
    }

    await handler.on_credential_filled(credential, schema_provider, ctx)
    method, args = pool.calls[-1]
    assert method == "connect"
    assert args["transport_type"] == "streamable_http"
    assert args["url"] == "https://mcp.example.com/v1"
    assert args["headers"] == {"Authorization": "Bearer bearer_xyz"}
    _ok("http → streamable_http with Authorization header from env_template")


async def test_12_expires_at_datetime_or_string() -> None:
    _header("12. Store accepts expires_at as datetime OR ISO string")
    from datetime import datetime, timedelta, timezone
    from digitorn.core.credentials import Scope

    store, _, _ = await setup_in_memory_store()

    # Path A: datetime input (native form)
    future_dt = datetime.now(timezone.utc) + timedelta(hours=2)
    stored_a = await store.upsert_credential(
        user_id="alice", app_id=None,
        provider_name="provider_a", provider_type="api_key",
        scope=Scope.PER_USER,
        fields={"key": "a"},
        expires_at=future_dt,
    )
    assert stored_a["expires_at"] is not None
    _ok(f"datetime input → stored {stored_a['expires_at'][:19]}")

    # Path B: ISO string input (what handlers emit)
    future_iso = (datetime.now(timezone.utc) + timedelta(hours=3)).isoformat()
    stored_b = await store.upsert_credential(
        user_id="alice", app_id=None,
        provider_name="provider_b", provider_type="api_key",
        scope=Scope.PER_USER,
        fields={"key": "b"},
        expires_at=future_iso,
    )
    assert stored_b["expires_at"] is not None
    _ok(f"ISO string input → stored {stored_b['expires_at'][:19]}")

    # Path C: None explicitly → cleared (for new credential)
    stored_c = await store.upsert_credential(
        user_id="alice", app_id=None,
        provider_name="provider_c", provider_type="api_key",
        scope=Scope.PER_USER,
        fields={"key": "c"},
        expires_at=None,
    )
    assert stored_c["expires_at"] is None
    _ok("None input → no expiry")

    # Path D: invalid string → logged, stored as None (graceful degrade)
    stored_d = await store.upsert_credential(
        user_id="alice", app_id=None,
        provider_name="provider_d", provider_type="api_key",
        scope=Scope.PER_USER,
        fields={"key": "d"},
        expires_at="not-a-date",
    )
    assert stored_d["expires_at"] is None
    _ok("invalid string → gracefully None, no crash")

    # Path E: naive datetime → normalised to UTC
    naive_dt = datetime(2026, 12, 31, 23, 59, 59)
    stored_e = await store.upsert_credential(
        user_id="alice", app_id=None,
        provider_name="provider_e", provider_type="api_key",
        scope=Scope.PER_USER,
        fields={"key": "e"},
        expires_at=naive_dt,
    )
    assert stored_e["expires_at"] is not None
    assert "2026-12-31" in stored_e["expires_at"]
    _ok(f"naive datetime → UTC normalised: {stored_e['expires_at']}")


async def test_13_compile_passthrough_for_unknown_secret() -> None:
    _header("13. Compile-time passthrough for unknown secrets")
    from digitorn.core.app.variables import resolve_variables

    # A YAML using a per_user secret that's unknown at compile time.
    # Today this used to raise ValueError — now it passes through
    # as a literal ``{{secret.X}}`` so the runtime can resolve it.
    data = {
        "greeting": "Hello {{name}}!",
        "api": "Using {{secret.OPENAI_USER_KEY}} for requests",
        "nested": {
            "auth_header": "Bearer {{secret.per_user_token}}",
        },
    }
    resolved = resolve_variables(
        data,
        variables={"name": "Alice"},
        env={},
        secrets={},  # empty — secret.X should passthrough
    )
    assert resolved["greeting"] == "Hello Alice!"
    assert resolved["api"] == "Using {{secret.OPENAI_USER_KEY}} for requests"
    assert resolved["nested"]["auth_header"] == "Bearer {{secret.per_user_token}}"
    _ok("unknown secrets passthrough as templates (was: ValueError)")

    # Known secrets still resolve normally
    resolved = resolve_variables(
        {"x": "{{secret.KNOWN}}"},
        variables={},
        env={},
        secrets={"KNOWN": "known-value"},
    )
    assert resolved["x"] == "known-value"
    _ok("known secrets still resolve at compile time")


async def test_14_runtime_resolver() -> None:
    _header("14. Runtime per-user secret resolution")
    from digitorn.core.credentials import (
        Scope,
        resolve_runtime_secrets_in_value,
        collect_unresolved_secrets,
    )

    store, _, _ = await setup_in_memory_store()

    # Alice stores a user-owned key and grants "myapp" access
    alice_cred = await store.upsert_user_credential(
        user_id="alice",
        provider_name="ALICE_KEY", provider_type="api_key",
        fields={"ALICE_KEY": "sk-ant-alice-real-key"},
    )
    await store.create_grant(
        credential_id=alice_cred["id"], user_id="alice", app_id="myapp",
    )
    # Bob stores his own, also granted to "myapp"
    bob_cred = await store.upsert_user_credential(
        user_id="bob",
        provider_name="ALICE_KEY", provider_type="api_key",
        fields={"ALICE_KEY": "sk-ant-bob-real-key"},
    )
    await store.create_grant(
        credential_id=bob_cred["id"], user_id="bob", app_id="myapp",
    )

    template = {
        "system_prompt": "You are an agent. API key: {{secret.ALICE_KEY}}",
        "nested": [
            "prefix {{secret.ALICE_KEY}} suffix",
            "no secret here",
        ],
    }

    # Alice sees her key
    resolved_a = await resolve_runtime_secrets_in_value(
        template, store=store, user_id="alice", app_id="myapp",
    )
    assert "sk-ant-alice-real-key" in resolved_a["system_prompt"]
    assert "sk-ant-alice-real-key" in resolved_a["nested"][0]
    assert resolved_a["nested"][1] == "no secret here"
    _ok("alice sees her own key")

    # Bob sees his key — same template, different user → different value
    resolved_b = await resolve_runtime_secrets_in_value(
        template, store=store, user_id="bob", app_id="myapp",
    )
    assert "sk-ant-bob-real-key" in resolved_b["system_prompt"]
    assert "sk-ant-alice" not in resolved_b["system_prompt"]
    _ok("bob sees HIS key — per-user isolation confirmed")

    # Unknown user → template stays (no raise with default raise_on_miss=False)
    resolved_c = await resolve_runtime_secrets_in_value(
        template, store=store, user_id="carol", app_id="myapp",
    )
    assert "{{secret.ALICE_KEY}}" in resolved_c["system_prompt"]
    _ok("unknown user → template preserved (no silent empty)")

    # collect_unresolved_secrets detects the template
    unresolved = collect_unresolved_secrets(resolved_c)
    assert "ALICE_KEY" in unresolved
    _ok(f"collect_unresolved_secrets found {len(unresolved)} passthrough(s)")

    # raise_on_miss=True raises CredentialMissing
    from digitorn.core.credentials.store import CredentialMissing
    try:
        await resolve_runtime_secrets_in_value(
            template, store=store, user_id="carol", app_id="myapp",
            raise_on_miss=True,
        )
        _fail("should have raised CredentialMissing")
    except CredentialMissing as exc:
        assert exc.provider == "ALICE_KEY"
        _ok(f"raise_on_miss=True → CredentialMissing({exc.provider})")


async def test_15_end_to_end_per_user() -> None:
    _header("15. End-to-end: compile → runtime resolution")
    from digitorn.core.app.variables import resolve_variables
    from digitorn.core.credentials import (
        Scope,
        resolve_runtime_secrets_in_value,
    )
    from digitorn.core.credentials.compile_resolver import (
        build_compile_secrets,
    )

    store, _, _ = await setup_in_memory_store()

    # System-wide Anthropic key (applies to every user)
    await store.upsert_credential(
        user_id=None, app_id=None,
        provider_name="SHARED_KEY", provider_type="api_key",
        scope=Scope.SYSTEM_WIDE,
        fields={"SHARED_KEY": "sk-shared-for-everyone"},
    )
    # Per-user Anthropic keys — one for alice, one for bob
    alice_c = await store.upsert_user_credential(
        user_id="alice",
        provider_name="USER_KEY", provider_type="api_key",
        fields={"USER_KEY": "sk-alice-personal"},
    )
    await store.create_grant(
        credential_id=alice_c["id"], user_id="alice", app_id="myapp",
    )
    bob_c = await store.upsert_user_credential(
        user_id="bob",
        provider_name="USER_KEY", provider_type="api_key",
        fields={"USER_KEY": "sk-bob-personal"},
    )
    await store.create_grant(
        credential_id=bob_c["id"], user_id="bob", app_id="myapp",
    )

    # Simulated YAML app using both kinds of secrets
    app_yaml_data = {
        "brain": {
            "api_key": "{{secret.SHARED_KEY}}",
            "fallback_key": "{{secret.USER_KEY}}",
        },
        "system_prompt": "You are authed with {{secret.USER_KEY}}",
    }

    # COMPILE PHASE: build the secrets dict for the app
    compile_secrets = await build_compile_secrets(
        store, app_id="myapp",
    )
    # system_wide "SHARED_KEY" is in the dict
    assert compile_secrets.get("SHARED_KEY") == "sk-shared-for-everyone"
    # per_user "USER_KEY" is NOT (user context missing)
    assert "USER_KEY" not in compile_secrets
    _ok("compile secrets: system_wide yes, per_user no")

    # Run the compile-time resolver
    compiled = resolve_variables(
        app_yaml_data,
        variables={},
        env={},
        secrets=compile_secrets,
    )
    # SHARED_KEY resolved, USER_KEY passthrough
    assert compiled["brain"]["api_key"] == "sk-shared-for-everyone"
    assert compiled["brain"]["fallback_key"] == "{{secret.USER_KEY}}"
    assert compiled["system_prompt"] == "You are authed with {{secret.USER_KEY}}"
    _ok("compile: system_wide resolved, per_user passthrough")

    # RUNTIME PHASE: now we know who's running. Alice's turn.
    runtime_for_alice = await resolve_runtime_secrets_in_value(
        compiled, store=store, user_id="alice", app_id="myapp",
    )
    assert runtime_for_alice["brain"]["fallback_key"] == "sk-alice-personal"
    assert "sk-alice-personal" in runtime_for_alice["system_prompt"]
    # SHARED_KEY stays as the already-resolved value
    assert runtime_for_alice["brain"]["api_key"] == "sk-shared-for-everyone"
    _ok("runtime for alice: her per_user key injected, shared untouched")

    # Bob's turn — same compiled output, different runtime substitution
    runtime_for_bob = await resolve_runtime_secrets_in_value(
        compiled, store=store, user_id="bob", app_id="myapp",
    )
    assert runtime_for_bob["brain"]["fallback_key"] == "sk-bob-personal"
    assert "sk-bob-personal" in runtime_for_bob["system_prompt"]
    assert "sk-alice" not in runtime_for_bob["system_prompt"]
    _ok("runtime for bob: his key, alice's key NOT visible")

    print("\n  → full 2-phase flow validated:")
    print("     compile (system_wide) + runtime (per_user) = correct multi-tenant isolation")


# ════════════════════════════════════════════════════════════════════
# Grant-based model tests (new user-owned credentials + grants)
# ════════════════════════════════════════════════════════════════════


async def test_16_grant_first_use_flow() -> None:
    """User creates a credential, first-use on an app raises auth_required,
    user grants the credential, second use resolves silently."""
    _header("16. First-use flow: CredentialAuthRequired → grant → resolved")
    from digitorn.core.credentials import (
        CredentialAuthRequired,
        resolve_runtime_secrets_in_value,
    )

    store, _, _ = await setup_in_memory_store()

    cred = await store.upsert_user_credential(
        user_id="alice",
        provider_name="DEEPSEEK_API_KEY",
        provider_type="api_key",
        label="personal",
        fields={"DEEPSEEK_API_KEY": "sk-deep-alice"},
    )
    _ok("user credential created (no grant)")

    template = {"api_key": "{{env.DEEPSEEK_API_KEY}}"}

    # First use — no grant → CredentialAuthRequired with 1 candidate
    try:
        await resolve_runtime_secrets_in_value(
            template, store=store, user_id="alice", app_id="digitorn-code",
        )
        _fail("expected CredentialAuthRequired")
    except CredentialAuthRequired as exc:
        assert exc.provider == "DEEPSEEK_API_KEY"
        assert exc.app_id == "digitorn-code"
        assert len(exc.candidates) == 1
        assert exc.candidates[0]["id"] == cred["id"]
        _ok(f"CredentialAuthRequired raised with {len(exc.candidates)} candidate")

    # User grants the credential to the app
    grant = await store.create_grant(
        credential_id=cred["id"], user_id="alice", app_id="digitorn-code",
    )
    assert grant["active"] is True
    _ok("grant created")

    # Second use — silent success, api_key substituted
    resolved = await resolve_runtime_secrets_in_value(
        template, store=store, user_id="alice", app_id="digitorn-code",
    )
    assert resolved["api_key"] == "sk-deep-alice"
    _ok("resolved silently after grant")

    # Third app (different) → still asks for auth
    try:
        await resolve_runtime_secrets_in_value(
            template, store=store, user_id="alice", app_id="other-app",
        )
        _fail("expected CredentialAuthRequired for other-app")
    except CredentialAuthRequired:
        _ok("different app → still prompts (per-app authorization)")


async def test_17_grant_revoke_blocks_access() -> None:
    """After revoking a grant, the runtime raises auth_required again."""
    _header("17. Grant revocation re-blocks access")
    from digitorn.core.credentials import (
        CredentialAuthRequired,
        resolve_runtime_secrets_in_value,
    )

    store, _, _ = await setup_in_memory_store()

    cred = await store.upsert_user_credential(
        user_id="alice",
        provider_name="OPENAI_API_KEY",
        provider_type="api_key",
        fields={"OPENAI_API_KEY": "sk-openai"},
    )
    await store.create_grant(
        credential_id=cred["id"], user_id="alice", app_id="myapp",
    )

    # Works
    template = {"k": "{{secret.OPENAI_API_KEY}}"}
    r = await resolve_runtime_secrets_in_value(
        template, store=store, user_id="alice", app_id="myapp",
    )
    assert r["k"] == "sk-openai"
    _ok("before revoke: resolves")

    # Revoke
    ok = await store.revoke_grant(
        credential_id=cred["id"], app_id="myapp", user_id="alice",
    )
    assert ok
    _ok("grant revoked")

    # Blocks again
    try:
        await resolve_runtime_secrets_in_value(
            template, store=store, user_id="alice", app_id="myapp",
        )
        _fail("expected CredentialAuthRequired post-revoke")
    except CredentialAuthRequired:
        _ok("after revoke: CredentialAuthRequired raised")


async def test_18_system_credential_implicit() -> None:
    """System-wide credentials don't need grants — they're visible to all apps."""
    _header("18. System credentials — implicit access")
    from digitorn.core.credentials import resolve_runtime_secrets_in_value

    store, _, _ = await setup_in_memory_store()

    await store.upsert_system_credential(
        provider_name="INTERNAL_KEY",
        provider_type="api_key",
        fields={"INTERNAL_KEY": "company-shared-secret"},
    )
    _ok("system credential created")

    template = {"k": "{{secret.INTERNAL_KEY}}"}
    r = await resolve_runtime_secrets_in_value(
        template, store=store, user_id="anyone", app_id="any-app",
    )
    assert r["k"] == "company-shared-secret"
    _ok("any user / any app → resolved without grant")


async def test_19_system_credential_app_restricted() -> None:
    """A system credential scoped to one app is invisible to other apps."""
    _header("19. System credentials — app-restricted (enterprise case)")
    from digitorn.core.credentials import resolve_runtime_secrets_in_value

    store, _, _ = await setup_in_memory_store()

    await store.upsert_system_credential(
        provider_name="ACME_KEY",
        provider_type="api_key",
        app_id="acme-crm",
        fields={"ACME_KEY": "shared-between-acme-users"},
    )

    template = {"k": "{{secret.ACME_KEY}}"}
    # Matching app → ok
    r = await resolve_runtime_secrets_in_value(
        template, store=store, user_id="bob", app_id="acme-crm",
    )
    assert r["k"] == "shared-between-acme-users"
    _ok("acme-crm users see the key")

    # Other app → not visible, returns passthrough
    r2 = await resolve_runtime_secrets_in_value(
        template, store=store, user_id="bob", app_id="some-other-app",
    )
    assert r2["k"] == "{{secret.ACME_KEY}}"
    _ok("other apps → not visible, passthrough returned")


async def test_20_migration_from_legacy_scopes() -> None:
    """Legacy 4-scope rows get owner_type + grants filled in idempotently."""
    _header("20. Migration from legacy 4-scope rows")
    from digitorn.core.credentials import Scope

    store, _, _ = await setup_in_memory_store()

    # Create rows the legacy way
    await store.upsert_credential(
        user_id="alice", app_id="app1",
        provider_name="KEY1", provider_type="api_key",
        scope=Scope.PER_APP_PER_USER,
        fields={"KEY1": "v1"},
    )
    await store.upsert_credential(
        user_id="alice", app_id=None,
        provider_name="KEY2", provider_type="api_key",
        scope=Scope.PER_USER,
        fields={"KEY2": "v2"},
    )
    await store.upsert_credential(
        user_id=None, app_id=None,
        provider_name="KEY3", provider_type="api_key",
        scope=Scope.SYSTEM_WIDE,
        fields={"KEY3": "v3"},
    )
    _ok("3 legacy rows seeded")

    # Simulate pre-migration state: rows created before the
    # owner_type column existed have an empty string (what the
    # ALTER TABLE default gives existing rows).
    from digitorn.core.database import get_session_factory
    from digitorn.core.models import Credential
    from sqlalchemy import update
    async with get_session_factory()() as db:
        await db.execute(update(Credential).values(owner_type=""))
        await db.commit()
    _ok("owner_type cleared to simulate pre-migration state")

    # Run the migration
    result = await store.migrate_legacy_scopes()
    _ok(
        f"migration: user_rows={result['user_rows']} "
        f"system_rows={result['system_rows']} "
        f"grants_created={result['grants_created']}"
    )
    assert result["user_rows"] >= 2
    assert result["system_rows"] >= 1

    # The per_app_per_user row should now be user-owned + have a grant
    grants_a = await store.list_grants(user_id="alice")
    assert any(g["provider_name"] == "KEY1" and g["app_id"] == "app1" for g in grants_a)
    _ok("per_app_per_user migrated to user-owned + grant")

    # Second run is a no-op (idempotent)
    result2 = await store.migrate_legacy_scopes()
    assert result2["user_rows"] == 0
    assert result2["system_rows"] == 0
    _ok("second migration run is a no-op")


async def test_21_label_disambiguates_multiple_keys() -> None:
    """A user can have several credentials for the same provider
    distinguished by label — picker shows all candidates."""
    _header("21. Multiple labels per provider (personal vs work)")
    from digitorn.core.credentials import CredentialAuthRequired
    from digitorn.core.credentials.runtime_resolver import resolve_runtime_secrets_in_value

    store, _, _ = await setup_in_memory_store()

    await store.upsert_user_credential(
        user_id="alice", provider_name="NOTION",
        provider_type="oauth2", label="personal",
        fields={"access_token": "tok-perso"},
    )
    await store.upsert_user_credential(
        user_id="alice", provider_name="NOTION",
        provider_type="oauth2", label="work",
        fields={"access_token": "tok-work"},
    )

    creds = await store.list_user_credentials(
        user_id="alice", provider_name="NOTION",
    )
    assert len(creds) == 2
    _ok(f"user has {len(creds)} NOTION credentials")

    # First-use flow should surface both as candidates
    try:
        await resolve_runtime_secrets_in_value(
            {"t": "{{secret.NOTION.access_token}}"},
            store=store, user_id="alice", app_id="blog",
        )
        _fail("expected CredentialAuthRequired")
    except CredentialAuthRequired as exc:
        assert len(exc.candidates) == 2
        _ok(f"picker receives {len(exc.candidates)} candidates")


async def test_22_session_resolver_integration() -> None:
    """ensure_user_credentials_for_app mutates live providers."""
    _header("22. Session resolver — provider api_key mutation")
    from digitorn.core.credentials import (
        CredentialAuthRequired,
        ensure_user_credentials_for_app,
    )

    store, _, _ = await setup_in_memory_store()

    await store.upsert_user_credential(
        user_id="alice", provider_name="DEEPSEEK_API_KEY",
        provider_type="api_key",
        fields={"DEEPSEEK_API_KEY": "sk-real-alice"},
    )

    # Fake a DeployedApp with a fake llm_module + compiled config
    class _FakeProvider:
        def __init__(self) -> None:
            self.api_key = "{{env.DEEPSEEK_API_KEY}}"
            self.base_url = "https://api.deepseek.com/v1"

    class _FakeLLM:
        def __init__(self) -> None:
            self._providers = {"main": _FakeProvider()}

    class _FakeModuleCfg:
        def __init__(self, cfg):
            self.config = cfg

    class _FakeCompiled:
        def __init__(self):
            self.modules = {
                "llm_provider": _FakeModuleCfg({
                    "providers": {
                        "main": {
                            "backend": "openai_compat",
                            "api_key": "{{env.DEEPSEEK_API_KEY}}",
                            "base_url": "https://api.deepseek.com/v1",
                        }
                    }
                })
            }

    class _FakeDeployed:
        def __init__(self):
            self.app_id = "myapp"
            self.compiled = _FakeCompiled()
            self.modules = {"llm_provider": _FakeLLM()}

    deployed = _FakeDeployed()

    # First call: no grant → CredentialAuthRequired
    try:
        await ensure_user_credentials_for_app(
            deployed_app=deployed, user_id="alice",
            credential_store=store,
        )
        _fail("expected CredentialAuthRequired")
    except CredentialAuthRequired:
        _ok("ensure_user_credentials raises without grant")

    # Grant + retry
    creds = await store.list_user_credentials(
        user_id="alice", provider_name="DEEPSEEK_API_KEY",
    )
    await store.create_grant(
        credential_id=creds[0]["id"], user_id="alice", app_id="myapp",
    )

    diag = await ensure_user_credentials_for_app(
        deployed_app=deployed, user_id="alice",
        credential_store=store,
    )
    assert "main" in diag["resolved_providers"]
    assert deployed.modules["llm_provider"]._providers["main"].api_key == "sk-real-alice"
    _ok("provider api_key mutated to real value after grant")


async def main() -> None:
    tests = [
        test_1_encryption,
        test_2_store_crud,
        test_3_resolver_order,
        test_4_handler_validation,
        test_5_yaml_compile,
        test_6_compile_secret_resolver,
        test_7_bootstrap_env_import,
        test_8_oauth_flow,
        test_9_mcp_env_template,
        test_10_mcp_credential_lifecycle,
        test_11_mcp_http_transport,
        test_12_expires_at_datetime_or_string,
        test_13_compile_passthrough_for_unknown_secret,
        test_14_runtime_resolver,
        test_15_end_to_end_per_user,
        test_16_grant_first_use_flow,
        test_17_grant_revoke_blocks_access,
        test_18_system_credential_implicit,
        test_19_system_credential_app_restricted,
        test_20_migration_from_legacy_scopes,
        test_21_label_disambiguates_multiple_keys,
        test_22_session_resolver_integration,
    ]
    for t in tests:
        await t()

    print(f"\n{'═' * 60}")
    print(f"  ALL {len(tests)} TESTS PASSED ✓")
    print(f"{'═' * 60}\n")


if __name__ == "__main__":
    asyncio.run(main())
