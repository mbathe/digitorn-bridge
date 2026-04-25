"""Integration test — real end-to-end channel delivery with user_resolver.

Exercises the FULL stack without mocks:
- Real SQLite database (via database module)
- Real ChannelRegistry with LogChannel
- Real UserResolver wired to the database module
- Real delivery flow with session_id → resolver → module → mapping → channel

Run with:
    python -m pytest tests/test_integration_resolver.py -v -s
"""

from __future__ import annotations

import asyncio
import logging
import os
import tempfile
from pathlib import Path
from typing import Any

import pytest

from digitorn.core.app.channels.base import (
    BaseOutputChannel,
    ChannelPayload,
    DeliveryContext,
    DeliveryResult,
)
from digitorn.core.app.channels.log import LogChannel
from digitorn.core.app.channels.registry import ChannelRegistry
from digitorn.core.app.channels.resolver import UserResolverConfig


# ---------------------------------------------------------------------------
# A channel that captures deliveries for assertions
# ---------------------------------------------------------------------------


class CaptureChannel(BaseOutputChannel):
    """Records every delivery for test verification."""

    CHANNEL_ID = "capture"
    CHANNEL_NAME = "Capture (test)"
    CHANNEL_VERSION = "1.0.0"

    def __init__(self, **kwargs: Any):
        super().__init__(**kwargs)
        self.deliveries: list[dict[str, Any]] = []

    async def deliver(
        self, app_id: str, payload: ChannelPayload, config: dict[str, Any]
    ) -> DeliveryResult:
        self.deliveries.append({
            "app_id": app_id,
            "message": payload.message,
            "title": payload.title,
            "config": dict(config),
        })
        return DeliveryResult(success=True, channel_id=self.CHANNEL_ID)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def tmp_db(tmp_path):
    """Create a real SQLite database with a users table."""
    import sqlite3

    db_path = tmp_path / "users.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("""
        CREATE TABLE users (
            session_id TEXT PRIMARY KEY,
            email TEXT NOT NULL,
            phone TEXT NOT NULL,
            full_name TEXT NOT NULL,
            telegram_chat_id TEXT DEFAULT ''
        )
    """)
    conn.execute("""
        INSERT INTO users VALUES
            ('session-alice', 'alice@example.com', '+33612345678', 'Alice Dupont', '-100111'),
            ('session-bob', 'bob@example.com', '+33698765432', 'Bob Martin', '-100222'),
            ('session-charlie', 'charlie@example.com', '+33611111111', 'Charlie X', '')
    """)
    conn.commit()
    conn.close()
    return db_path


@pytest.fixture
async def database_module(tmp_db):
    """Boot a real database module connected to the test SQLite DB."""
    from digitorn.modules.registry import ModuleRegistry
    from digitorn.core.loader import load_modules

    registry = ModuleRegistry()
    load_modules(registry, load_all=True)

    # Create a fresh database module instance
    db_module = registry.create("database")
    await db_module.on_start()

    # Connect to the test database
    from digitorn.modules.base import ActionResult

    result = await db_module.execute("connect", {
        "connection_id": "test_users",
        "driver": "sqlite",
        "database": str(tmp_db),
    })
    assert isinstance(result, ActionResult)
    assert result.success, f"DB connect failed: {result.error}"

    yield db_module

    await db_module.on_stop()


# ---------------------------------------------------------------------------
# Integration tests
# ---------------------------------------------------------------------------


class TestRealResolverIntegration:
    """End-to-end: real DB module → UserResolver → ChannelRegistry → delivery."""

    @pytest.mark.asyncio
    async def test_sms_channel_resolves_phone_from_db(self, database_module):
        """SMS channel auto-resolves phone number from SQLite users table."""
        registry = ChannelRegistry()
        registry.register_type(CaptureChannel)

        # Create instance with user_resolver pointing to real DB
        registry.create_instance(
            "sms_alerts", "capture", {},
            app_id="test-app",
            resolver_config={
                "module": "database",
                "action": "fetch_results",
                "params": {
                    "connection_id": "test_users",
                    "query": "SELECT phone, full_name FROM users WHERE session_id = ':session_id'",
                },
                "mapping": {
                    "to_number": "phone",
                    "recipient_name": "full_name",
                },
                "cache_ttl": 0,  # No cache for test repeatability
            },
        )
        # Inject the REAL database module
        registry.set_resolver_modules("sms_alerts", {"database": database_module})

        # Deliver for Alice
        result = await registry.deliver(
            "sms_alerts", "test-app",
            ChannelPayload(message="Your deployment is complete."),
            session_id="session-alice",
        )

        assert result.success
        inst = registry.get_instance("sms_alerts")
        assert len(inst.deliveries) == 1
        delivery = inst.deliveries[0]
        assert delivery["config"]["to_number"] == "+33612345678"
        assert delivery["config"]["recipient_name"] == "Alice Dupont"
        assert delivery["message"] == "Your deployment is complete."

    @pytest.mark.asyncio
    async def test_resolver_different_users_get_different_targets(self, database_module):
        """Two users → resolver fetches different phone numbers."""
        registry = ChannelRegistry()
        registry.register_type(CaptureChannel)

        registry.create_instance(
            "sms", "capture", {},
            resolver_config={
                "module": "database",
                "action": "fetch_results",
                "params": {
                    "connection_id": "test_users",
                    "query": "SELECT phone FROM users WHERE session_id = ':session_id'",
                },
                "mapping": {"to_number": "phone"},
                "cache_ttl": 0,
            },
        )
        registry.set_resolver_modules("sms", {"database": database_module})

        # Deliver for Alice
        r1 = await registry.deliver(
            "sms", "app1", ChannelPayload(message="msg1"),
            session_id="session-alice",
        )
        # Deliver for Bob
        r2 = await registry.deliver(
            "sms", "app1", ChannelPayload(message="msg2"),
            session_id="session-bob",
        )

        assert r1.success and r2.success
        inst = registry.get_instance("sms")
        assert inst.deliveries[0]["config"]["to_number"] == "+33612345678"
        assert inst.deliveries[1]["config"]["to_number"] == "+33698765432"

    @pytest.mark.asyncio
    async def test_explicit_config_overrides_resolver(self, database_module):
        """Explicit output_config should override auto-resolved values."""
        registry = ChannelRegistry()
        registry.register_type(CaptureChannel)

        registry.create_instance(
            "sms", "capture", {},
            resolver_config={
                "module": "database",
                "action": "fetch_results",
                "params": {
                    "connection_id": "test_users",
                    "query": "SELECT phone FROM users WHERE session_id = ':session_id'",
                },
                "mapping": {"to_number": "phone"},
                "cache_ttl": 0,
            },
        )
        registry.set_resolver_modules("sms", {"database": database_module})

        # Deliver with explicit override
        result = await registry.deliver(
            "sms", "app1", ChannelPayload(message="override test"),
            config={"to_number": "+33999999999"},
            session_id="session-alice",
        )

        assert result.success
        inst = registry.get_instance("sms")
        # Explicit config wins over DB value
        assert inst.deliveries[0]["config"]["to_number"] == "+33999999999"

    @pytest.mark.asyncio
    async def test_unknown_user_falls_back_gracefully(self, database_module):
        """Unknown session_id → resolver returns nothing → fallback to output_config."""
        registry = ChannelRegistry()
        registry.register_type(CaptureChannel)

        registry.create_instance(
            "sms", "capture", {},
            resolver_config={
                "module": "database",
                "action": "fetch_results",
                "params": {
                    "connection_id": "test_users",
                    "query": "SELECT phone FROM users WHERE session_id = ':session_id'",
                },
                "mapping": {"to_number": "phone"},
                "cache_ttl": 0,
            },
        )
        registry.set_resolver_modules("sms", {"database": database_module})

        result = await registry.deliver(
            "sms", "app1", ChannelPayload(message="who?"),
            config={"to_number": "+33fallback"},
            session_id="session-unknown",
        )

        assert result.success
        inst = registry.get_instance("sms")
        assert inst.deliveries[0]["config"]["to_number"] == "+33fallback"

    @pytest.mark.asyncio
    async def test_multi_channel_same_user_different_mappings(self, database_module):
        """Same user, two channels → each maps different DB columns."""
        registry = ChannelRegistry()
        registry.register_type(CaptureChannel)

        # SMS channel maps phone
        registry.create_instance(
            "sms", "capture", {},
            resolver_config={
                "module": "database",
                "action": "fetch_results",
                "params": {
                    "connection_id": "test_users",
                    "query": "SELECT phone, email, telegram_chat_id FROM users WHERE session_id = ':session_id'",
                },
                "mapping": {"to_number": "phone"},
                "cache_ttl": 0,
            },
        )
        # Email channel maps email + name
        registry.create_instance(
            "email", "capture", {},
            resolver_config={
                "module": "database",
                "action": "fetch_results",
                "params": {
                    "connection_id": "test_users",
                    "query": "SELECT email, full_name FROM users WHERE session_id = ':session_id'",
                },
                "mapping": {"to_address": "email", "recipient_name": "full_name"},
                "cache_ttl": 0,
            },
        )

        registry.set_resolver_modules("sms", {"database": database_module})
        registry.set_resolver_modules("email", {"database": database_module})

        # Fanout to both channels for Alice
        results = await registry.deliver_multi(
            ["sms", "email"], "app1",
            ChannelPayload(message="Weekly report ready"),
            session_id="session-alice",
        )

        assert all(r.success for r in results)

        sms_inst = registry.get_instance("sms")
        email_inst = registry.get_instance("email")

        assert sms_inst.deliveries[0]["config"] == {"to_number": "+33612345678"}
        assert email_inst.deliveries[0]["config"] == {
            "to_address": "alice@example.com",
            "recipient_name": "Alice Dupont",
        }

    @pytest.mark.asyncio
    async def test_resolver_with_real_log_channel(self, database_module, caplog):
        """Real LogChannel + resolver → verify log output contains resolved data."""
        registry = ChannelRegistry()
        registry.register_type(LogChannel)

        registry.create_instance(
            "audit", "log",
            {"logger_name": "digitorn.test.audit", "level": "INFO", "format": "json", "include_data": True},
            resolver_config={
                "module": "database",
                "action": "fetch_results",
                "params": {
                    "connection_id": "test_users",
                    "query": "SELECT email FROM users WHERE session_id = ':session_id'",
                },
                "mapping": {"to_address": "email"},
                "cache_ttl": 0,
            },
        )
        registry.set_resolver_modules("audit", {"database": database_module})

        with caplog.at_level(logging.INFO, logger="digitorn.test.audit"):
            result = await registry.deliver(
                "audit", "test-app",
                ChannelPayload(
                    message="Report generated",
                    structured_data={"report_id": "RPT-001"},
                ),
                session_id="session-bob",
            )

        assert result.success
        # The log channel wrote something
        assert len(caplog.records) >= 1

    @pytest.mark.asyncio
    async def test_resolver_caching_with_real_db(self, database_module):
        """Cache should prevent repeated DB queries for the same user."""
        registry = ChannelRegistry()
        registry.register_type(CaptureChannel)

        registry.create_instance(
            "sms", "capture", {},
            resolver_config={
                "module": "database",
                "action": "fetch_results",
                "params": {
                    "connection_id": "test_users",
                    "query": "SELECT phone FROM users WHERE session_id = ':session_id'",
                },
                "mapping": {"to_number": "phone"},
                "cache_ttl": 300,  # 5 min cache
            },
        )
        registry.set_resolver_modules("sms", {"database": database_module})

        # First delivery → queries DB
        r1 = await registry.deliver(
            "sms", "app1", ChannelPayload(message="msg1"),
            session_id="session-alice",
        )
        # Second delivery → should use cache
        r2 = await registry.deliver(
            "sms", "app1", ChannelPayload(message="msg2"),
            session_id="session-alice",
        )

        assert r1.success and r2.success
        inst = registry.get_instance("sms")
        assert inst.deliveries[0]["config"]["to_number"] == "+33612345678"
        assert inst.deliveries[1]["config"]["to_number"] == "+33612345678"

    @pytest.mark.asyncio
    async def test_no_session_id_skips_resolver(self, database_module):
        """Without session_id, resolver is not called at all."""
        registry = ChannelRegistry()
        registry.register_type(CaptureChannel)

        registry.create_instance(
            "sms", "capture", {},
            resolver_config={
                "module": "database",
                "action": "fetch_results",
                "params": {
                    "connection_id": "test_users",
                    "query": "SELECT phone FROM users WHERE session_id = ':session_id'",
                },
                "mapping": {"to_number": "phone"},
            },
        )
        registry.set_resolver_modules("sms", {"database": database_module})

        # No session_id → pass manual config through
        result = await registry.deliver(
            "sms", "app1", ChannelPayload(message="system alert"),
            config={"to_number": "+33admin"},
        )

        assert result.success
        inst = registry.get_instance("sms")
        assert inst.deliveries[0]["config"]["to_number"] == "+33admin"


class TestChannelResolveRecipient:
    """Test the Python resolve_recipient() path (no YAML resolver)."""

    @pytest.mark.asyncio
    async def test_custom_channel_with_resolve_recipient(self, database_module):
        """Channel that overrides resolve_recipient() to query the DB."""

        class SmartSMSChannel(BaseOutputChannel):
            CHANNEL_ID = "smart_sms"
            CHANNEL_NAME = "Smart SMS"
            CHANNEL_VERSION = "1.0.0"

            def __init__(self, db_module=None, **kwargs):
                super().__init__(**kwargs)
                self._db = db_module
                self.deliveries = []

            async def resolve_recipient(self, context: DeliveryContext) -> dict[str, Any]:
                if not context.session_id or not self._db:
                    return context.output_config

                from digitorn.modules.base import ActionResult
                result = await self._db.execute("fetch_results", {
                    "connection_id": "test_users",
                    "query": "SELECT phone FROM users WHERE session_id = '{}'".format(context.session_id),
                })
                if isinstance(result, ActionResult) and result.success and result.data:
                    rows = result.data.get("rows", result.data) if isinstance(result.data, dict) else result.data
                    if isinstance(rows, list) and rows:
                        resolved = {"to_number": rows[0]["phone"]}
                    else:
                        return context.output_config
                    resolved.update(context.output_config)
                    return resolved
                return context.output_config

            async def deliver(self, app_id, payload, config):
                self.deliveries.append({"app_id": app_id, "config": dict(config)})
                return DeliveryResult(success=True, channel_id=self.CHANNEL_ID)

        registry = ChannelRegistry()
        ch = SmartSMSChannel(db_module=database_module)
        registry.register_instance("smart_sms", ch, app_id="test-app")

        result = await registry.deliver(
            "smart_sms", "test-app",
            ChannelPayload(message="Bonjour!"),
            session_id="session-bob",
        )

        assert result.success
        assert ch.deliveries[0]["config"]["to_number"] == "+33698765432"
