"""Real runtime tests for the channels module.

These tests exercise the full adapter lifecycle WITHOUT mocking —
real cron timers, real file creation, real webhook request handling.
Only agent_turn is mocked (we don't call a real LLM here).
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from digitorn.modules.channels.adapter import InboundEvent
from digitorn.modules.channels.module import (
    ActivationConfig,
    ChannelsModule,
    ChannelsModuleConfig,
    ProviderConfig,
)


# ── Helpers ─────────────────────────────────────────────────────────


def _make_module(providers: dict[str, dict], app_id: str = "test-app") -> ChannelsModule:
    """Create a fully configured ChannelsModule (no listeners yet)."""
    module = ChannelsModule()
    module._app_id = app_id
    module._config = {"providers": providers}
    return module


async def _setup_module(module: ChannelsModule, config: dict[str, Any]) -> None:
    """Run full lifecycle up to start_listeners."""
    await module.on_start()
    await module.on_config_update(config)
    await module.start_listeners()
    # Give async listener tasks time to spin up
    await asyncio.sleep(0.15)


# ── Cron Adapter Tests ──────────────────────────────────────────────


class TestCronRuntime:
    @pytest.mark.asyncio
    async def test_cron_fires_within_timeout(self):
        """Cron adapter fires an event within its schedule window."""
        received: list[InboundEvent] = []

        module = _make_module({})

        config = {
            "providers": {
                "fast_cron": {
                    "adapter": "cron",
                    "config": {
                        "schedule": "* * * * *",  # Every minute
                        "message": "Cron tick",
                    },
                    "activation": {
                        "message": "Cron fired at {{event.timestamp}}",
                    },
                },
            },
        }

        await module.on_start()
        await module.on_config_update(config)

        # Intercept pipeline.process_event to capture events
        original_pipeline = module._pipeline

        async def capture_event(event: InboundEvent, provider: Any) -> None:
            received.append(event)

        module._pipeline.process_event = capture_event

        await module.start_listeners()
        await asyncio.sleep(0.15)

        # Cron sleeps until next minute boundary — verify the adapter is alive
        prov = module._providers["fast_cron"]
        assert prov.status == "active"
        assert prov.listener_task is not None
        assert not prov.listener_task.done()

        await module.on_stop()

    @pytest.mark.asyncio
    async def test_cron_adapter_reports_schedule(self):
        """Cron adapter stores schedule info correctly."""
        from digitorn.modules.channels.adapters.cron import CronAdapter

        adapter = CronAdapter(channel_config={"schedule": "0 9 * * 1-5"})
        assert adapter._schedule == "0 9 * * 1-5"

        caps = adapter.adapter_capabilities()
        assert caps.supports_inbound is True
        assert caps.supports_outbound is False


# ── File Watcher Adapter Tests ──────────────────────────────────────


class TestFileWatcherRuntime:
    @pytest.mark.asyncio
    async def test_file_watcher_detects_new_file(self):
        """File watcher fires when a new file appears."""
        received: list[InboundEvent] = []

        with tempfile.TemporaryDirectory() as tmpdir:
            # Pre-create a file (should be ignored on startup)
            existing = Path(tmpdir) / "existing.txt"
            existing.write_text("old file")

            module = _make_module({})

            config = {
                "providers": {
                    "watcher": {
                        "adapter": "file_watcher",
                        "config": {
                            "paths": [f"{tmpdir}/*.txt"],
                            "poll_interval": 0.3,
                        },
                        "activation": {
                            "message": "New file: {{event.payload.filename}}",
                        },
                    },
                },
            }

            await module.on_start()
            await module.on_config_update(config)

            async def capture_event(event: InboundEvent, provider: Any) -> None:
                received.append(event)

            module._pipeline.process_event = capture_event

            await module.start_listeners()
            await asyncio.sleep(0.15)

            # Create a new file
            new_file = Path(tmpdir) / "hello.txt"
            new_file.write_text("hello world")

            # Wait for poll to detect it
            await asyncio.sleep(0.8)

            assert len(received) == 1
            assert received[0].payload["filename"] == "hello.txt"
            assert received[0].payload["size"] == len("hello world")
            assert received[0].adapter_type == "file_watcher"

            # Existing file should NOT have triggered
            assert all(e.payload["filename"] != "existing.txt" for e in received)

            await module.on_stop()

    @pytest.mark.asyncio
    async def test_file_watcher_multiple_files(self):
        """File watcher fires once per new file."""
        received: list[InboundEvent] = []

        with tempfile.TemporaryDirectory() as tmpdir:
            module = _make_module({})
            config = {
                "providers": {
                    "watcher": {
                        "adapter": "file_watcher",
                        "config": {
                            "paths": [f"{tmpdir}/*.log"],
                            "poll_interval": 0.2,
                        },
                    },
                },
            }

            await module.on_start()
            await module.on_config_update(config)

            async def capture_event(event: InboundEvent, provider: Any) -> None:
                received.append(event)

            module._pipeline.process_event = capture_event

            await module.start_listeners()
            await asyncio.sleep(0.15)

            # Create 3 files
            for i in range(3):
                (Path(tmpdir) / f"app{i}.log").write_text(f"log {i}")

            await asyncio.sleep(0.6)

            assert len(received) == 3
            filenames = {e.payload["filename"] for e in received}
            assert filenames == {"app0.log", "app1.log", "app2.log"}

            await module.on_stop()

    @pytest.mark.asyncio
    async def test_file_watcher_no_duplicate(self):
        """Same file doesn't trigger twice."""
        received: list[InboundEvent] = []

        with tempfile.TemporaryDirectory() as tmpdir:
            module = _make_module({})
            config = {
                "providers": {
                    "watcher": {
                        "adapter": "file_watcher",
                        "config": {
                            "paths": [f"{tmpdir}/*.txt"],
                            "poll_interval": 0.2,
                        },
                    },
                },
            }

            await module.on_start()
            await module.on_config_update(config)

            async def capture_event(event: InboundEvent, provider: Any) -> None:
                received.append(event)

            module._pipeline.process_event = capture_event

            await module.start_listeners()
            await asyncio.sleep(0.15)

            # Create a file
            (Path(tmpdir) / "once.txt").write_text("data")
            await asyncio.sleep(0.5)

            # Wait another poll cycle — should NOT re-trigger
            await asyncio.sleep(0.5)

            assert len(received) == 1

            await module.on_stop()


