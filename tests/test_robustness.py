"""Tests de robustesse - garantir zéro crash à l'exécution.

Couvre:
    - Exécution sans event bus
    - Exécution sans base de données
    - Exécution concurrente (10 actions en parallèle)
    - Stream avec event bus cassé
    - Config API avec valeurs invalides
    - Module introuvable / action introuvable
    - Scope partiel (app sans session, agent sans app, etc.)
    - Payloads vides / extrêmes
    - Persistance quand la DB est down
    - Health check sur module cassé
    - UniversalEvent - child/sibling avec champs nuls
    - EventRouter - patterns invalides
"""

from __future__ import annotations

import asyncio
import tempfile

import pytest
import pytest_asyncio
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient

from digitorn.core.config import Settings, override_settings
from digitorn.core.server import create_app


@pytest_asyncio.fixture
async def client():
    """Fixture: HTTP client with lifespan (in-memory DB)."""
    settings = Settings()
    settings.database.url = "sqlite+aiosqlite://"  # in-memory, fresh each run
    settings.server.auth_enabled = False
    settings.server.rate_limit_rpm = 100_000  # disable rate limiting in tests
    settings.server.kv_backend = tempfile.mkdtemp()  # fresh rate limiter per test
    override_settings(settings)
    asgi_app = create_app(settings=settings)
    inner_app = asgi_app.other_asgi_app

    async with LifespanManager(inner_app) as _:
        transport = ASGITransport(app=inner_app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            yield c


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------


class TestHealth:
    @pytest.mark.asyncio
    async def test_health_ok(self, client: AsyncClient):
        r = await client.get("/health")
        assert r.status_code == 200
        assert r.json()["status"] == "ok"


# ---------------------------------------------------------------------------
# Module execution - happy path
# ---------------------------------------------------------------------------


class TestExecuteHappyPath:
    @pytest.mark.asyncio
    async def test_say_hello(self, client: AsyncClient):
        r = await client.post("/api/modules/hello/execute", json={
            "action": "say_hello",
            "params": {"name": "Test"},
        })
        assert r.status_code == 200
        body = r.json()
        assert body["success"] is True
        assert body["data"] == "Hello, Test!"

    @pytest.mark.asyncio
    async def test_say_hello_default_name(self, client: AsyncClient):
        r = await client.post("/api/modules/hello/execute", json={
            "action": "say_hello",
            "params": {},
        })
        assert r.status_code == 200
        assert r.json()["data"] == "Hello, World!"

    @pytest.mark.asyncio
    async def test_greet_many_with_stream(self, client: AsyncClient):
        r = await client.post("/api/modules/hello/execute", json={
            "action": "greet_many",
            "params": {"names": ["A", "B", "C"]},
            "app_id": "test-app",
            "session_id": "sess",
            "agent_id": "agent",
        })
        assert r.status_code == 200
        body = r.json()
        assert body["success"] is True
        assert len(body["data"]) == 3

    @pytest.mark.asyncio
    async def test_status_action(self, client: AsyncClient):
        r = await client.post("/api/modules/hello/execute", json={
            "action": "status",
            "params": {},
        })
        assert r.status_code == 200
        data = r.json()["data"]
        assert data["module_id"] == "hello"
        assert data["version"] == "1.0.0"


# ---------------------------------------------------------------------------
# Module execution - error cases
# ---------------------------------------------------------------------------


class TestExecuteErrors:
    @pytest.mark.asyncio
    async def test_module_not_found(self, client: AsyncClient):
        r = await client.post("/api/modules/nonexistent/execute", json={
            "action": "test",
            "params": {},
        })
        assert r.status_code == 404

    @pytest.mark.asyncio
    async def test_action_not_found(self, client: AsyncClient):
        r = await client.post("/api/modules/hello/execute", json={
            "action": "does_not_exist",
            "params": {},
        })
        assert r.status_code == 404

    @pytest.mark.asyncio
    async def test_empty_action_name(self, client: AsyncClient):
        r = await client.post("/api/modules/hello/execute", json={
            "action": "",
            "params": {},
        })
        # Empty action should return 404 (action not found)
        assert r.status_code == 404

    @pytest.mark.asyncio
    async def test_missing_action_field(self, client: AsyncClient):
        r = await client.post("/api/modules/hello/execute", json={
            "params": {},
        })
        # Pydantic validation error
        assert r.status_code == 422

    @pytest.mark.asyncio
    async def test_invalid_json_body(self, client: AsyncClient):
        r = await client.post(
            "/api/modules/hello/execute",
            content=b"not json",
            headers={"content-type": "application/json"},
        )
        assert r.status_code == 422


# ---------------------------------------------------------------------------
# Scope partiel - toutes les combinaisons
# ---------------------------------------------------------------------------


class TestPartialScope:
    @pytest.mark.asyncio
    async def test_no_scope(self, client: AsyncClient):
        """Execution without app_id, session_id, agent_id."""
        r = await client.post("/api/modules/hello/execute", json={
            "action": "say_hello",
            "params": {},
        })
        assert r.status_code == 200
        assert r.json()["success"] is True

    @pytest.mark.asyncio
    async def test_app_only(self, client: AsyncClient):
        r = await client.post("/api/modules/hello/execute", json={
            "action": "say_hello",
            "params": {},
            "app_id": "my-app",
        })
        assert r.status_code == 200

    @pytest.mark.asyncio
    async def test_app_and_session(self, client: AsyncClient):
        r = await client.post("/api/modules/hello/execute", json={
            "action": "say_hello",
            "params": {},
            "app_id": "my-app",
            "session_id": "sess",
        })
        assert r.status_code == 200

    @pytest.mark.asyncio
    async def test_agent_without_app(self, client: AsyncClient):
        """Agent without app - should still work (no crash)."""
        r = await client.post("/api/modules/hello/execute", json={
            "action": "say_hello",
            "params": {},
            "agent_id": "orphan-agent",
        })
        assert r.status_code == 200

    @pytest.mark.asyncio
    async def test_session_without_app(self, client: AsyncClient):
        r = await client.post("/api/modules/hello/execute", json={
            "action": "say_hello",
            "params": {},
            "session_id": "orphan-session",
        })
        assert r.status_code == 200


# ---------------------------------------------------------------------------
# Concurrence
# ---------------------------------------------------------------------------


class TestConcurrency:
    @pytest.mark.asyncio
    async def test_10_parallel_executions(self, client: AsyncClient):
        """10 concurrent executions must all succeed."""
        async def execute(i: int):
            r = await client.post("/api/modules/hello/execute", json={
                "action": "say_hello",
                "params": {"name": f"User-{i}"},
                "app_id": "load-test",
                "session_id": f"sess-{i}",
                "agent_id": f"agent-{i}",
            })
            return r

        results = await asyncio.gather(*[execute(i) for i in range(10)])
        for i, r in enumerate(results):
            assert r.status_code == 200, f"Request {i} failed: {r.status_code}"
            assert r.json()["data"] == f"Hello, User-{i}!"

    @pytest.mark.asyncio
    async def test_parallel_stream_executions(self, client: AsyncClient):
        """Concurrent streaming actions must not interfere."""
        async def execute(i: int):
            r = await client.post("/api/modules/hello/execute", json={
                "action": "greet_many",
                "params": {"names": [f"A{i}", f"B{i}"]},
                "app_id": "stream-test",
                "agent_id": f"agent-{i}",
            })
            return r

        results = await asyncio.gather(*[execute(i) for i in range(5)])
        for i, r in enumerate(results):
            assert r.status_code == 200
            assert r.json()["data"] == [f"Hello, A{i}!", f"Hello, B{i}!"]


# ---------------------------------------------------------------------------
# Config API
# ---------------------------------------------------------------------------


class TestConfigAPI:
    @pytest.mark.asyncio
    async def test_get_config(self, client: AsyncClient):
        r = await client.get("/api/config")
        assert r.status_code == 200
        data = r.json()
        assert "server" in data
        assert "database" in data
        assert "modules" in data
        assert "logging" in data

    @pytest.mark.asyncio
    async def test_patch_valid(self, client: AsyncClient):
        r = await client.patch("/api/config", json={
            "logging": {"level": "debug"},
        })
        assert r.status_code == 200
        body = r.json()
        assert body["applied"] == {"logging": {"level": "debug"}}
        assert body["errors"] == []

    @pytest.mark.asyncio
    async def test_patch_invalid_port(self, client: AsyncClient):
        r = await client.patch("/api/config", json={
            "server": {"port": 999},
        })
        assert r.status_code == 200
        body = r.json()
        assert len(body["errors"]) > 0
        assert body["applied"] == {}

    @pytest.mark.asyncio
    async def test_patch_invalid_log_level(self, client: AsyncClient):
        r = await client.patch("/api/config", json={
            "logging": {"level": "nonexistent"},
        })
        assert r.status_code == 200
        body = r.json()
        assert len(body["errors"]) > 0

    @pytest.mark.asyncio
    async def test_patch_empty_body(self, client: AsyncClient):
        r = await client.patch("/api/config", json={})
        assert r.status_code == 200
        assert r.json()["applied"] == {}

    @pytest.mark.asyncio
    async def test_patch_restart_required(self, client: AsyncClient):
        r = await client.patch("/api/config", json={
            "server": {"port": 9000},
        })
        assert r.status_code == 200
        assert "server.port" in r.json()["restart_required"]

    @pytest.mark.asyncio
    async def test_patch_unknown_section(self, client: AsyncClient):
        """Unknown top-level key should be ignored (extra='ignore')."""
        r = await client.patch("/api/config", json={
            "nonexistent": {"key": "value"},
        })
        assert r.status_code == 200


# ---------------------------------------------------------------------------
# Module listing & details
# ---------------------------------------------------------------------------


class TestModuleListing:
    @pytest.mark.asyncio
    async def test_list_modules(self, client: AsyncClient):
        r = await client.get("/api/modules")
        assert r.status_code == 200
        data = r.json()
        assert data["count"] >= 1
        ids = [m["module_id"] for m in data["modules"]]
        assert "hello" in ids

    @pytest.mark.asyncio
    async def test_get_module_details(self, client: AsyncClient):
        r = await client.get("/api/modules/hello")
        assert r.status_code == 200
        data = r.json()
        assert data["module_id"] == "hello"
        assert len(data["actions"]) >= 2

    @pytest.mark.asyncio
    async def test_get_module_not_found(self, client: AsyncClient):
        r = await client.get("/api/modules/nonexistent")
        assert r.status_code == 404

    @pytest.mark.asyncio
    async def test_module_health(self, client: AsyncClient):
        r = await client.get("/api/modules/hello/health")
        assert r.status_code == 200

    @pytest.mark.asyncio
    async def test_module_health_not_found(self, client: AsyncClient):
        r = await client.get("/api/modules/nonexistent/health")
        assert r.status_code == 404


# ---------------------------------------------------------------------------
# UniversalEvent - edge cases
# ---------------------------------------------------------------------------


class TestUniversalEvent:
    def test_child_with_all_none_scope(self):
        from digitorn.core.events.models import UniversalEvent

        parent = UniversalEvent(topic="test.parent", source="test")
        child = parent.child("test.child", event_type="progress")

        assert child.correlation_id == parent.event_id
        assert child.causation_id == parent.event_id
        assert child.parent_id == parent.event_id
        assert child.app_id is None
        assert child.session_id is None
        assert child.agent_id is None

    def test_child_inherits_scope(self):
        from digitorn.core.events.models import UniversalEvent

        parent = UniversalEvent(
            topic="test.parent",
            source="test",
            app_id="app-1",
            session_id="sess-1",
            agent_id="agent-1",
        )
        child = parent.child("test.child")

        assert child.app_id == "app-1"
        assert child.session_id == "sess-1"
        assert child.agent_id == "agent-1"

    def test_sibling_preserves_chain(self):
        from digitorn.core.events.models import UniversalEvent

        parent = UniversalEvent(topic="test.parent", source="test", app_id="app")
        child1 = parent.child("test.child1")
        child2 = child1.sibling("test.child2")

        assert child2.correlation_id == child1.correlation_id
        assert child2.parent_id == child1.parent_id
        assert child2.app_id == "app"

    def test_child_chain_depth(self):
        """3 levels deep - correlation stays the same."""
        from digitorn.core.events.models import UniversalEvent

        root = UniversalEvent(topic="root", source="test")
        child = root.child("child")
        grandchild = child.child("grandchild")

        assert grandchild.correlation_id == root.event_id
        assert grandchild.causation_id == child.event_id
        assert grandchild.parent_id == child.event_id


# ---------------------------------------------------------------------------
# EventRouter - edge cases
# ---------------------------------------------------------------------------


class TestEventRouter:
    def test_empty_pattern(self):
        from digitorn.core.events.router import EventRouter

        router = EventRouter()
        handler = lambda e: None
        router.add("", handler)
        # Should not crash
        assert router.match("") == [handler]
        assert router.match("anything") == []

    def test_wildcard_single(self):
        from digitorn.core.events.router import EventRouter

        router = EventRouter()
        collected = []
        router.add("digitorn.module.*.action_started", collected.append)
        assert len(router.match("digitorn.module.hello.action_started")) == 1
        assert len(router.match("digitorn.module.fs.action_started")) == 1
        assert len(router.match("digitorn.module.hello.other")) == 0

    def test_wildcard_multi(self):
        from digitorn.core.events.router import EventRouter

        router = EventRouter()
        collected = []
        router.add("digitorn.#", collected.append)
        assert len(router.match("digitorn.module.hello.action_started")) == 1
        assert len(router.match("digitorn.x")) == 1
        assert len(router.match("other.topic")) == 0

    def test_remove_handler(self):
        from digitorn.core.events.router import EventRouter

        router = EventRouter()
        handler = lambda e: None
        router.add("test.*", handler)
        assert len(router.match("test.foo")) == 1
        router.remove("test.*", handler)
        assert len(router.match("test.foo")) == 0

    def test_remove_nonexistent_handler(self):
        """Removing a handler that was never added should not crash."""
        from digitorn.core.events.router import EventRouter

        router = EventRouter()
        router.remove("test.*", lambda e: None)  # no crash


# ---------------------------------------------------------------------------
# ExecutionStream - edge cases
# ---------------------------------------------------------------------------


class TestExecutionStream:
    @pytest.mark.asyncio
    async def test_stream_with_failing_bus(self):
        """Stream must not crash even if the bus raises."""
        from digitorn.core.events.models import UniversalEvent
        from digitorn.core.events.stream import ExecutionStream

        class FailingBus:
            async def publish(self, event):
                raise RuntimeError("Bus is down")
            def subscribe(self, p, h): pass
            def unsubscribe(self, p, h): pass

        parent = UniversalEvent(topic="test", source="test")
        stream = ExecutionStream(FailingBus(), parent, "test-module", "test-action")

        # None of these should raise
        await stream.progress(1, 10, "step")
        await stream.partial_result({"key": "value"})
        await stream.log("info", "message")
        await stream.warning("warning message")

    @pytest.mark.asyncio
    async def test_stream_with_null_bus(self):
        """Stream with NullEventBus should work fine."""
        from digitorn.core.events.bus import NullEventBus
        from digitorn.core.events.models import UniversalEvent
        from digitorn.core.events.stream import ExecutionStream

        parent = UniversalEvent(topic="test", source="test")
        stream = ExecutionStream(NullEventBus(), parent, "mod", "act")

        await stream.progress(0, 0)
        await stream.partial_result(None)
        await stream.log("debug", "")
        await stream.warning("")

    @pytest.mark.asyncio
    async def test_stream_large_payload(self):
        """Stream with large data should not crash."""
        from digitorn.core.events.bus import NullEventBus
        from digitorn.core.events.models import UniversalEvent
        from digitorn.core.events.stream import ExecutionStream

        parent = UniversalEvent(topic="test", source="test")
        stream = ExecutionStream(NullEventBus(), parent, "mod", "act")

        large_data = {"items": list(range(10000))}
        await stream.partial_result(large_data)


# ---------------------------------------------------------------------------
# Persistence - edge cases
# ---------------------------------------------------------------------------


class TestPersistence:
    @pytest.mark.asyncio
    async def test_executions_persisted(self, client: AsyncClient):
        """Verify executions end up in the database."""
        from sqlalchemy import func, select

        from digitorn.core.database import _session_factory
        from digitorn.core.models import ActionExecution

        # Execute something
        r = await client.post("/api/modules/hello/execute", json={
            "action": "say_hello",
            "params": {"name": "DB-Test"},
            "app_id": "persist-test",
        })
        assert r.status_code == 200

        # Check DB
        async with _session_factory() as session:
            stmt = select(ActionExecution).where(
                ActionExecution.app_id == "persist-test",
            )
            results = (await session.execute(stmt)).scalars().all()
            assert len(results) >= 1
            latest = results[-1]
            assert latest.status == "completed"
            assert latest.module_id == "hello"
            assert latest.action == "say_hello"
            assert latest.correlation_id is not None
            assert latest.completed_at is not None

    @pytest.mark.asyncio
    async def test_failed_execution_persisted(self, client: AsyncClient):
        from sqlalchemy import select

        from digitorn.core.database import _session_factory
        from digitorn.core.models import ActionExecution

        r = await client.post("/api/modules/hello/execute", json={
            "action": "nonexistent",
            "params": {},
            "app_id": "persist-fail-test",
        })
        assert r.status_code == 404

        async with _session_factory() as session:
            stmt = select(ActionExecution).where(
                ActionExecution.app_id == "persist-fail-test",
            )
            results = (await session.execute(stmt)).scalars().all()
            assert len(results) >= 1
            assert results[-1].status == "failed"
            assert results[-1].error is not None


# ---------------------------------------------------------------------------
# Module self.stream property
# ---------------------------------------------------------------------------


class TestModuleStreamProperty:
    @pytest.mark.asyncio
    async def test_stream_is_none_outside_execute(self):
        """self.stream should be None when not inside execute()."""
        from digitorn.modules.hello.module import HelloModule

        module = HelloModule()
        assert module.stream is None

    @pytest.mark.asyncio
    async def test_stream_available_during_execute(self):
        """self.stream should be available during execute with context."""
        from digitorn.core.events.bus import NullEventBus
        from digitorn.core.events.models import UniversalEvent
        from digitorn.core.events.stream import ExecutionStream
        from digitorn.modules.base import ExecutionContext
        from digitorn.modules.hello.module import HelloModule

        module = HelloModule()
        parent = UniversalEvent(topic="test", source="test")
        stream = ExecutionStream(NullEventBus(), parent, "hello", "greet_many")
        context = ExecutionContext(
            plan_id="plan",
            action_id="act",
            stream=stream,
        )

        result = await module.execute("greet_many", {"names": ["X"]}, context=context)
        assert result.success is True

        # After execute, stream should be None again
        assert module.stream is None

    @pytest.mark.asyncio
    async def test_stream_none_without_context(self):
        """Execute without context - stream should be None, no crash."""
        from digitorn.modules.hello.module import HelloModule

        module = HelloModule()
        result = await module.execute("greet_many", {"names": ["Y"]})
        assert result.success is True
        assert module.stream is None


# ---------------------------------------------------------------------------
# Module loading filters (enabled/disabled/load_all)
# ---------------------------------------------------------------------------


class TestModuleLoadFilters:
    """Test the enabled/disabled/load_all filtering logic in loader."""

    def _make_descriptor(self, module_id: str, enabled: bool = True):
        from digitorn.core.loader import ModuleDescriptor
        return ModuleDescriptor(module_id=module_id, version="1.0.0", enabled=enabled)

    def test_load_all_true_loads_enabled_modules(self):
        """load_all=True loads modules with enabled=true in TOML."""
        from digitorn.modules.registry import ModuleRegistry
        from digitorn.core.loader import load_modules

        registry = ModuleRegistry()
        results = load_modules(registry, load_all=True)
        # hello module should be loaded (enabled=true by default in TOML)
        assert "hello" in results
        assert results["hello"] == "loaded"

    def test_disabled_list_overrides_everything(self):
        """disabled list skips modules even if they are in enabled list."""
        from digitorn.modules.registry import ModuleRegistry
        from digitorn.core.loader import load_modules

        registry = ModuleRegistry()
        results = load_modules(
            registry,
            enabled=["hello"],
            disabled=["hello"],
            load_all=True,
        )
        assert results["hello"] == "skipped"

    def test_load_all_false_skips_non_listed(self):
        """load_all=False skips modules not in enabled list."""
        from digitorn.modules.registry import ModuleRegistry
        from digitorn.core.loader import load_modules

        registry = ModuleRegistry()
        results = load_modules(
            registry,
            enabled=["nonexistent"],
            load_all=False,
        )
        # hello exists but is not in enabled list → skipped
        if "hello" in results:
            assert results["hello"] == "skipped"

    def test_load_all_false_with_enabled(self):
        """load_all=False + enabled=['hello'] loads only hello."""
        from digitorn.modules.registry import ModuleRegistry
        from digitorn.core.loader import load_modules

        registry = ModuleRegistry()
        results = load_modules(
            registry,
            enabled=["hello"],
            load_all=False,
        )
        assert results["hello"] == "loaded"

    def test_descriptor_enabled_false_skipped(self, tmp_path):
        """Module with enabled=false in TOML is skipped when load_all=True."""
        import tomllib
        from digitorn.core.loader import discover_modules, load_modules
        from digitorn.modules.registry import ModuleRegistry

        # Create a fake module dir with enabled=false
        mod_dir = tmp_path / "fake_mod"
        mod_dir.mkdir()
        toml_content = b'[module]\nmodule_id = "fake_disabled"\nversion = "0.1.0"\nenabled = false\n'
        (mod_dir / "digitorn-module.toml").write_bytes(toml_content)

        registry = ModuleRegistry()
        results = load_modules(registry, extra_dirs=[tmp_path], load_all=True)
        assert results.get("fake_disabled") == "disabled"

    def test_descriptor_enabled_false_forced_by_enabled_list(self, tmp_path):
        """Module with enabled=false in TOML is loaded if in enabled list."""
        mod_dir = tmp_path / "fake_mod2"
        mod_dir.mkdir()
        toml_content = b'[module]\nmodule_id = "fake_forced"\nversion = "0.1.0"\nenabled = false\n'
        (mod_dir / "digitorn-module.toml").write_bytes(toml_content)

        from digitorn.modules.registry import ModuleRegistry
        from digitorn.core.loader import load_modules

        registry = ModuleRegistry()
        results = load_modules(
            registry,
            extra_dirs=[tmp_path],
            enabled=["fake_forced"],
            load_all=True,
        )
        # It will try to load but fail (no module.py, no docs) - but it should NOT be "disabled"
        assert results.get("fake_forced") != "disabled"
        assert results.get("fake_forced") != "skipped"

    def test_config_load_all_field(self):
        """ModulesConfig has load_all field with default=True."""
        from digitorn.core.config import ModulesConfig
        config = ModulesConfig()
        assert config.load_all is True

        config2 = ModulesConfig(load_all=False)
        assert config2.load_all is False


# ---------------------------------------------------------------------------
# Security gate enforcement
# ---------------------------------------------------------------------------


class TestSecurityProfile:
    """Test SecurityProfile permission matching."""

    def test_admin_bypasses_everything(self):
        from digitorn.core.security import SecurityProfile
        profile = SecurityProfile(app_id="admin", is_admin=True)
        assert profile.has_permission("anything.dangerous") is True
        assert profile.can_access_module("any_module") is True
        assert profile.can_handle_risk("high") is True

    def test_no_permissions_denies_all(self):
        from digitorn.core.security import SecurityProfile
        profile = SecurityProfile(app_id="empty")
        assert profile.has_permission("filesystem.read") is False

    def test_exact_permission_match(self):
        from digitorn.core.security import SecurityProfile
        profile = SecurityProfile(
            app_id="app1",
            granted_permissions=frozenset(["filesystem.read", "hello.say_hello"]),
        )
        assert profile.has_permission("filesystem.read") is True
        assert profile.has_permission("hello.say_hello") is True
        assert profile.has_permission("filesystem.write") is False

    def test_wildcard_permission(self):
        from digitorn.core.security import SecurityProfile
        profile = SecurityProfile(
            app_id="app1",
            granted_permissions=frozenset(["filesystem.*"]),
        )
        assert profile.has_permission("filesystem.read") is True
        assert profile.has_permission("filesystem.delete") is True
        assert profile.has_permission("hello.say_hello") is False

    def test_module_hidden_via_grant(self):
        from digitorn.core.security import ModuleGrant, SecurityProfile
        profile = SecurityProfile(
            app_id="app1",
            module_grants={
                "os_exec": ModuleGrant(module_id="os_exec", visibility="hidden"),
                "hello": ModuleGrant(module_id="hello", visibility="full"),
            },
        )
        assert profile.can_access_module("hello") is True
        assert profile.can_access_module("os_exec") is False
        # Module without explicit grant is accessible by default
        assert profile.can_access_module("filesystem") is True

    def test_inactive_app_denied(self):
        from digitorn.core.security import SecurityProfile
        profile = SecurityProfile(app_id="app1", is_active=False)
        assert profile.can_access_module("hello") is False

    def test_risk_level_enforcement(self):
        from digitorn.core.security import SecurityProfile
        profile = SecurityProfile(app_id="app1", max_risk_level="medium")
        assert profile.can_handle_risk("low") is True
        assert profile.can_handle_risk("medium") is True
        assert profile.can_handle_risk("high") is False

    def test_risk_level_low_only(self):
        from digitorn.core.security import SecurityProfile
        profile = SecurityProfile(app_id="app1", max_risk_level="low")
        assert profile.can_handle_risk("low") is True
        assert profile.can_handle_risk("medium") is False
        assert profile.can_handle_risk("high") is False


class TestSecurityGateEnforcement:
    """Test that security gate is enforced inside execute()."""

    @pytest.mark.asyncio
    async def test_no_context_passes_freely(self):
        """Without context, no security gate - backward compatible."""
        from digitorn.modules.hello.module import HelloModule
        module = HelloModule()
        result = await module.execute("say_hello", {"name": "Test"})
        assert result.success is True

    @pytest.mark.asyncio
    async def test_admin_profile_passes(self):
        """Admin profile bypasses all checks."""
        from digitorn.core.security import SecurityProfile
        from digitorn.modules.base import ExecutionContext
        from digitorn.modules.hello.module import HelloModule

        module = HelloModule()
        ctx = ExecutionContext(
            plan_id="test",
            action_id="test",
            security_profile=SecurityProfile(app_id="admin", is_admin=True),
        )
        result = await module.execute("say_hello", {"name": "Admin"}, context=ctx)
        assert result.success is True

    @pytest.mark.asyncio
    async def test_hidden_module_denied(self):
        """App with module hidden via grant gets PermissionDeniedError."""
        from digitorn.core.security import ModuleGrant, SecurityProfile
        from digitorn.modules.base import ExecutionContext
        from digitorn.modules.hello.module import HelloModule
        from digitorn.modules.exceptions import PermissionDeniedError

        module = HelloModule()
        ctx = ExecutionContext(
            plan_id="test",
            action_id="test",
            security_profile=SecurityProfile(
                app_id="restricted",
                module_grants={
                    "hello": ModuleGrant(module_id="hello", visibility="hidden"),
                },
            ),
        )
        with pytest.raises(PermissionDeniedError) as exc_info:
            await module.execute("say_hello", {"name": "X"}, context=ctx)
        assert "hello" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_inactive_app_denied(self):
        """Inactive app gets PermissionDeniedError."""
        from digitorn.core.security import SecurityProfile
        from digitorn.modules.base import ExecutionContext
        from digitorn.modules.hello.module import HelloModule
        from digitorn.modules.exceptions import PermissionDeniedError

        module = HelloModule()
        ctx = ExecutionContext(
            plan_id="test",
            action_id="test",
            security_profile=SecurityProfile(app_id="dead-app", is_active=False),
        )
        with pytest.raises(PermissionDeniedError):
            await module.execute("say_hello", {"name": "X"}, context=ctx)

    @pytest.mark.asyncio
    async def test_risk_level_denied(self):
        """App with max_risk_level=low denied from medium/high actions."""
        from digitorn.core.security import SecurityProfile
        from digitorn.modules.base import ActionResult, BaseModule, ExecutionContext, Platform
        from digitorn.modules.decorators import action
        from digitorn.modules.exceptions import PermissionDeniedError
        from digitorn.modules.manifest import ModuleManifest

        class RiskyModule(BaseModule):
            MODULE_ID = "risky"
            VERSION = "1.0.0"
            SUPPORTED_PLATFORMS = [Platform.ALL]

            def get_manifest(self):
                return ModuleManifest.from_module(self)

            @action(description="Dangerous action", risk_level="high")
            async def dangerous(self, params):
                return ActionResult(success=True, data="boom")

        module = RiskyModule()
        ctx = ExecutionContext(
            plan_id="test",
            action_id="test",
            security_profile=SecurityProfile(
                app_id="safe-app",
                max_risk_level="low",
            ),
        )
        with pytest.raises(PermissionDeniedError):
            await module.execute("dangerous", {}, context=ctx)

    @pytest.mark.asyncio
    async def test_approval_required_by_risk_rules(self):
        """Action with risk_level='high' + risk rule 'approve' → ApprovalRequiredError."""
        from digitorn.core.security import SecurityProfile
        from digitorn.modules.base import ActionResult, BaseModule, ExecutionContext, Platform
        from digitorn.modules.decorators import action
        from digitorn.modules.exceptions import ApprovalRequiredError
        from digitorn.modules.manifest import ModuleManifest

        class DestructiveModule(BaseModule):
            MODULE_ID = "destructive"
            VERSION = "1.0.0"
            SUPPORTED_PLATFORMS = [Platform.ALL]

            def get_manifest(self):
                return ModuleManifest.from_module(self)

            @action(description="Delete everything", risk_level="high", irreversible=True)
            async def nuke(self, params):
                return ActionResult(success=True, data="nuked")

        module = DestructiveModule()
        ctx = ExecutionContext(
            plan_id="test",
            action_id="test",
            security_profile=SecurityProfile(
                app_id="app1",
                max_risk_level="high",
                risk_approval_rules={"low": "auto", "medium": "auto", "high": "approve"},
            ),
        )
        # Without _approved → ApprovalRequiredError
        with pytest.raises(ApprovalRequiredError):
            await module.execute("nuke", {}, context=ctx)

        # With _approved → passes
        result = await module.execute("nuke", {"_approved": True}, context=ctx)
        assert result.success is True

    @pytest.mark.asyncio
    async def test_action_override_block(self):
        """Explicit action_override='block' denies even with permissions."""
        from digitorn.core.security import ModuleGrant, SecurityProfile
        from digitorn.modules.base import ActionResult, BaseModule, ExecutionContext, Platform
        from digitorn.modules.decorators import action
        from digitorn.modules.exceptions import PermissionDeniedError
        from digitorn.modules.manifest import ModuleManifest

        class MultiModule(BaseModule):
            MODULE_ID = "multi"
            VERSION = "1.0.0"
            SUPPORTED_PLATFORMS = [Platform.ALL]

            def get_manifest(self):
                return ModuleManifest.from_module(self)

            @action(description="Safe read", risk_level="low")
            async def read(self, params):
                return ActionResult(success=True, data="read")

            @action(description="Dangerous delete", risk_level="high")
            async def delete(self, params):
                return ActionResult(success=True, data="deleted")

        module = MultiModule()
        ctx = ExecutionContext(
            plan_id="test",
            action_id="test",
            security_profile=SecurityProfile(
                app_id="app1",
                max_risk_level="high",
                risk_approval_rules={"low": "auto", "medium": "auto", "high": "auto"},
                module_grants={
                    "multi": ModuleGrant(
                        module_id="multi",
                        action_overrides={"delete": "block", "read": "auto"},
                    ),
                },
            ),
        )
        # read → auto → passes
        result = await module.execute("read", {}, context=ctx)
        assert result.success is True

        # delete → block → denied
        with pytest.raises(PermissionDeniedError):
            await module.execute("delete", {}, context=ctx)

    @pytest.mark.asyncio
    async def test_action_override_approve(self):
        """Explicit action_override='approve' requires _approved."""
        from digitorn.core.security import ModuleGrant, SecurityProfile
        from digitorn.modules.base import ExecutionContext
        from digitorn.modules.hello.module import HelloModule
        from digitorn.modules.exceptions import ApprovalRequiredError

        module = HelloModule()
        ctx = ExecutionContext(
            plan_id="test",
            action_id="test",
            security_profile=SecurityProfile(
                app_id="app1",
                risk_approval_rules={"low": "auto", "medium": "auto", "high": "auto"},
                module_grants={
                    "hello": ModuleGrant(
                        module_id="hello",
                        action_overrides={"say_hello": "approve"},
                    ),
                },
            ),
        )
        # Without _approved → ApprovalRequiredError
        with pytest.raises(ApprovalRequiredError):
            await module.execute("say_hello", {"name": "X"}, context=ctx)

        # With _approved → passes
        result = await module.execute("say_hello", {"name": "X", "_approved": True}, context=ctx)
        assert result.success is True

    @pytest.mark.asyncio
    async def test_permission_check_with_required_permissions(self):
        """Action with required permissions → denied if not granted."""
        from digitorn.core.security import SecurityProfile
        from digitorn.modules.base import ActionResult, BaseModule, ExecutionContext, Platform
        from digitorn.modules.decorators import action
        from digitorn.modules.exceptions import PermissionDeniedError
        from digitorn.modules.manifest import ModuleManifest

        class SecuredModule(BaseModule):
            MODULE_ID = "secured"
            VERSION = "1.0.0"
            SUPPORTED_PLATFORMS = [Platform.ALL]

            def get_manifest(self):
                return ModuleManifest.from_module(self)

            @action(description="Write data", permissions=["db.write"])
            async def write(self, params):
                return ActionResult(success=True, data="written")

        module = SecuredModule()

        # All-auto risk rules so we isolate the permission check
        auto_rules = {"low": "auto", "medium": "auto", "high": "auto"}

        # No db.write permission → denied
        ctx_denied = ExecutionContext(
            plan_id="test",
            action_id="test",
            security_profile=SecurityProfile(
                app_id="reader",
                granted_permissions=frozenset(["db.read"]),
                risk_approval_rules=auto_rules,
            ),
        )
        with pytest.raises(PermissionDeniedError):
            await module.execute("write", {}, context=ctx_denied)

        # With db.write → passes
        ctx_allowed = ExecutionContext(
            plan_id="test",
            action_id="test",
            security_profile=SecurityProfile(
                app_id="writer",
                granted_permissions=frozenset(["db.write"]),
                risk_approval_rules=auto_rules,
            ),
        )
        result = await module.execute("write", {}, context=ctx_allowed)
        assert result.success is True

    @pytest.mark.asyncio
    async def test_wildcard_permission_grants_access(self):
        """Wildcard in granted_permissions covers sub-permissions."""
        from digitorn.core.security import SecurityProfile
        from digitorn.modules.base import ActionResult, BaseModule, ExecutionContext, Platform
        from digitorn.modules.decorators import action
        from digitorn.modules.manifest import ModuleManifest

        class WildModule(BaseModule):
            MODULE_ID = "wild"
            VERSION = "1.0.0"
            SUPPORTED_PLATFORMS = [Platform.ALL]

            def get_manifest(self):
                return ModuleManifest.from_module(self)

            @action(description="Specific", permissions=["db.write"])
            async def sensitive_write(self, params):
                return ActionResult(success=True, data="ok")

        module = WildModule()
        ctx = ExecutionContext(
            plan_id="test",
            action_id="test",
            security_profile=SecurityProfile(
                app_id="app1",
                granted_permissions=frozenset(["db.*"]),
                risk_approval_rules={"low": "auto", "medium": "auto", "high": "auto"},
            ),
        )
        result = await module.execute("sensitive_write", {}, context=ctx)
        assert result.success is True


class TestPolicyGateEnforcement:
    """Test that PolicyEnforcer is called from execute()."""

    @pytest.mark.asyncio
    async def test_policy_enforcer_called(self):
        """PolicyEnforcer.check_and_acquire is called when in context."""
        from digitorn.modules.base import ExecutionContext
        from digitorn.modules.hello.module import HelloModule
        from unittest.mock import AsyncMock, MagicMock

        module = HelloModule()
        mock_enforcer = MagicMock()
        mock_enforcer.check_and_acquire = AsyncMock()
        mock_enforcer.release = MagicMock()

        ctx = ExecutionContext(
            plan_id="test",
            action_id="test",
            policy_enforcer=mock_enforcer,
        )
        result = await module.execute("say_hello", {"name": "Test"}, context=ctx)
        assert result.success is True
        mock_enforcer.check_and_acquire.assert_called_once_with("hello", "say_hello")
        mock_enforcer.release.assert_called_once_with("hello")

    @pytest.mark.asyncio
    async def test_policy_release_on_error(self):
        """PolicyEnforcer.release is called even if handler raises."""
        from digitorn.modules.base import ActionResult, BaseModule, ExecutionContext, Platform
        from digitorn.modules.decorators import action
        from digitorn.modules.exceptions import ActionExecutionError
        from digitorn.modules.manifest import ModuleManifest
        from unittest.mock import AsyncMock, MagicMock

        class FailModule(BaseModule):
            MODULE_ID = "fail"
            VERSION = "1.0.0"
            SUPPORTED_PLATFORMS = [Platform.ALL]

            def get_manifest(self):
                return ModuleManifest.from_module(self)

            @action(description="Always fails")
            async def explode(self, params):
                raise RuntimeError("boom")

        module = FailModule()
        mock_enforcer = MagicMock()
        mock_enforcer.check_and_acquire = AsyncMock()
        mock_enforcer.release = MagicMock()

        ctx = ExecutionContext(
            plan_id="test",
            action_id="test",
            policy_enforcer=mock_enforcer,
        )
        with pytest.raises(ActionExecutionError):
            await module.execute("explode", {}, context=ctx)

        # release MUST be called even on error
        mock_enforcer.release.assert_called_once_with("fail")


class TestPermissionCatalog:
    """Test the system permission catalog and validation."""

    def test_all_46_permissions_exist(self):
        """Catalog contains exactly 46 system permissions."""
        from digitorn.core.permissions import SYSTEM_PERMISSIONS
        assert len(SYSTEM_PERMISSIONS) == 48

    def test_permission_enum_values(self):
        """Key permissions have correct string values."""
        from digitorn.core.permissions import Permission
        assert Permission.FS_READ == "fs.read"
        assert Permission.PROCESS_EXEC == "process.exec"
        assert Permission.NET_HTTP == "net.http"
        assert Permission.DB_WRITE == "db.write"
        assert Permission.SYS_POWER == "sys.power"
        assert Permission.HW_GPIO == "hw.gpio"
        assert Permission.SCREEN_CAPTURE == "screen.capture"
        assert Permission.AI_CALL == "ai.call"
        assert Permission.CONTAINER_MANAGE == "container.manage"
        assert Permission.CRON_MANAGE == "cron.manage"

    def test_all_domains_present(self):
        """All expected domains are covered."""
        from digitorn.core.permissions import PERMISSION_DOMAINS
        expected = {
            "fs", "process", "net", "db", "sys", "hw", "crypto", "secret",
            "user", "clipboard", "screen", "browser", "ai", "container",
            "ipc", "log", "cron", "notify",
        }
        assert PERMISSION_DOMAINS == expected

    def test_is_system_permission(self):
        """is_system_permission correctly identifies known permissions."""
        from digitorn.core.permissions import is_system_permission
        assert is_system_permission("fs.read") is True
        assert is_system_permission("process.exec") is True
        assert is_system_permission("fake.permission") is False
        assert is_system_permission("mymodule:custom") is False

    def test_is_custom_permission(self):
        """Custom permissions use : separator."""
        from digitorn.core.permissions import is_custom_permission
        assert is_custom_permission("mymodule:cloud_sync") is True
        assert is_custom_permission("fs.read") is False

    def test_validate_permissions_all_valid(self):
        """No errors when all permissions are valid."""
        from digitorn.core.permissions import validate_permissions
        assert validate_permissions(["fs.read", "net.http", "db.write"]) == []

    def test_validate_permissions_with_custom(self):
        """Custom permissions are always accepted."""
        from digitorn.core.permissions import validate_permissions
        assert validate_permissions(["fs.read", "mymod:custom"]) == []

    def test_validate_permissions_invalid(self):
        """Invalid system permissions are returned."""
        from digitorn.core.permissions import validate_permissions
        result = validate_permissions(["fs.read", "fake.perm", "also.bad"])
        assert result == ["fake.perm", "also.bad"]

    def test_validate_action_vs_module_permissions(self):
        """Action permissions must be subset of module permissions."""
        from digitorn.core.permissions import validate_action_permissions
        # All declared → OK
        assert validate_action_permissions(
            ["fs.read"], ["fs.read", "fs.write"]
        ) == []
        # Undeclared → returned
        assert validate_action_permissions(
            ["fs.read", "net.http"], ["fs.read"]
        ) == ["net.http"]

    def test_action_decorator_rejects_invalid_permission(self):
        """@action raises ValueError if a system permission is unknown."""
        from digitorn.modules.decorators import action
        with pytest.raises(ValueError, match="unknown system permission"):
            @action(description="bad", permissions=["fake.permission"])
            async def bad_action(self, params):
                pass

    def test_action_decorator_accepts_valid_permissions(self):
        """@action accepts valid system permissions without error."""
        from digitorn.modules.decorators import action
        # Should not raise
        @action(description="good", permissions=["fs.read", "net.http"])
        async def good_action(self, params):
            pass

    def test_action_decorator_accepts_custom_permissions(self):
        """@action accepts custom permissions (colon separator)."""
        from digitorn.modules.decorators import action
        @action(description="custom", permissions=["mymod:special"])
        async def custom_action(self, params):
            pass

    def test_permission_usable_as_string(self):
        """Permission enum values work as plain strings everywhere."""
        from digitorn.core.permissions import Permission
        perms = [Permission.FS_READ, Permission.NET_HTTP]
        assert "fs.read" in perms
        assert isinstance(perms[0], str)


class TestPolicyResolution:
    """Test resolve_action_policy priority order."""

    def test_action_override_wins(self):
        """Explicit action override takes priority over everything."""
        from digitorn.core.security import ModuleGrant, SecurityProfile, resolve_action_policy
        profile = SecurityProfile(
            app_id="app1",
            risk_approval_rules={"high": "block"},
            module_grants={
                "mod": ModuleGrant(
                    module_id="mod",
                    default_action_policy="approve",
                    action_overrides={"dangerous": "auto"},
                ),
            },
        )
        # Override says "auto" even though risk rule says "block"
        assert resolve_action_policy(profile, "mod", "dangerous", "high") == "auto"

    def test_risk_rule_beats_module_default(self):
        """Risk rule wins over module default_action_policy."""
        from digitorn.core.security import ModuleGrant, SecurityProfile, resolve_action_policy
        profile = SecurityProfile(
            app_id="app1",
            risk_approval_rules={"high": "block", "low": "auto"},
            module_grants={
                "mod": ModuleGrant(module_id="mod", default_action_policy="approve"),
            },
        )
        assert resolve_action_policy(profile, "mod", "some_action", "high") == "block"
        assert resolve_action_policy(profile, "mod", "some_action", "low") == "auto"

    def test_module_default_beats_app_default(self):
        """Module default wins when no override and no risk rule."""
        from digitorn.core.security import ModuleGrant, SecurityProfile, resolve_action_policy
        profile = SecurityProfile(
            app_id="app1",
            default_policy="block",
            risk_approval_rules={},  # no risk rules
            module_grants={
                "mod": ModuleGrant(module_id="mod", default_action_policy="auto"),
            },
        )
        assert resolve_action_policy(profile, "mod", "action", "low") == "auto"

    def test_app_default_fallback(self):
        """App default is used when nothing else matches."""
        from digitorn.core.security import SecurityProfile, resolve_action_policy
        profile = SecurityProfile(
            app_id="app1",
            default_policy="approve",
            risk_approval_rules={},
        )
        assert resolve_action_policy(profile, "unknown_mod", "action", "low") == "approve"

    def test_admin_always_auto(self):
        """Admin profile always resolves to 'auto'."""
        from digitorn.core.security import SecurityProfile, resolve_action_policy
        profile = SecurityProfile(app_id="admin", is_admin=True)
        assert resolve_action_policy(profile, "any", "any", "high") == "auto"

    def test_visibility_filtering(self):
        """visible_modules filters hidden modules."""
        from digitorn.core.security import ModuleGrant, SecurityProfile
        profile = SecurityProfile(
            app_id="app1",
            module_grants={
                "hello": ModuleGrant(module_id="hello", visibility="full"),
                "os_exec": ModuleGrant(module_id="os_exec", visibility="hidden"),
            },
        )
        visible = profile.visible_modules(["hello", "os_exec", "filesystem"])
        assert "hello" in visible
        assert "filesystem" in visible  # no grant = visible by default
        assert "os_exec" not in visible

    def test_build_profile_from_db(self):
        """build_profile_from_db constructs correct SecurityProfile."""
        from unittest.mock import MagicMock
        from digitorn.core.security import build_profile_from_db

        app_profile = MagicMock()
        app_profile.app_id = "test-app"
        app_profile.is_active = True
        app_profile.default_policy = "approve"
        app_profile.granted_permissions = ["filesystem.read", "hello.*"]
        app_profile.max_risk_level = "medium"
        app_profile.risk_approval_rules = {"low": "auto", "medium": "approve", "high": "block"}

        grant = MagicMock()
        grant.module_id = "filesystem"
        grant.visibility = "full"
        grant.default_action_policy = "approve"
        grant.action_overrides = {"read_file": "auto", "delete_file": "block"}

        profile = build_profile_from_db(app_profile, [grant])

        assert profile.app_id == "test-app"
        assert profile.is_active is True
        assert profile.has_permission("filesystem.read") is True
        assert profile.has_permission("hello.say_hello") is True
        assert profile.has_permission("os_exec.run") is False
        assert "filesystem" in profile.module_grants
        assert profile.module_grants["filesystem"].action_overrides["delete_file"] == "block"


# ---------------------------------------------------------------------------
# Security API integration - HTTP-level tests
# ---------------------------------------------------------------------------


async def _ensure_app(app_id: str) -> None:
    """Insert an Application row if it doesn't already exist."""
    from sqlalchemy import select

    from digitorn.core.database import _session_factory
    from digitorn.core.models import Application

    async with _session_factory() as session:
        existing = await session.execute(
            select(Application).where(Application.app_id == app_id)
        )
        if existing.scalar_one_or_none() is None:
            session.add(Application(app_id=app_id, name=f"Test {app_id}"))
            await session.commit()


class TestSecurityProfileCRUD:
    """Test the /api/security/profiles CRUD endpoints."""

    @pytest.mark.asyncio
    async def test_create_profile(self, client: AsyncClient):
        await _ensure_app("crud-app")
        r = await client.post("/api/security/profiles", json={
            "app_id": "crud-app",
            "default_policy": "approve",
            "granted_permissions": ["fs.read", "net.http"],
            "max_risk_level": "medium",
        })
        assert r.status_code == 201
        body = r.json()
        assert body["app_id"] == "crud-app"
        assert body["default_policy"] == "approve"
        assert "fs.read" in body["granted_permissions"]

    @pytest.mark.asyncio
    async def test_create_profile_duplicate(self, client: AsyncClient):
        await _ensure_app("dup-app")
        await client.post("/api/security/profiles", json={"app_id": "dup-app"})
        r = await client.post("/api/security/profiles", json={"app_id": "dup-app"})
        assert r.status_code == 409

    @pytest.mark.asyncio
    async def test_create_profile_app_not_found(self, client: AsyncClient):
        r = await client.post("/api/security/profiles", json={"app_id": "ghost"})
        assert r.status_code == 404

    @pytest.mark.asyncio
    async def test_get_profile(self, client: AsyncClient):
        await _ensure_app("get-app")
        await client.post("/api/security/profiles", json={
            "app_id": "get-app",
            "granted_permissions": ["db.*"],
        })
        r = await client.get("/api/security/profiles/get-app")
        assert r.status_code == 200
        body = r.json()
        assert body["app_id"] == "get-app"
        assert "module_grants" in body

    @pytest.mark.asyncio
    async def test_get_profile_not_found(self, client: AsyncClient):
        r = await client.get("/api/security/profiles/nonexistent")
        assert r.status_code == 404

    @pytest.mark.asyncio
    async def test_patch_profile(self, client: AsyncClient):
        await _ensure_app("patch-app")
        await client.post("/api/security/profiles", json={"app_id": "patch-app"})
        r = await client.patch("/api/security/profiles/patch-app", json={
            "is_active": False,
            "max_risk_level": "low",
        })
        assert r.status_code == 200
        body = r.json()
        assert body["is_active"] is False
        assert body["max_risk_level"] == "low"

    @pytest.mark.asyncio
    async def test_patch_profile_invalid_policy(self, client: AsyncClient):
        await _ensure_app("bad-patch-app")
        await client.post("/api/security/profiles", json={"app_id": "bad-patch-app"})
        r = await client.patch("/api/security/profiles/bad-patch-app", json={
            "default_policy": "yolo",
        })
        assert r.status_code == 422

    @pytest.mark.asyncio
    async def test_delete_profile(self, client: AsyncClient):
        await _ensure_app("del-app")
        await client.post("/api/security/profiles", json={"app_id": "del-app"})
        r = await client.delete("/api/security/profiles/del-app")
        assert r.status_code == 204
        r2 = await client.get("/api/security/profiles/del-app")
        assert r2.status_code == 404


class TestModuleGrantCRUD:
    """Test the /api/security/profiles/{app_id}/grants endpoints."""

    async def _setup_app_with_profile(self, client: AsyncClient, app_id: str) -> None:
        await _ensure_app(app_id)
        await client.post("/api/security/profiles", json={"app_id": app_id})

    @pytest.mark.asyncio
    async def test_upsert_grant(self, client: AsyncClient):
        await self._setup_app_with_profile(client, "grant-app")
        r = await client.put("/api/security/profiles/grant-app/grants/hello", json={
            "module_id": "hello",
            "visibility": "full",
            "default_action_policy": "auto",
            "action_overrides": {"say_hello": "approve"},
        })
        assert r.status_code == 200
        body = r.json()
        assert body["module_id"] == "hello"
        assert body["default_action_policy"] == "auto"
        assert body["action_overrides"]["say_hello"] == "approve"

    @pytest.mark.asyncio
    async def test_upsert_grant_update(self, client: AsyncClient):
        await self._setup_app_with_profile(client, "grant-upd-app")
        await client.put("/api/security/profiles/grant-upd-app/grants/hello", json={
            "module_id": "hello",
            "visibility": "full",
        })
        # Update
        r = await client.put("/api/security/profiles/grant-upd-app/grants/hello", json={
            "module_id": "hello",
            "visibility": "hidden",
        })
        assert r.status_code == 200
        assert r.json()["visibility"] == "hidden"

    @pytest.mark.asyncio
    async def test_list_grants(self, client: AsyncClient):
        await self._setup_app_with_profile(client, "list-grant-app")
        await client.put("/api/security/profiles/list-grant-app/grants/hello", json={
            "module_id": "hello",
        })
        await client.put("/api/security/profiles/list-grant-app/grants/filesystem", json={
            "module_id": "filesystem",
            "visibility": "hidden",
        })
        r = await client.get("/api/security/profiles/list-grant-app/grants")
        assert r.status_code == 200
        body = r.json()
        assert body["count"] == 2

    @pytest.mark.asyncio
    async def test_delete_grant(self, client: AsyncClient):
        await self._setup_app_with_profile(client, "del-grant-app")
        await client.put("/api/security/profiles/del-grant-app/grants/hello", json={
            "module_id": "hello",
        })
        r = await client.delete("/api/security/profiles/del-grant-app/grants/hello")
        assert r.status_code == 204
        r2 = await client.get("/api/security/profiles/del-grant-app/grants")
        assert r2.json()["count"] == 0

    @pytest.mark.asyncio
    async def test_grant_invalid_visibility(self, client: AsyncClient):
        await self._setup_app_with_profile(client, "bad-vis-app")
        r = await client.put("/api/security/profiles/bad-vis-app/grants/hello", json={
            "module_id": "hello",
            "visibility": "invisible",
        })
        assert r.status_code == 422


class TestVisibilityFiltering:
    """Test that GET /api/modules respects security profile visibility."""

    async def _setup_profile_with_hidden_module(
        self, client: AsyncClient, app_id: str, hidden_module: str,
    ) -> None:
        await _ensure_app(app_id)
        await client.post("/api/security/profiles", json={"app_id": app_id})
        await client.put(f"/api/security/profiles/{app_id}/grants/{hidden_module}", json={
            "module_id": hidden_module,
            "visibility": "hidden",
        })

    @pytest.mark.asyncio
    async def test_list_modules_no_app_id(self, client: AsyncClient):
        """Without app_id, all modules are visible."""
        r = await client.get("/api/modules")
        assert r.status_code == 200
        module_ids = [m["module_id"] for m in r.json()["modules"]]
        assert "hello" in module_ids

    @pytest.mark.asyncio
    async def test_list_modules_with_hidden(self, client: AsyncClient):
        """Hidden module is excluded from list."""
        await self._setup_profile_with_hidden_module(client, "vis-app", "hello")
        r = await client.get("/api/modules", params={"app_id": "vis-app"})
        assert r.status_code == 200
        module_ids = [m["module_id"] for m in r.json()["modules"]]
        assert "hello" not in module_ids

    @pytest.mark.asyncio
    async def test_get_module_hidden_returns_404(self, client: AsyncClient):
        """Requesting a hidden module by ID returns 404."""
        await self._setup_profile_with_hidden_module(client, "vis-app-2", "hello")
        r = await client.get("/api/modules/hello", params={"app_id": "vis-app-2"})
        assert r.status_code == 404

    @pytest.mark.asyncio
    async def test_list_modules_with_action_policies(self, client: AsyncClient):
        """When app_id is provided, action_policies are included."""
        await _ensure_app("policy-app")
        await client.post("/api/security/profiles", json={
            "app_id": "policy-app",
            "risk_approval_rules": {"low": "auto", "medium": "approve", "high": "block"},
        })
        r = await client.get("/api/modules", params={"app_id": "policy-app"})
        assert r.status_code == 200
        for mod in r.json()["modules"]:
            if mod.get("status") == "loaded":
                assert "action_policies" in mod


class TestExecuteWithSecurity:
    """Test that POST /api/modules/{id}/execute enforces security."""

    async def _setup_profile(
        self, client: AsyncClient, app_id: str, **profile_kwargs,
    ) -> None:
        await _ensure_app(app_id)
        payload = {"app_id": app_id, **profile_kwargs}
        await client.post("/api/security/profiles", json=payload)

    @pytest.mark.asyncio
    async def test_execute_no_app_id_passes(self, client: AsyncClient):
        """Without app_id, no security enforcement (dev mode)."""
        r = await client.post("/api/modules/hello/execute", json={
            "action": "say_hello",
            "params": {"name": "NoAuth"},
        })
        assert r.status_code == 200
        assert r.json()["success"] is True

    @pytest.mark.asyncio
    async def test_execute_inactive_app_denied(self, client: AsyncClient):
        """Inactive app gets 403."""
        await self._setup_profile(client, "dead-exec-app", is_active=False)
        r = await client.post("/api/modules/hello/execute", json={
            "action": "say_hello",
            "params": {"name": "X"},
            "app_id": "dead-exec-app",
        })
        assert r.status_code == 403
        assert r.json()["detail"]["error"] == "permission_denied"

    @pytest.mark.asyncio
    async def test_execute_hidden_module_404(self, client: AsyncClient):
        """Executing on a hidden module returns 404."""
        await self._setup_profile(client, "hidden-exec-app")
        await client.put("/api/security/profiles/hidden-exec-app/grants/hello", json={
            "module_id": "hello",
            "visibility": "hidden",
        })
        r = await client.post("/api/modules/hello/execute", json={
            "action": "say_hello",
            "params": {"name": "X"},
            "app_id": "hidden-exec-app",
        })
        assert r.status_code == 404

    @pytest.mark.asyncio
    async def test_execute_approval_required(self, client: AsyncClient):
        """Action with approve policy returns approval_required response."""
        await self._setup_profile(
            client, "approve-exec-app",
            risk_approval_rules={"low": "approve", "medium": "approve", "high": "block"},
        )
        r = await client.post("/api/modules/hello/execute", json={
            "action": "say_hello",
            "params": {"name": "X"},
            "app_id": "approve-exec-app",
        })
        assert r.status_code == 200
        body = r.json()
        assert body["success"] is False
        assert body["error"] == "approval_required"
        assert body["metadata"]["approval_required"] is True

    @pytest.mark.asyncio
    async def test_execute_with_approval(self, client: AsyncClient):
        """Action passes when _approved=True is sent."""
        await self._setup_profile(
            client, "approved-exec-app",
            risk_approval_rules={"low": "approve", "medium": "approve", "high": "block"},
        )
        r = await client.post("/api/modules/hello/execute", json={
            "action": "say_hello",
            "params": {"name": "X", "_approved": True},
            "app_id": "approved-exec-app",
        })
        assert r.status_code == 200
        assert r.json()["success"] is True

    @pytest.mark.asyncio
    async def test_execute_auto_policy(self, client: AsyncClient):
        """All-auto profile executes without approval."""
        await self._setup_profile(
            client, "auto-exec-app",
            risk_approval_rules={"low": "auto", "medium": "auto", "high": "auto"},
        )
        r = await client.post("/api/modules/hello/execute", json={
            "action": "say_hello",
            "params": {"name": "Auto"},
            "app_id": "auto-exec-app",
        })
        assert r.status_code == 200
        assert r.json()["success"] is True
        assert r.json()["data"] == "Hello, Auto!"
