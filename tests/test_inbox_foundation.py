"""End-to-end tests for the inbox + global user event stream.

Exercises:
1. Event bus per-user fan-out with monotonic seq + replay buffer
2. InboxStore CRUD (items, devices, prefs)
3. InboxProducer: raw event → persisted inbox row
4. Mark-read / archive / unread_count
5. Kind routing: turn_complete → SESSION_COMPLETED,
   error → SESSION_FAILED or CREDENTIAL_MISSING, approval_request
   → SESSION_AWAITING_APPROVAL, notification_result → BG
"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

logging.basicConfig(level=logging.ERROR)

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:
        pass

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "packages"))


def _h(title: str) -> None:
    print(f"\n{'=' * 60}\n  {title}\n{'=' * 60}")


def _ok(label: str) -> None:
    print(f"  OK {label}")


async def _build_store():
    """In-memory SQLite + InboxStore."""
    from digitorn.core.config import get_settings, override_settings
    from digitorn.core.database import Base, get_session_factory, init_db
    from digitorn.core.inbox import InboxStore

    settings = get_settings()
    override_settings(settings.model_copy(update={
        "database": settings.database.model_copy(update={
            "url": "sqlite+aiosqlite:///:memory:",
        }),
    }))
    engine = await init_db(get_settings())
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return InboxStore(get_session_factory())


async def test_bus_fanout_and_replay() -> None:
    _h("1. Bus per-user fanout + seq + replay")
    from digitorn.core.app.event_bus import SessionEventBus

    bus = SessionEventBus()

    # Publish 3 events for alice
    for i in range(3):
        await bus.publish(
            bus.session_key("myapp", f"sess{i}", "alice"),
            {"type": "turn_complete", "data": {"i": i}},
        )

    assert bus.user_latest_seq("alice") == 3
    _ok(f"seq after 3 publishes = {bus.user_latest_seq('alice')}")

    replay = bus.user_replay("alice", since_seq=0)
    assert len(replay) == 3
    assert [e["seq"] for e in replay] == [1, 2, 3]
    _ok("replay since=0 returns 3 envelopes with seq 1,2,3")

    mid = bus.user_replay("alice", since_seq=1)
    assert [e["seq"] for e in mid] == [2, 3]
    _ok("replay since=1 returns seq 2,3")

    # Alice and bob are isolated
    await bus.publish(
        bus.session_key("myapp", "sess0", "bob"),
        {"type": "error", "data": {"code": "x"}},
    )
    assert bus.user_latest_seq("bob") == 1
    assert bus.user_latest_seq("alice") == 3
    _ok("alice/bob seq counters are isolated")


async def test_envelope_shape() -> None:
    _h("2. Envelope shape + kind mapping")
    from digitorn.core.app.event_bus import SessionEventBus

    bus = SessionEventBus()

    cases = [
        ("turn_complete", "session"),
        ("error", "error"),
        ("approval_request", "approval"),
        ("notification_result", "background_activation"),
        ("token", "session"),
        ("unknown_new_event", "session"),
    ]
    for raw, expected_kind in cases:
        await bus.publish(
            bus.session_key("app1", "s1", "u1"),
            {"type": raw, "data": {"hello": raw}},
        )
    envs = bus.user_replay("u1", since_seq=0)
    assert len(envs) == len(cases)
    for env, (raw, expected_kind) in zip(envs, cases):
        assert env["type"] == raw
        assert env["kind"] == expected_kind
        assert env["app_id"] == "app1"
        assert env["session_id"] == "s1"
        assert env["payload"]["hello"] == raw
        assert env["ts"] is not None
    _ok(f"{len(cases)} envelope shapes validated")


async def test_inbox_store_crud() -> None:
    _h("3. InboxStore CRUD")
    store = await _build_store()

    item = await store.create_item(
        user_id="alice",
        kind="session.completed",
        title="Test",
        subtitle="A test",
        app_id="myapp",
        session_id="s1",
        metadata={"tokens": 100},
    )
    assert item["id"]
    assert item["read_at"] is None
    assert item["kind"] == "session.completed"
    _ok(f"created item {item['id'][:8]}...")

    # Unread count
    unread = await store.count_unread(user_id="alice")
    assert unread == 1
    _ok(f"unread_count = {unread}")

    # Mark read
    ok = await store.mark_read(user_id="alice", item_id=item["id"])
    assert ok
    assert await store.count_unread(user_id="alice") == 0
    _ok("mark_read works, unread_count back to 0")

    # Second item + mark_all_read
    await store.create_item(
        user_id="alice", kind="session.failed", title="Err",
    )
    await store.create_item(
        user_id="alice", kind="session.awaiting_approval", title="Approve",
    )
    assert await store.count_unread(user_id="alice") == 2
    marked = await store.mark_all_read(user_id="alice")
    assert marked == 2
    assert await store.count_unread(user_id="alice") == 0
    _ok(f"mark_all_read = {marked}")

    # List
    items = await store.list_for_user(user_id="alice", limit=10)
    assert len(items) == 3
    _ok(f"list_for_user returns {len(items)} items")

    # Archive one
    await store.archive(user_id="alice", item_id=items[0]["id"])
    visible = await store.list_for_user(user_id="alice", limit=10)
    assert len(visible) == 2
    all_including = await store.list_for_user(
        user_id="alice", limit=10, include_archived=True,
    )
    assert len(all_including) == 3
    _ok("archive hides from default list but not include_archived=True")

    # Cross-user isolation
    await store.create_item(
        user_id="bob", kind="session.completed", title="Bob item",
    )
    bob_items = await store.list_for_user(user_id="bob", limit=10)
    alice_items = await store.list_for_user(user_id="alice", limit=10)
    assert len(bob_items) == 1
    assert len(alice_items) == 2
    assert bob_items[0]["title"] == "Bob item"
    _ok("alice and bob are isolated")


async def test_producer_promotes_events() -> None:
    _h("4. InboxProducer: raw bus events → inbox rows")
    from digitorn.core.app.event_bus import SessionEventBus
    from digitorn.core.inbox import InboxProducer, InboxKind

    store = await _build_store()
    bus = SessionEventBus()
    producer = InboxProducer(store=store, event_bus=bus)
    await producer.start()

    try:
        # Bump the user into the bus registry so the producer notices
        await bus.publish(
            bus.session_key("myapp", "s1", "alice"),
            {"type": "thinking", "data": {"content": "ignored"}},
        )
        # Wait for producer to spawn the watcher
        await asyncio.sleep(6)  # 5s poll + margin

        # Publish events the producer should persist. ``result`` is
        # the canonical end-of-turn event (manager.py line ~1205).
        await bus.publish(
            bus.session_key("myapp", "s1", "alice"),
            {"type": "result", "data": {
                "content": "Response preview",
                "tokens": 42,
                "duration": 1200,
            }},
        )
        # A second ``result`` carrying an error → should NOT create a
        # session.completed row (the dedicated error event that
        # follows creates the session.failed row instead).
        await bus.publish(
            bus.session_key("myapp", "s1", "alice"),
            {"type": "result", "data": {
                "error": "API rejected",
                "content": "Partial",
            }},
        )
        await bus.publish(
            bus.session_key("myapp", "s1", "alice"),
            {"type": "error", "data": {
                "error": "API key rejected",
                "code": "auth_error",
                "category": "auth",
            }},
        )
        await bus.publish(
            bus.session_key("myapp", "s1", "alice"),
            {"type": "error", "data": {
                "code": "credential_auth_required",
                "provider": "deepseek",
                "category": "auth",
                "candidates": [],
            }},
        )
        await bus.publish(
            bus.session_key("myapp", "s1", "alice"),
            {"type": "approval_request", "data": {
                "tool": "shell.bash",
                "request_id": "req_abc",
            }},
        )
        await bus.publish(
            bus.session_key("myapp", "s1", "alice"),
            {"type": "notification_result", "data": {
                "summary": "Background job done",
                "activation_id": "act_xyz",
            }},
        )

        # Give the producer time to flush
        await asyncio.sleep(0.3)

        items = await store.list_for_user(user_id="alice", limit=20)
        kinds = {i["kind"] for i in items}
        assert InboxKind.SESSION_COMPLETED in kinds
        assert InboxKind.SESSION_FAILED in kinds
        assert InboxKind.CREDENTIAL_MISSING in kinds
        assert InboxKind.SESSION_AWAITING_APPROVAL in kinds
        assert InboxKind.BG_ACTIVATION_COMPLETED in kinds
        # Exactly one session.completed even though we published two
        # ``result`` events (the second carried an error → skipped).
        completed_rows = [i for i in items if i["kind"] == InboxKind.SESSION_COMPLETED]
        assert len(completed_rows) == 1, (
            f"expected 1 session.completed row, got {len(completed_rows)}"
        )
        _ok(f"producer created {len(items)} rows covering 5 kinds")
        _ok("error-carrying 'result' correctly skipped (no duplicate)")

        # Verify content
        completed = next(i for i in items if i["kind"] == InboxKind.SESSION_COMPLETED)
        assert "Response preview" in completed["subtitle"]
        assert completed["app_id"] == "myapp"
        assert completed["session_id"] == "s1"
        _ok("session.completed row has preview + app_id + session_id")

        missing = next(i for i in items if i["kind"] == InboxKind.CREDENTIAL_MISSING)
        assert missing["credential_provider"] == "deepseek"
        _ok("credential.missing row carries provider")

    finally:
        await producer.stop()


async def test_device_and_prefs_stubs() -> None:
    _h("5. Devices + notification prefs (stubs)")
    store = await _build_store()

    # Register device
    d1 = await store.register_device(
        user_id="alice", platform="ios",
        fcm_token="tok_abc", device_name="iPhone 15",
    )
    assert d1["id"]
    _ok(f"registered device {d1['id'][:8]}")

    # Upsert same token → same id
    d2 = await store.register_device(
        user_id="alice", platform="ios",
        fcm_token="tok_abc", device_name="iPhone 15 Pro",
    )
    assert d2["id"] == d1["id"]
    _ok("same token upserts, id stable")

    devices = await store.list_devices(user_id="alice")
    assert len(devices) == 1
    _ok("list_devices works")

    ok = await store.unregister_device(user_id="alice", device_id=d1["id"])
    assert ok
    assert len(await store.list_devices(user_id="alice")) == 0
    _ok("unregister_device")

    # Prefs
    assert await store.get_notification_prefs(user_id="alice") is None
    saved = await store.save_notification_prefs(
        user_id="alice",
        prefs={
            "events": {"session.failed": ["desktop", "push"]},
            "quiet_hours": {"start": 22, "end": 7},
            "channels": {"email": "marie@example.com"},
        },
    )
    assert saved["events"]["session.failed"] == ["desktop", "push"]
    got = await store.get_notification_prefs(user_id="alice")
    assert got == saved
    _ok("notification prefs save + get roundtrip")


async def test_notification_policy() -> None:
    _h("6. NotificationPolicy — routing, defaults, quiet hours")
    from datetime import datetime, timezone
    from digitorn.core.inbox import InboxKind, NotificationPolicy

    # Default routing when no prefs
    ch = NotificationPolicy.channels_for(
        kind=InboxKind.SESSION_COMPLETED, prefs=None,
    )
    assert ch == ["desktop"]
    _ok("default routing: session.completed → [desktop]")

    ch = NotificationPolicy.channels_for(
        kind=InboxKind.SESSION_FAILED, prefs=None,
    )
    assert "push" in ch and "desktop" in ch
    _ok("default routing: session.failed → desktop + push")

    # Custom routing
    prefs = {
        "enabled": True,
        "events": {
            InboxKind.SESSION_COMPLETED: ["push", "email"],
        },
    }
    ch = NotificationPolicy.channels_for(
        kind=InboxKind.SESSION_COMPLETED, prefs=prefs,
    )
    assert ch == ["push", "email"]
    _ok("custom routing overrides default")

    # Master switch off
    ch = NotificationPolicy.channels_for(
        kind=InboxKind.SESSION_FAILED,
        prefs={"enabled": False, "events": {}},
    )
    assert ch == []
    _ok("enabled=False → no channels")

    # Quiet hours — 22:00 → 07:00, non-critical silenced
    prefs_quiet = {
        "quiet_hours": {"start": 22, "end": 7},
    }
    midnight = datetime(2026, 4, 13, 23, 30, tzinfo=timezone.utc)
    ch = NotificationPolicy.channels_for(
        kind=InboxKind.SESSION_COMPLETED, prefs=prefs_quiet, now=midnight,
    )
    assert ch == []
    _ok("quiet hours silences session.completed at 23:30")

    # Quiet hours — critical kinds bypass
    ch = NotificationPolicy.channels_for(
        kind=InboxKind.SESSION_FAILED, prefs=prefs_quiet, now=midnight,
    )
    assert len(ch) > 0
    _ok("quiet hours does NOT silence session.failed (critical)")

    # Outside quiet hours — normal delivery
    noon = datetime(2026, 4, 13, 12, 0, tzinfo=timezone.utc)
    ch = NotificationPolicy.channels_for(
        kind=InboxKind.SESSION_COMPLETED, prefs=prefs_quiet, now=noon,
    )
    assert ch == ["desktop"]
    _ok("outside quiet hours at 12:00 → delivered")

    # Wrap-around validated: 22 → 07, current 05:30 → quiet
    early = datetime(2026, 4, 13, 5, 30, tzinfo=timezone.utc)
    ch = NotificationPolicy.channels_for(
        kind=InboxKind.SESSION_COMPLETED, prefs=prefs_quiet, now=early,
    )
    assert ch == []
    _ok("wrap-around midnight: 05:30 in quiet window")


async def test_dispatcher_with_stub_backend() -> None:
    _h("7. Dispatcher — end-to-end with a stub push backend")
    from digitorn.core.inbox import (
        InboxKind,
        NotificationBackend,
        NotificationDispatcher,
    )

    store = await _build_store()

    class _StubBackend(NotificationBackend):
        name = "stub"

        def __init__(self) -> None:
            self.calls: list[dict] = []

        def is_configured(self) -> bool:
            return True

        async def send(self, *, user_id, item, context):
            self.calls.append({
                "user_id": user_id,
                "item_id": item["id"],
                "kind": item["kind"],
                "devices": len(context.get("devices") or []),
            })
            return True

    stub_fcm = _StubBackend()
    stub_smtp = _StubBackend()
    dispatcher = NotificationDispatcher(
        store=store, fcm=stub_fcm, smtp=stub_smtp,
    )

    # Register a device and set prefs that route session.failed
    # through push + email
    await store.register_device(
        user_id="alice", platform="ios",
        fcm_token="tok_test", device_name="iPhone",
    )
    await store.save_notification_prefs(
        user_id="alice",
        prefs={
            "enabled": True,
            "events": {
                InboxKind.SESSION_FAILED: ["push", "email"],
                InboxKind.SESSION_COMPLETED: ["desktop"],
            },
            "channels": {"email": "alice@example.com"},
        },
    )

    # Create an item and dispatch
    item_failed = await store.create_item(
        user_id="alice",
        kind=InboxKind.SESSION_FAILED,
        title="Failure",
        subtitle="Something broke",
    )
    result = await dispatcher.dispatch("alice", item_failed)
    assert set(result["channels"]) == {"push", "email"}
    assert result["delivered"]["push"] is True
    assert result["delivered"]["email"] is True
    assert len(stub_fcm.calls) == 1
    assert stub_fcm.calls[0]["devices"] == 1
    assert len(stub_smtp.calls) == 1
    _ok("dispatcher fired push + email for session.failed")

    # session.completed → desktop only → neither backend should fire
    item_done = await store.create_item(
        user_id="alice",
        kind=InboxKind.SESSION_COMPLETED,
        title="Done",
    )
    stub_fcm.calls.clear()
    stub_smtp.calls.clear()
    result = await dispatcher.dispatch("alice", item_done)
    assert result["channels"] == ["desktop"]
    assert result["delivered"]["desktop"] is True
    assert len(stub_fcm.calls) == 0
    assert len(stub_smtp.calls) == 0
    _ok("desktop-only routing → no backend fired")


async def test_dispatcher_graceful_degrade() -> None:
    _h("8. Dispatcher — graceful degrade when backends unconfigured")
    from digitorn.core.inbox import (
        FCMBackend, InboxKind, NotificationDispatcher, SmtpBackend,
    )

    store = await _build_store()

    # Use the real backends WITHOUT env vars set → they must
    # report is_configured() = False and not raise.
    for var in (
        "DIGITORN_FCM_CREDENTIALS_PATH",
        "DIGITORN_SMTP_HOST",
        "DIGITORN_SMTP_FROM",
    ):
        import os
        os.environ.pop(var, None)

    fcm = FCMBackend()
    smtp = SmtpBackend()
    assert fcm.is_configured() is False
    assert smtp.is_configured() is False
    _ok("FCM + SMTP report unconfigured when env vars absent")

    dispatcher = NotificationDispatcher(store=store, fcm=fcm, smtp=smtp)
    # Explicitly opt into email so both push and smtp backends
    # get exercised.
    await store.save_notification_prefs(
        user_id="alice",
        prefs={
            "enabled": True,
            "events": {
                InboxKind.SESSION_FAILED: ["desktop", "push", "email"],
            },
            "channels": {"email": "alice@example.com"},
        },
    )
    item = await store.create_item(
        user_id="alice", kind=InboxKind.SESSION_FAILED, title="Oops",
    )
    # This would try to dispatch push + email but both backends
    # are no-ops. Must NOT raise.
    result = await dispatcher.dispatch("alice", item)
    assert "push" in result["channels"]
    assert "email" in result["channels"]
    # Both backends return False silently (unconfigured)
    assert result["delivered"].get("push") is False
    assert result["delivered"].get("email") is False
    _ok("dispatch with unconfigured backends → no crash, returns False")


async def test_producer_calls_dispatcher() -> None:
    _h("9. Producer wires dispatcher through _persist")
    from digitorn.core.app.event_bus import SessionEventBus
    from digitorn.core.inbox import (
        InboxKind, InboxProducer, NotificationBackend, NotificationDispatcher,
    )

    store = await _build_store()
    bus = SessionEventBus()

    class _SpyBackend(NotificationBackend):
        name = "spy"
        def __init__(self): self.hit = 0
        def is_configured(self): return True
        async def send(self, **kwargs): self.hit += 1; return True

    spy = _SpyBackend()
    dispatcher = NotificationDispatcher(store=store, fcm=spy, smtp=spy)

    producer = InboxProducer(
        store=store, event_bus=bus, dispatcher=dispatcher,
    )
    await producer.start()
    try:
        # Seed a user
        await bus.publish(
            bus.session_key("myapp", "s1", "alice"),
            {"type": "status", "data": {}},
        )
        await asyncio.sleep(6)

        # Publish an error that triggers push (default routing)
        await bus.publish(
            bus.session_key("myapp", "s1", "alice"),
            {"type": "error", "data": {"error": "boom", "code": "internal"}},
        )
        await asyncio.sleep(0.3)

        items = await store.list_for_user(user_id="alice", limit=10)
        failed = [i for i in items if i["kind"] == InboxKind.SESSION_FAILED]
        assert len(failed) == 1
        # Default routing for session.failed = desktop + push →
        # the spy FCM backend should have been called once.
        assert spy.hit >= 1
        _ok(f"producer → dispatcher → backend: hit={spy.hit}")
    finally:
        await producer.stop()


async def main() -> None:
    tests = [
        test_bus_fanout_and_replay,
        test_envelope_shape,
        test_inbox_store_crud,
        test_producer_promotes_events,
        test_device_and_prefs_stubs,
        test_notification_policy,
        test_dispatcher_with_stub_backend,
        test_dispatcher_graceful_degrade,
        test_producer_calls_dispatcher,
    ]
    for t in tests:
        await t()
    print(f"\n{'=' * 60}\n  ALL {len(tests)} TESTS PASSED\n{'=' * 60}\n")


if __name__ == "__main__":
    asyncio.run(main())