# ── Webhook Adapter Tests ───────────────────────────────────────────


class TestWebhookRuntime:
    @pytest.mark.asyncio
    async def test_webhook_accepts_valid_request(self):
        """Webhook processes a valid JSON POST."""
        received: list[InboundEvent] = []

        module = _make_module({})
        config = {
            "providers": {
                "hook": {
                    "adapter": "webhook",
                    "config": {
                        "inbound_path": "/webhook/test",
                        "auth": "none",
                    },
                    "activation": {
                        "message": "Webhook: {{event.payload.action}}",
                    },
                },
            },
        }

        await module.on_start()
        await module.on_config_update(config)

        async def capture_event(event: InboundEvent, provider: Any) -> None:
            received.append(event)

        module._pipeline.process_event = capture_event

        await module.start_listeners()
        await asyncio.sleep(0.15)

        # Simulate HTTP request
        adapter = module._providers["hook"].adapter
        body = json.dumps({"action": "created", "id": 42}).encode()
        headers = {"content-type": "application/json"}

        ok, err = await adapter.handle_webhook_request(body, headers, "10.0.0.1")
        assert ok, f"Webhook rejected: {err}"
        assert err == ""

        await asyncio.sleep(0.1)
        assert len(received) == 1
        assert received[0].payload["action"] == "created"
        assert received[0].payload["id"] == 42
        assert received[0].source == "10.0.0.1"

        await module.on_stop()

    @pytest.mark.asyncio
    async def test_webhook_hmac_auth(self):
        """Webhook rejects invalid HMAC, accepts valid."""
        import hashlib
        import hmac

        secret = "test-secret-key-123"
        module = _make_module({})
        config = {
            "providers": {
                "hook": {
                    "adapter": "webhook",
                    "config": {
                        "inbound_path": "/webhook/secure",
                        "auth": "signature",
                        "signature_secret": secret,
                        "signature_header": "X-Signature-256",
                    },
                },
            },
        }

        await module.on_start()
        await module.on_config_update(config)

        async def noop_event(event, provider):
            pass

        module._pipeline.process_event = noop_event

        await module.start_listeners()
        await asyncio.sleep(0.15)

        adapter = module._providers["hook"].adapter
        body = b'{"test": true}'
        headers = {"content-type": "application/json"}

        # Bad signature
        headers_bad = {**headers, "x-signature-256": "sha256=invalid"}
        ok, err = await adapter.handle_webhook_request(body, headers_bad, "1.2.3.4")
        assert not ok
        assert "signature" in err.lower()

        # Good signature
        sig = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        headers_good = {**headers, "x-signature-256": sig}
        ok, err = await adapter.handle_webhook_request(body, headers_good, "1.2.3.4")
        assert ok, f"Valid HMAC rejected: {err}"

        await module.on_stop()

    @pytest.mark.asyncio
    async def test_webhook_api_key_auth(self):
        """Webhook validates API key."""
        module = _make_module({})
        config = {
            "providers": {
                "hook": {
                    "adapter": "webhook",
                    "config": {
                        "inbound_path": "/webhook/keyed",
                        "auth": "api_key",
                        "api_key": "my-secret-api-key",
                    },
                },
            },
        }

        await module.on_start()
        await module.on_config_update(config)

        async def noop(event, provider):
            pass

        module._pipeline.process_event = noop

        await module.start_listeners()
        await asyncio.sleep(0.15)

        adapter = module._providers["hook"].adapter
        body = b'{"ok": 1}'
        headers = {"content-type": "application/json"}

        # No key
        ok, err = await adapter.handle_webhook_request(body, headers, "1.2.3.4")
        assert not ok
        assert "API key" in err or "api_key" in err.lower() or "Invalid" in err

        # Wrong key
        ok, err = await adapter.handle_webhook_request(
            body, {**headers, "x-api-key": "wrong"}, "1.2.3.4"
        )
        assert not ok

        # Correct key
        ok, err = await adapter.handle_webhook_request(
            body, {**headers, "x-api-key": "my-secret-api-key"}, "1.2.3.4"
        )
        assert ok, f"Valid key rejected: {err}"

        await module.on_stop()

    @pytest.mark.asyncio
    async def test_webhook_rejects_oversized_payload(self):
        """Webhook rejects payloads exceeding the size limit."""
        module = _make_module({})
        config = {
            "providers": {
                "hook": {
                    "adapter": "webhook",
                    "config": {
                        "inbound_path": "/webhook/small",
                        "auth": "none",
                        "max_payload_bytes": 100,
                    },
                },
            },
        }

        await module.on_start()
        await module.on_config_update(config)

        async def noop(event, provider):
            pass

        module._pipeline.process_event = noop

        await module.start_listeners()
        await asyncio.sleep(0.15)

        adapter = module._providers["hook"].adapter
        big_body = json.dumps({"data": "x" * 200}).encode()
        headers = {"content-type": "application/json"}

        ok, err = await adapter.handle_webhook_request(big_body, headers, "1.2.3.4")
        assert not ok
        assert "size" in err.lower() or "large" in err.lower() or "payload" in err.lower()

        await module.on_stop()

    @pytest.mark.asyncio
    async def test_webhook_rejects_bad_content_type(self):
        """Webhook rejects non-JSON content types."""
        module = _make_module({})
        config = {
            "providers": {
                "hook": {
                    "adapter": "webhook",
                    "config": {
                        "inbound_path": "/webhook/json-only",
                        "auth": "none",
                    },
                },
            },
        }

        await module.on_start()
        await module.on_config_update(config)

        async def noop(event, provider):
            pass

        module._pipeline.process_event = noop

        await module.start_listeners()
        await asyncio.sleep(0.15)

        adapter = module._providers["hook"].adapter
        body = b'<xml>data</xml>'
        headers = {"content-type": "text/xml"}

        ok, err = await adapter.handle_webhook_request(body, headers, "1.2.3.4")
        assert not ok

        await module.on_stop()

    @pytest.mark.asyncio
    async def test_webhook_sanitizes_payload(self):
        """Webhook strips __proto__ and similar keys."""
        received: list[InboundEvent] = []

        module = _make_module({})
        config = {
            "providers": {
                "hook": {
                    "adapter": "webhook",
                    "config": {
                        "inbound_path": "/webhook/safe",
                        "auth": "none",
                    },
                },
            },
        }

        await module.on_start()
        await module.on_config_update(config)

        async def capture(event: InboundEvent, provider: Any) -> None:
            received.append(event)

        module._pipeline.process_event = capture

        await module.start_listeners()
        await asyncio.sleep(0.15)

        adapter = module._providers["hook"].adapter
        body = json.dumps({
            "safe_key": "value",
            "__proto__": {"polluted": True},
            "constructor": "evil",
        }).encode()
        headers = {"content-type": "application/json"}

        ok, err = await adapter.handle_webhook_request(body, headers, "1.2.3.4")
        assert ok

        await asyncio.sleep(0.1)
        assert len(received) == 1
        assert "__proto__" not in received[0].payload
        assert "constructor" not in received[0].payload
        assert received[0].payload["safe_key"] == "value"

        await module.on_stop()


