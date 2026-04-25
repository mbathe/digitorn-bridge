"""Tests for per-user channel resolution (UserResolver + registry integration).

Covers:
- _inject_session_id placeholder replacement
- UserResolverConfig construction
- UserResolver: query, mapping, caching, error handling
- ChannelRegistry.deliver() with session_id (YAML resolver path)
- ChannelRegistry.deliver() with session_id (channel resolve_recipient path)
- DeliveryContext data structure
- End-to-end: resolver → module → mapping → deliver
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any

import pytest

from digitorn.core.app.channels.base import (
    BaseOutputChannel,
    ChannelPayload,
    DeliveryContext,
    DeliveryResult,
)
from digitorn.core.app.channels.registry import ChannelRegistry
from digitorn.core.app.channels.resolver import (
    UserResolver,
    UserResolverConfig,
    _inject_session_id,
)


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


class DummyChannel(BaseOutputChannel):
    """Minimal channel for testing."""

    CHANNEL_ID = "dummy"
    CHANNEL_NAME = "Dummy"
    CHANNEL_VERSION = "1.0.0"

    def __init__(self, **kwargs: Any):
        super().__init__(**kwargs)
        self.deliveries: list[tuple[str, ChannelPayload, dict]] = []

    async def deliver(
        self, app_id: str, payload: ChannelPayload, config: dict[str, Any]
    ) -> DeliveryResult:
        self.deliveries.append((app_id, payload, config))
        return DeliveryResult(success=True, channel_id=self.CHANNEL_ID)


class ResolverChannel(BaseOutputChannel):
    """Channel with custom resolve_recipient for testing the fallback path."""

    CHANNEL_ID = "resolver_ch"
    CHANNEL_NAME = "Resolver Channel"
    CHANNEL_VERSION = "1.0.0"

    def __init__(self, **kwargs: Any):
        super().__init__(**kwargs)
        self.deliveries: list[tuple[str, ChannelPayload, dict]] = []

    async def resolve_recipient(self, context: DeliveryContext) -> dict[str, Any]:
        """Look up user from channel_config['users'] map."""
        users = self.channel_config.get("users", {})
        if context.session_id and context.session_id in users:
            resolved = dict(users[context.session_id])
            resolved.update(context.output_config)
            return resolved
        return context.output_config

    async def deliver(
        self, app_id: str, payload: ChannelPayload, config: dict[str, Any]
    ) -> DeliveryResult:
        self.deliveries.append((app_id, payload, config))
        return DeliveryResult(success=True, channel_id=self.CHANNEL_ID)


class FakeModule:
    """Simulates a module with execute(action, params) for resolver queries."""

    def __init__(self, rows: list[dict[str, Any]] | None = None):
        self.rows = rows or []
        self.calls: list[tuple[str, dict]] = []

    async def execute(self, action: str, params: dict[str, Any]) -> Any:
        self.calls.append((action, params))
        return self.rows


class FailingModule:
    """Module that raises on execute."""

    async def execute(self, action: str, params: dict[str, Any]) -> Any:
        raise RuntimeError("DB connection refused")


@dataclass
class FakeActionResult:
    """Simulates modules.base.ActionResult."""

    success: bool
    data: Any = None
    error: str = ""


class ActionResultModule:
    """Module that returns ActionResult instead of raw data."""

    def __init__(self, result: FakeActionResult):
        self._result = result

    async def execute(self, action: str, params: dict[str, Any]) -> Any:
        return self._result


# ---------------------------------------------------------------------------
# _inject_session_id tests
# ---------------------------------------------------------------------------


class TestInjectSessionId:
    def test_colon_placeholder(self):
        params = {"query": "SELECT * FROM users WHERE sid = :session_id"}
        result = _inject_session_id(params, "user-42")
        assert result["query"] == "SELECT * FROM users WHERE sid = user-42"

    def test_template_placeholder(self):
        params = {"url": "https://api.example.com/users/{{session_id}}"}
        result = _inject_session_id(params, "user-42")
        assert result["url"] == "https://api.example.com/users/user-42"

    def test_both_placeholders(self):
        params = {"q": ":session_id and {{session_id}}"}
        result = _inject_session_id(params, "abc")
        assert result["q"] == "abc and abc"

    def test_no_placeholder(self):
        params = {"query": "SELECT 1", "limit": 10}
        result = _inject_session_id(params, "user-42")
        assert result == {"query": "SELECT 1", "limit": 10}

    def test_nested_dict(self):
        params = {"body": {"filter": {"session": ":session_id"}}}
        result = _inject_session_id(params, "user-42")
        assert result["body"]["filter"]["session"] == "user-42"

    def test_non_string_values_untouched(self):
        params = {"limit": 10, "active": True, "tags": ["a", "b"]}
        result = _inject_session_id(params, "user-42")
        assert result["limit"] == 10
        assert result["active"] is True
        assert result["tags"] == ["a", "b"]

    def test_empty_params(self):
        assert _inject_session_id({}, "user-42") == {}

    def test_original_not_mutated(self):
        params = {"q": ":session_id"}
        result = _inject_session_id(params, "user-42")
        assert params["q"] == ":session_id"
        assert result["q"] == "user-42"


# ---------------------------------------------------------------------------
# UserResolverConfig tests
# ---------------------------------------------------------------------------


class TestUserResolverConfig:
    def test_basic_construction(self):
        cfg = UserResolverConfig(module="database", action="fetch_results")
        assert cfg.module == "database"
        assert cfg.action == "fetch_results"
        assert cfg.params == {}
        assert cfg.mapping == {}
        assert cfg.cache_ttl == 300.0

    def test_from_dict(self):
        cfg = UserResolverConfig.from_dict({
            "module": "database",
            "action": "fetch_results",
            "params": {"query": "SELECT phone FROM users WHERE sid = :session_id"},
            "mapping": {"to_number": "phone"},
            "cache_ttl": 60.0,
        })
        assert cfg.module == "database"
        assert cfg.mapping == {"to_number": "phone"}
        assert cfg.cache_ttl == 60.0

    def test_from_dict_ignores_extra_keys(self):
        cfg = UserResolverConfig.from_dict({
            "module": "database",
            "action": "fetch_results",
            "unknown_key": "ignored",
        })
        assert cfg.module == "database"
        assert not hasattr(cfg, "unknown_key")


# ---------------------------------------------------------------------------
# UserResolver tests
# ---------------------------------------------------------------------------


class TestUserResolver:
    def _make_resolver(
        self,
        module_data: list[dict] | None = None,
        mapping: dict[str, str] | None = None,
        cache_ttl: float = 0.0,
        module_id: str = "database",
    ) -> tuple[UserResolver, FakeModule]:
        module = FakeModule(rows=module_data or [])
        config = UserResolverConfig(
            module=module_id,
            action="fetch_results",
            params={"query": "SELECT * FROM users WHERE sid = :session_id"},
            mapping=mapping or {},
            cache_ttl=cache_ttl,
        )
        resolver = UserResolver(config, modules={module_id: module})
        return resolver, module

    @pytest.mark.asyncio
    async def test_resolve_no_session_id(self):
        resolver, _ = self._make_resolver()
        result = await resolver.resolve(None, {"to_number": "+33123"})
        assert result == {"to_number": "+33123"}

    @pytest.mark.asyncio
    async def test_resolve_no_session_id_no_config(self):
        resolver, _ = self._make_resolver()
        result = await resolver.resolve(None)
        assert result == {}

    @pytest.mark.asyncio
    async def test_resolve_basic_mapping(self):
        resolver, module = self._make_resolver(
            module_data=[{"phone": "+33612345678", "email": "user@example.com"}],
            mapping={"to_number": "phone", "to_address": "email"},
        )
        result = await resolver.resolve("user-42")
        assert result == {
            "to_number": "+33612345678",
            "to_address": "user@example.com",
        }
        # Verify the module was called with session_id injected
        assert len(module.calls) == 1
        action, params = module.calls[0]
        assert action == "fetch_results"
        assert "user-42" in params["query"]

    @pytest.mark.asyncio
    async def test_resolve_explicit_config_overrides(self):
        """Explicit output_config should override auto-resolved values."""
        resolver, _ = self._make_resolver(
            module_data=[{"phone": "+33612345678"}],
            mapping={"to_number": "phone"},
        )
        result = await resolver.resolve("user-42", {"to_number": "+33999999999"})
        # Explicit value wins
        assert result["to_number"] == "+33999999999"

    @pytest.mark.asyncio
    async def test_resolve_partial_mapping(self):
        """Only mapped fields that exist in the result are included."""
        resolver, _ = self._make_resolver(
            module_data=[{"phone": "+33612345678"}],
            mapping={"to_number": "phone", "to_address": "email"},
        )
        result = await resolver.resolve("user-42")
        assert result == {"to_number": "+33612345678"}
        assert "to_address" not in result

    @pytest.mark.asyncio
    async def test_resolve_empty_result(self):
        """No rows returned → fall back to output_config."""
        resolver, _ = self._make_resolver(
            module_data=[],
            mapping={"to_number": "phone"},
        )
        result = await resolver.resolve("user-42", {"fallback": "yes"})
        assert result == {"fallback": "yes"}

    @pytest.mark.asyncio
    async def test_resolve_dict_result(self):
        """Module returns a single dict instead of list of rows."""
        config = UserResolverConfig(
            module="http",
            action="get",
            params={"url": "https://api.example.com/users/:session_id"},
            mapping={"to_number": "phone"},
        )
        module = FakeModule()
        # Override to return a dict directly
        async def execute_dict(action, params):
            module.calls.append((action, params))
            return {"phone": "+33612345678", "name": "Alice"}
        module.execute = execute_dict

        resolver = UserResolver(config, modules={"http": module})
        result = await resolver.resolve("user-42")
        assert result == {"to_number": "+33612345678"}

    @pytest.mark.asyncio
    async def test_resolve_module_not_found(self):
        """Missing module → fall back to output_config."""
        config = UserResolverConfig(module="nonexistent", action="get")
        resolver = UserResolver(config, modules={})
        result = await resolver.resolve("user-42", {"fallback": "yes"})
        assert result == {"fallback": "yes"}

    @pytest.mark.asyncio
    async def test_resolve_module_exception(self):
        """Module raises → fall back to output_config."""
        config = UserResolverConfig(
            module="database",
            action="fetch_results",
            mapping={"to_number": "phone"},
        )
        resolver = UserResolver(config, modules={"database": FailingModule()})
        result = await resolver.resolve("user-42", {"safe": True})
        assert result == {"safe": True}

    @pytest.mark.asyncio
    async def test_resolve_action_result_success(self):
        """Module returns ActionResult with success=True."""
        config = UserResolverConfig(
            module="database",
            action="fetch_results",
            mapping={"to_number": "phone"},
        )
        ar = FakeActionResult(
            success=True,
            data=[{"phone": "+33612345678"}],
        )
        resolver = UserResolver(
            config, modules={"database": ActionResultModule(ar)}
        )
        result = await resolver.resolve("user-42")
        assert result == {"to_number": "+33612345678"}

    @pytest.mark.asyncio
    async def test_resolve_action_result_failure(self):
        """Module returns ActionResult with success=False."""
        config = UserResolverConfig(
            module="database",
            action="fetch_results",
            mapping={"to_number": "phone"},
        )
        ar = FakeActionResult(success=False, error="table not found")
        resolver = UserResolver(
            config, modules={"database": ActionResultModule(ar)}
        )
        result = await resolver.resolve("user-42", {"fallback": True})
        assert result == {"fallback": True}

    # --- Caching ---

    @pytest.mark.asyncio
    async def test_cache_hit(self):
        resolver, module = self._make_resolver(
            module_data=[{"phone": "+33612345678"}],
            mapping={"to_number": "phone"},
            cache_ttl=300.0,
        )
        # First call → queries module
        r1 = await resolver.resolve("user-42")
        assert r1 == {"to_number": "+33612345678"}
        assert len(module.calls) == 1

        # Second call → cache hit
        r2 = await resolver.resolve("user-42")
        assert r2 == {"to_number": "+33612345678"}
        assert len(module.calls) == 1  # No second query

    @pytest.mark.asyncio
    async def test_cache_disabled(self):
        resolver, module = self._make_resolver(
            module_data=[{"phone": "+33612345678"}],
            mapping={"to_number": "phone"},
            cache_ttl=0.0,
        )
        await resolver.resolve("user-42")
        await resolver.resolve("user-42")
        assert len(module.calls) == 2  # Queried both times

    @pytest.mark.asyncio
    async def test_cache_per_session(self):
        resolver, module = self._make_resolver(
            module_data=[{"phone": "+33612345678"}],
            mapping={"to_number": "phone"},
            cache_ttl=300.0,
        )
        await resolver.resolve("user-1")
        await resolver.resolve("user-2")
        assert len(module.calls) == 2  # Different sessions = different queries

    @pytest.mark.asyncio
    async def test_cache_clear_specific(self):
        resolver, module = self._make_resolver(
            module_data=[{"phone": "+33612345678"}],
            mapping={"to_number": "phone"},
            cache_ttl=300.0,
        )
        await resolver.resolve("user-42")
        resolver.clear_cache("user-42")
        await resolver.resolve("user-42")
        assert len(module.calls) == 2  # Cache was cleared

    @pytest.mark.asyncio
    async def test_cache_clear_all(self):
        resolver, module = self._make_resolver(
            module_data=[{"phone": "+33612345678"}],
            mapping={"to_number": "phone"},
            cache_ttl=300.0,
        )
        await resolver.resolve("user-1")
        await resolver.resolve("user-2")
        resolver.clear_cache()
        await resolver.resolve("user-1")
        await resolver.resolve("user-2")
        assert len(module.calls) == 4

    def test_set_modules(self):
        config = UserResolverConfig(module="database", action="fetch_results")
        resolver = UserResolver(config)
        assert resolver._modules == {}
        module = FakeModule()
        resolver.set_modules({"database": module})
        assert resolver._modules["database"] is module


# ---------------------------------------------------------------------------
# DeliveryContext tests
# ---------------------------------------------------------------------------


class TestDeliveryContext:
    def test_basic(self):
        ctx = DeliveryContext(app_id="myapp")
        assert ctx.app_id == "myapp"
        assert ctx.session_id is None
        assert ctx.output_config == {}

    def test_with_session(self):
        ctx = DeliveryContext(
            app_id="myapp",
            session_id="user-42",
            output_config={"to_number": "+33123"},
        )
        assert ctx.session_id == "user-42"
        assert ctx.output_config["to_number"] == "+33123"


# ---------------------------------------------------------------------------
# ChannelRegistry + resolver integration tests
# ---------------------------------------------------------------------------


class TestRegistryWithResolver:
    """Tests for ChannelRegistry.deliver() with session_id."""

    @pytest.mark.asyncio
    async def test_deliver_with_yaml_resolver(self):
        """YAML user_resolver path: registry uses UserResolver."""
        reg = ChannelRegistry()
        reg.register_type(DummyChannel)
        reg.create_instance(
            "sms_ch", "dummy", {},
            resolver_config={
                "module": "database",
                "action": "fetch_results",
                "params": {"query": "SELECT phone FROM users WHERE sid = :session_id"},
                "mapping": {"to_number": "phone"},
                "cache_ttl": 0,
            },
        )
        # Inject module into resolver
        module = FakeModule(rows=[{"phone": "+33612345678"}])
        reg.set_resolver_modules("sms_ch", {"database": module})

        payload = ChannelPayload(message="Hello")
        result = await reg.deliver(
            "sms_ch", "app1", payload, session_id="user-42"
        )
        assert result.success

        # Check that the channel received the resolved config
        inst = reg.get_instance("sms_ch")
        _, _, config = inst.deliveries[0]
        assert config == {"to_number": "+33612345678"}

    @pytest.mark.asyncio
    async def test_deliver_without_session_id(self):
        """No session_id → resolver is not called, config passed through."""
        reg = ChannelRegistry()
        reg.register_type(DummyChannel)
        reg.create_instance(
            "sms_ch", "dummy", {},
            resolver_config={
                "module": "database",
                "action": "fetch_results",
                "mapping": {"to_number": "phone"},
            },
        )
        module = FakeModule(rows=[{"phone": "+33612345678"}])
        reg.set_resolver_modules("sms_ch", {"database": module})

        result = await reg.deliver(
            "sms_ch", "app1", ChannelPayload(message="test"),
            {"manual_field": "val"},
        )
        assert result.success
        inst = reg.get_instance("sms_ch")
        _, _, config = inst.deliveries[0]
        assert config == {"manual_field": "val"}
        # Module was NOT queried
        assert len(module.calls) == 0

    @pytest.mark.asyncio
    async def test_deliver_resolver_error_fallback(self):
        """Resolver error → fall back to explicit config."""
        reg = ChannelRegistry()
        reg.register_type(DummyChannel)
        reg.create_instance(
            "sms_ch", "dummy", {},
            resolver_config={
                "module": "database",
                "action": "fetch_results",
                "mapping": {"to_number": "phone"},
            },
        )
        # Inject a failing module
        reg.set_resolver_modules("sms_ch", {"database": FailingModule()})

        result = await reg.deliver(
            "sms_ch", "app1", ChannelPayload(message="test"),
            {"to_number": "+33fallback"},
            session_id="user-42",
        )
        assert result.success
        inst = reg.get_instance("sms_ch")
        _, _, config = inst.deliveries[0]
        assert config == {"to_number": "+33fallback"}

    @pytest.mark.asyncio
    async def test_deliver_channel_resolve_recipient(self):
        """No YAML resolver → channel's own resolve_recipient is called."""
        reg = ChannelRegistry()
        reg.register_type(ResolverChannel)

        users_map = {
            "user-42": {"to_number": "+33612345678", "name": "Alice"},
        }
        reg.create_instance(
            "my_ch", "resolver_ch",
            {"users": users_map},
        )

        result = await reg.deliver(
            "my_ch", "app1", ChannelPayload(message="test"),
            session_id="user-42",
        )
        assert result.success
        inst = reg.get_instance("my_ch")
        _, _, config = inst.deliveries[0]
        assert config["to_number"] == "+33612345678"
        assert config["name"] == "Alice"

    @pytest.mark.asyncio
    async def test_deliver_channel_resolve_recipient_unknown_user(self):
        """resolve_recipient with unknown session_id → empty config."""
        reg = ChannelRegistry()
        reg.register_type(ResolverChannel)
        reg.create_instance("my_ch", "resolver_ch", {"users": {}})

        result = await reg.deliver(
            "my_ch", "app1", ChannelPayload(message="test"),
            {"fallback": True},
            session_id="unknown-user",
        )
        assert result.success
        inst = reg.get_instance("my_ch")
        _, _, config = inst.deliveries[0]
        assert config == {"fallback": True}

    @pytest.mark.asyncio
    async def test_yaml_resolver_takes_priority_over_channel(self):
        """YAML user_resolver takes priority over channel.resolve_recipient."""
        reg = ChannelRegistry()
        reg.register_type(ResolverChannel)

        # Channel has its own resolve_recipient with a users map
        reg.create_instance(
            "my_ch", "resolver_ch",
            {"users": {"user-42": {"to_number": "+33channel"}}},
            resolver_config={
                "module": "database",
                "action": "fetch_results",
                "mapping": {"to_number": "phone"},
                "cache_ttl": 0,
            },
        )
        module = FakeModule(rows=[{"phone": "+33resolver"}])
        reg.set_resolver_modules("my_ch", {"database": module})

        result = await reg.deliver(
            "my_ch", "app1", ChannelPayload(message="test"),
            session_id="user-42",
        )
        assert result.success
        inst = reg.get_instance("my_ch")
        _, _, config = inst.deliveries[0]
        # YAML resolver wins over channel's resolve_recipient
        assert config["to_number"] == "+33resolver"

    @pytest.mark.asyncio
    async def test_deliver_multi_with_session_id(self):
        """deliver_multi passes session_id to each channel."""
        reg = ChannelRegistry()
        reg.register_type(DummyChannel)
        reg.register_type(ResolverChannel)

        reg.create_instance("ch1", "dummy", {})
        reg.create_instance(
            "ch2", "resolver_ch",
            {"users": {"user-42": {"target": "alice"}}},
        )

        results = await reg.deliver_multi(
            ["ch1", "ch2"], "app1",
            ChannelPayload(message="fanout"),
            session_id="user-42",
        )
        assert len(results) == 2
        assert all(r.success for r in results)

        # ch2 should have resolved the user
        inst2 = reg.get_instance("ch2")
        _, _, config = inst2.deliveries[0]
        assert config["target"] == "alice"

    @pytest.mark.asyncio
    async def test_remove_instance_cleans_resolver(self):
        """remove_instance should also remove the resolver."""
        reg = ChannelRegistry()
        reg.register_type(DummyChannel)
        reg.create_instance(
            "sms_ch", "dummy", {},
            resolver_config={
                "module": "database",
                "action": "fetch_results",
                "mapping": {"to_number": "phone"},
            },
        )
        assert reg.get_resolver("sms_ch") is not None
        reg.remove_instance("sms_ch")
        assert reg.get_resolver("sms_ch") is None


# ---------------------------------------------------------------------------
# Schema integration test
# ---------------------------------------------------------------------------


class TestSchemaUserResolver:
    def test_channel_instance_config_with_resolver(self):
        from digitorn.core.app.schema import ChannelInstanceConfig

        cfg = ChannelInstanceConfig(
            type="webhook",
            config={"url": "https://example.com"},
            user_resolver={
                "module": "database",
                "action": "fetch_results",
                "params": {"query": "SELECT phone FROM users WHERE sid = :session_id"},
                "mapping": {"to_number": "phone"},
                "cache_ttl": 120,
            },
        )
        assert cfg.user_resolver is not None
        assert cfg.user_resolver.module == "database"
        assert cfg.user_resolver.mapping == {"to_number": "phone"}
        assert cfg.user_resolver.cache_ttl == 120

    def test_channel_instance_config_without_resolver(self):
        from digitorn.core.app.schema import ChannelInstanceConfig

        cfg = ChannelInstanceConfig(
            type="webhook",
            config={"url": "https://example.com"},
        )
        assert cfg.user_resolver is None