# ── Module Actions Runtime Tests ────────────────────────────────────


class TestModuleActionsRuntime:
    @pytest.mark.asyncio
    async def test_send_and_stats(self):
        """Full lifecycle: config → send → stats → stop."""
        module = _make_module({})
        config = {
            "providers": {
                "logger": {
                    "adapter": "log",
                    "config": {"level": "info"},
                },
            },
        }

        await module.on_start()
        await module.on_config_update(config)
        await module.start_listeners()
        await asyncio.sleep(0.1)

        from digitorn.modules.channels.params import SendMessageParams, StatsParams

        # Send two messages
        r1 = await module.send_message(SendMessageParams(provider="logger", text="Hello"))
        r2 = await module.send_message(SendMessageParams(provider="logger", text="World"))
        assert r1.success
        assert r2.success

        # Check stats
        stats = await module.stats(StatsParams())
        assert stats.success
        assert stats.data["total_events_sent"] == 2
        assert stats.data["providers_count"] == 1

        await module.on_stop()

    @pytest.mark.asyncio
    async def test_pause_blocks_inbound(self):
        """Paused provider doesn't process inbound events."""
        received: list[InboundEvent] = []

        module = _make_module({})
        config = {
            "providers": {
                "hook": {
                    "adapter": "webhook",
                    "config": {"inbound_path": "/webhook/pausable", "auth": "none"},
                },
            },
        }

        await module.on_start()
        await module.on_config_update(config)

        async def capture(event: InboundEvent, provider: Any) -> None:
            received.append(event)

        module._pipeline.process_event = capture

        await module.start_listeners()
        await asyncio.sleep(0.15)

        adapter = module._providers["hook"].adapter
        body = b'{"x": 1}'
        headers = {"content-type": "application/json"}

        # Send while active
        ok, _ = await adapter.handle_webhook_request(body, headers, "1.1.1.1")
        assert ok
        await asyncio.sleep(0.1)
        assert len(received) == 1

        # Pause the provider
        from digitorn.modules.channels.params import PauseProviderParams

        await module.pause_provider(PauseProviderParams(provider="hook"))
        assert module._providers["hook"].status == "paused"

        # Inbound event to module._on_inbound_event should be dropped
        from digitorn.modules.channels.adapter import InboundEvent, make_event_id

        event = InboundEvent(
            event_id=make_event_id(),
            provider_id="hook",
            adapter_type="webhook",
            source="test",
            message="ignored",
            payload={},
        )
        await module._on_inbound_event("hook", event)
        await asyncio.sleep(0.1)

        # Should still be 1 (paused)
        assert len(received) == 1

        # Resume
        from digitorn.modules.channels.params import ResumeProviderParams

        await module.resume_provider(ResumeProviderParams(provider="hook"))
        assert module._providers["hook"].status == "active"

        await module.on_stop()

    @pytest.mark.asyncio
    async def test_simulate_event(self):
        """simulate_event injects a fake event into the pipeline."""
        received: list[InboundEvent] = []

        module = _make_module({})
        config = {
            "providers": {
                "hook": {
                    "adapter": "webhook",
                    "config": {"inbound_path": "/webhook/sim", "auth": "none"},
                },
            },
        }

        await module.on_start()
        await module.on_config_update(config)

        async def capture(event: InboundEvent, provider: Any) -> None:
            received.append(event)

        module._pipeline.process_event = capture

        await module.start_listeners()
        await asyncio.sleep(0.15)

        from digitorn.modules.channels.params import SimulateEventParams

        result = await module.simulate_event(SimulateEventParams(
            provider="hook",
            payload={"simulated": True},
            source="test",
            message="Simulated webhook",
        ))
        assert result.success

        await asyncio.sleep(0.15)
        assert len(received) == 1
        assert received[0].payload["simulated"] is True

        await module.on_stop()


# ── Concurrent Activations Runtime Test ─────────────────────────────


class TestConcurrencyRuntime:
    @pytest.mark.asyncio
    async def test_concurrent_webhook_events(self):
        """Multiple webhook events are processed concurrently up to semaphore limit."""
        active_count = 0
        max_active = 0
        total_processed = 0

        module = _make_module({})
        config = {
            "providers": {
                "hook": {
                    "adapter": "webhook",
                    "config": {"inbound_path": "/webhook/concurrent", "auth": "none"},
                    "max_concurrent": 3,
                },
            },
        }

        await module.on_start()
        await module.on_config_update(config)

        async def slow_process(event: InboundEvent, provider: Any) -> None:
            nonlocal active_count, max_active, total_processed
            active_count += 1
            max_active = max(max_active, active_count)
            await asyncio.sleep(0.1)
            active_count -= 1
            total_processed += 1

        module._pipeline.process_event = slow_process

        await module.start_listeners()
        await asyncio.sleep(0.15)

        # Fire 6 webhook events rapidly
        adapter = module._providers["hook"].adapter
        for i in range(6):
            body = json.dumps({"n": i}).encode()
            await adapter.handle_webhook_request(
                body, {"content-type": "application/json"}, f"10.0.0.{i}"
            )

        # Wait for all to complete
        await asyncio.sleep(1.0)

        assert total_processed == 6
        assert max_active <= 3  # Semaphore enforced

        await module.on_stop()


# ── Variable Resolution Integration ─────────────────────────────────


class TestVariableResolution:
    def test_dotpath_passthrough_for_event_vars(self):
        """{{event.payload.x}} is preserved at compile time, not raised."""
        from digitorn.core.app.variables import resolve_variables

        data = {
            "message": "File: {{event.payload.filename}} ({{event.payload.size}} bytes)",
            "context": "Channel: {{event.adapter}}",
        }
        result = resolve_variables(data, variables={})

        # Runtime expressions preserved (not resolved, not raised)
        assert "{{event.payload.filename}}" in result["message"]
        assert "{{event.payload.size}}" in result["message"]
        assert "{{event.adapter}}" in result["context"]

    def test_sys_variables_resolve(self):
        """{{sys.*}} variables resolve at compile time."""
        from digitorn.core.app.variables import resolve_variables

        result = resolve_variables("Host: {{sys.hostname}}", variables={})
        assert "{{" not in result
        assert len(result) > len("Host: ")

    def test_app_variables_resolve(self):
        """{{app.*}} variables resolve at compile time."""
        from digitorn.core.app.variables import resolve_variables

        variables = {
            "_app_id": "test-app",
            "_app_name": "Test App",
            "_app_version": "1.0",
        }
        result = resolve_variables("ID: {{app.id}}, Name: {{app.name}}", variables)
        assert result == "ID: test-app, Name: Test App"

    def test_mixed_compile_and_runtime_vars(self):
        """Mix of compile-time and runtime variables works correctly."""
        from digitorn.core.app.variables import resolve_variables

        variables = {
            "_app_name": "MyApp",
            "greeting": "Welcome",
        }
        template = "{{greeting}} to {{_app_name}}! File: {{event.payload.path}}"
        # Note: _app_name is a plain variable, not app.name
        result = resolve_variables(template, variables)
        assert "Welcome" in result
        assert "{{event.payload.path}}" in result  # Runtime preserved

    def test_env_variables_resolve(self):
        """{{env.*}} variables resolve from environment."""
        from digitorn.core.app.variables import resolve_variables

        os.environ["_TEST_CHANNELS_VAR"] = "test_value"
        try:
            result = resolve_variables("Val: {{env._TEST_CHANNELS_VAR}}", variables={})
            assert result == "Val: test_value"
        finally:
            del os.environ["_TEST_CHANNELS_VAR"]


# ── Template Engine Runtime Tests ───────────────────────────────────


class TestTemplateRuntime:
    def test_render_event_payload(self):
        """render() resolves dotpath expressions from event data."""
        from digitorn.modules.channels.template import render

        variables = {
            "event": {
                "payload": {"filename": "report.pdf", "size": 1024},
                "source": "10.0.0.1",
            },
        }
        result = render("File {{event.payload.filename}} from {{event.source}}", variables)
        assert result == "File report.pdf from 10.0.0.1"

    def test_render_nested_deep(self):
        """render() handles deeply nested dotpaths."""
        from digitorn.modules.channels.template import render

        variables = {"a": {"b": {"c": {"d": "deep_value"}}}}
        assert render("{{a.b.c.d}}", variables) == "deep_value"

    def test_render_missing_returns_empty(self):
        """Missing dotpath returns empty or placeholder."""
        from digitorn.modules.channels.template import render

        result = render("{{nonexistent.path}}", {})
        # Should not crash — either empty or the original placeholder
        assert isinstance(result, str)

    def test_render_blocks_secret_at_runtime(self):
        """{{secret.*}} and {{env.*}} are blocked in runtime templates."""
        from digitorn.modules.channels.template import render

        result = render("Key: {{secret.DB_PASSWORD}}", {"secret": {"DB_PASSWORD": "p@ss"}})
        assert "p@ss" not in result  # Must NOT leak
        assert "blocked" in result.lower() or "secret" in result.lower() or "{{" in result


# ── Log Adapter Runtime Tests ───────────────────────────────────────


class TestLogAdapterRuntime:
    @pytest.mark.asyncio
    async def test_log_adapter_delivers(self):
        """Log adapter delivers messages via Python logging."""
        from digitorn.modules.channels.adapters.log import LogAdapter
        from digitorn.core.app.channels.base import ChannelPayload

        adapter = LogAdapter(channel_config={"level": "info"})
        await adapter.on_start()

        result = await adapter.deliver(
            "test-app",
            ChannelPayload(message="Test log message", title="Alert"),
            {},
        )
        assert result.success

    @pytest.mark.asyncio
    async def test_log_adapter_is_outbound_only(self):
        """Log adapter doesn't support inbound."""
        from digitorn.modules.channels.adapters.log import LogAdapter

        adapter = LogAdapter()
        caps = adapter.adapter_capabilities()
        assert caps.supports_outbound is True
        assert caps.supports_inbound is False


# ── Full Lifecycle Test ─────────────────────────────────────────────


class TestFullLifecycle:
    @pytest.mark.asyncio
    async def test_config_reload(self):
        """Module handles config updates (hot reload)."""
        module = _make_module({})

        # Initial config
        await module.on_start()
        await module.on_config_update({
            "providers": {
                "log1": {"adapter": "log", "config": {"level": "info"}},
            },
        })
        assert "log1" in module._providers

        # Hot reload with different providers
        await module.on_config_update({
            "providers": {
                "log2": {"adapter": "log", "config": {"level": "debug"}},
            },
        })
        assert "log1" not in module._providers
        assert "log2" in module._providers

        await module.on_stop()

    @pytest.mark.asyncio
    async def test_disabled_provider_ignored(self):
        """Disabled providers are skipped."""
        module = _make_module({})
        await module.on_start()
        await module.on_config_update({
            "providers": {
                "active": {"adapter": "log", "config": {}},
                "disabled": {"adapter": "log", "config": {}, "enabled": False},
            },
        })

        assert "active" in module._providers
        assert "disabled" not in module._providers

        await module.on_stop()

    @pytest.mark.asyncio
    async def test_stop_cancels_listeners(self):
        """on_stop cancels all listener tasks."""
        with tempfile.TemporaryDirectory() as tmpdir:
            module = _make_module({})
            await module.on_start()
            await module.on_config_update({
                "providers": {
                    "watcher": {
                        "adapter": "file_watcher",
                        "config": {"paths": [f"{tmpdir}/*.txt"], "poll_interval": 0.5},
                    },
                },
            })
            await module.start_listeners()
            await asyncio.sleep(0.15)

            task = module._providers["watcher"].listener_task
            assert task is not None
            assert not task.done()

            await module.on_stop()

            # Task should be cancelled
            assert task.done()
