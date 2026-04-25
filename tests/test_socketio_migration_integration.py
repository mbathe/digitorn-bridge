"""Real-wire integration tests for the Socket.IO session event bus.

Unlike the unit tests which exercise the bus against a ``FakeSio`` stub,
this file boots the **real daemon** on a random port via ``uvicorn.Server``
and connects a real ``socketio.AsyncClient`` to ``/events``. Every
assertion goes over a real TCP socket, through the real Socket.IO server
handlers, through the real ``SocketIOBus``, and back.

Scope — Part 1 (bus + routing):
    1–12. Connect, join, publish, replay, approval, leave, ping.

Scope — Part 2 (advanced):
    13. Full agent turn with mock LLM provider (token→tool_call→result)
    14. Reconnection with replay catch-up
    15. Multi-user JWT auth + user isolation
"""

from __future__ import annotations

import asyncio
import socket
import sys
import threading
import time
from pathlib import Path

import pytest
import pytest_asyncio

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "packages"))


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _override_settings_for_test(port: int) -> None:
    from digitorn.core.config import get_settings, override_settings
    s = get_settings()
    override_settings(s.model_copy(update={
        "database": s.database.model_copy(update={
            "url": "sqlite+aiosqlite:///:memory:",
        }),
        "server": s.server.model_copy(update={
            "auth_enabled": False,
            "sandbox": False,
            "host": "127.0.0.1",
            "port": port,
            "cors_origins": ["*"],
        }),
    }))


class UvicornInThread:
    """Run uvicorn.Server in a background thread with its own event loop."""

    def __init__(self, asgi_app, host: str, port: int) -> None:
        import uvicorn
        config = uvicorn.Config(
            asgi_app, host=host, port=port,
            log_level="error", loop="asyncio", lifespan="on",
            access_log=False,
        )
        self.server = uvicorn.Server(config)
        self.server.config.load()
        self.server.lifespan = None  # server will re-init
        self._thread: threading.Thread | None = None
        self.host = host
        self.port = port

    def start(self) -> None:
        def _run():
            asyncio.run(self.server.serve())
        self._thread = threading.Thread(target=_run, daemon=True)
        self._thread.start()
        # Wait for readiness
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if self.server.started:
                return
            time.sleep(0.05)
        raise RuntimeError("uvicorn did not start in 30s")

    def stop(self) -> None:
        self.server.should_exit = True
        if self._thread:
            self._thread.join(timeout=10)


@pytest.fixture(scope="module")
def daemon_url():
    """Spin up the full daemon via create_app() on a random port."""
    port = _free_port()
    _override_settings_for_test(port)

    from digitorn.core.server import create_app
    asgi_app = create_app()

    server = UvicornInThread(asgi_app, "127.0.0.1", port)
    server.start()

    # Save a reference so tests can reach app.state
    fastapi_app = getattr(asgi_app, "other_asgi_app", None) or asgi_app
    url = f"http://127.0.0.1:{port}"
    try:
        yield url, fastapi_app
    finally:
        server.stop()


@pytest_asyncio.fixture
async def sio_client(daemon_url):
    """Fresh socketio.AsyncClient connected to /events namespace."""
    import socketio
    url, _ = daemon_url
    client = socketio.AsyncClient(reconnection=False, logger=False, engineio_logger=False)
    received: list[dict] = []

    @client.on("event", namespace="/events")
    async def _on_event(env):
        received.append(env)

    await client.connect(
        url, namespaces=["/events"],
        transports=["websocket"], wait_timeout=10,
    )
    # Drain the handshake event
    await asyncio.sleep(0.15)
    try:
        yield client, received
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass


async def _wait_until(pred, timeout=3.0, interval=0.05):
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        if pred():
            return True
        await asyncio.sleep(interval)
    return False


# ════════════════════════════════════════════════════════════════════════
#  1. Connect + handshake
# ════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_connect_emits_handshake_with_latest_seq(sio_client):
    client, received = sio_client
    ok = await _wait_until(lambda: any(e.get("type") == "connected" for e in received))
    assert ok, f"no 'connected' handshake received, got: {received}"
    hs = next(e for e in received if e.get("type") == "connected")
    assert hs["kind"] == "system"
    assert "latest_seq" in hs
    assert "user_id" in hs
    assert "full_events" in hs.get("capabilities", [])


# ════════════════════════════════════════════════════════════════════════
#  2. Auto-join user room: direct bus publish on user key reaches client
# ════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_user_room_receives_user_level_publish(sio_client, daemon_url):
    client, received = sio_client
    _, fastapi_app = daemon_url
    bus = fastapi_app.state.session_bus

    # Auth disabled → user_id = "local"
    await bus.publish(
        bus.user_key("local"),
        {"type": "notification", "data": {"hello": "world"}},
    )
    ok = await _wait_until(
        lambda: any(e.get("type") == "notification" and e.get("payload", {}).get("hello") == "world"
                    for e in received),
        timeout=3.0,
    )
    assert ok, f"user-room publish not received, got: {received}"


# ════════════════════════════════════════════════════════════════════════
#  3. join_session denied when session does not exist
# ════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_join_session_denied_for_unknown_session(sio_client):
    client, _ = sio_client
    ack = await client.call(
        "join_session",
        {"app_id": "digitorn-chat", "session_id": "nope-nope-nope"},
        namespace="/events", timeout=5,
    )
    assert isinstance(ack, dict)
    assert ack.get("ok") is False
    assert "not found" in ack.get("error", "").lower() or "denied" in ack.get("error", "").lower()


@pytest.mark.asyncio
async def test_join_session_requires_both_ids(sio_client):
    client, _ = sio_client
    ack = await client.call(
        "join_session", {"app_id": "digitorn-chat"},
        namespace="/events", timeout=5,
    )
    assert ack.get("ok") is False
    assert "required" in ack.get("error", "")


# ════════════════════════════════════════════════════════════════════════
#  4. Real session flow: create session via HTTP, join via Socket.IO,
#     publish runtime events, verify they land in the session room.
# ════════════════════════════════════════════════════════════════════════

async def _create_session_via_http(url: str, app_id: str) -> str:
    import httpx
    async with httpx.AsyncClient(base_url=url, timeout=10.0) as http:
        resp = await http.post(f"/api/apps/{app_id}/sessions", json={})
        resp.raise_for_status()
        data = resp.json()["data"]
        return data["session_id"]


@pytest.mark.asyncio
async def test_session_room_receives_session_events(sio_client, daemon_url):
    client, received = sio_client
    url, fastapi_app = daemon_url
    bus = fastapi_app.state.session_bus

    app_id = "digitorn-chat"
    session_id = await _create_session_via_http(url, app_id)

    ack = await client.call(
        "join_session",
        {"app_id": app_id, "session_id": session_id},
        namespace="/events", timeout=5,
    )
    assert ack.get("ok") is True, f"join_session failed: {ack}"
    assert ack["room"] == f"session:{session_id}"

    # Mark baseline and publish a handful of events.
    received.clear()
    key = bus.session_key(app_id, session_id, "local")
    await bus.publish(key, {"type": "token", "data": {"text": "Hel"}})
    await bus.publish(key, {"type": "token", "data": {"text": "lo"}})
    await bus.publish(key, {
        "type": "tool_call",
        "data": {"tool": "Read", "args": {"path": "/tmp/x"}},
    })
    await bus.publish(key, {"type": "result", "data": {"text": "Hello"}})

    ok = await _wait_until(
        lambda: sum(1 for e in received if e.get("session_id") == session_id) >= 4,
        timeout=3.0,
    )
    assert ok, f"not all session events received, got: {received}"

    types = [e["type"] for e in received if e.get("session_id") == session_id]
    assert types == ["token", "token", "tool_call", "result"]
    # seq monotonic
    seqs = [e["seq"] for e in received if e.get("session_id") == session_id]
    assert seqs == sorted(seqs)


# ════════════════════════════════════════════════════════════════════════
#  5. Replay: disconnect, publish while offline, reconnect with since=N
# ════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_replay_on_demand_filters_by_session(daemon_url):
    import socketio
    url, fastapi_app = daemon_url
    bus = fastapi_app.state.session_bus

    app_id = "digitorn-chat"
    session_id = await _create_session_via_http(url, app_id)

    # Connect client A — receives handshake + tracks latest_seq
    received_a: list[dict] = []
    client_a = socketio.AsyncClient(reconnection=False)

    @client_a.on("event", namespace="/events")
    async def _ev(env):
        received_a.append(env)

    try:
        await client_a.connect(url, namespaces=["/events"], transports=["websocket"])
        await asyncio.sleep(0.15)
        baseline_seq = bus.user_latest_seq("local")

        # Publish before joining session — these end up in buffer but not
        # emitted to a room the client is in.
        key = bus.session_key(app_id, session_id, "local")
        for i in range(3):
            await bus.publish(key, {"type": "token", "data": {"text": f"t{i}"}})

        # On-demand replay filtered by session
        ack = await client_a.call(
            "replay",
            {"since": baseline_seq, "session_id": session_id},
            namespace="/events", timeout=5,
        )
        assert ack["ok"] is True
        assert ack["replayed"] == 3
        assert ack["latest_seq"] >= baseline_seq + 3

        # Events arrived via replay pump (sent to sid directly)
        ok = await _wait_until(
            lambda: sum(
                1 for e in received_a
                if e.get("type") == "token" and e.get("session_id") == session_id
            ) >= 3,
        )
        assert ok, f"replay events not received, got: {received_a}"
    finally:
        await client_a.disconnect()


# ════════════════════════════════════════════════════════════════════════
#  6. approval_request fans out to both session and user rooms
# ════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_approval_request_fans_out_session_and_user(sio_client, daemon_url):
    client, received = sio_client
    url, fastapi_app = daemon_url
    bus = fastapi_app.state.session_bus

    app_id = "digitorn-chat"
    session_id = await _create_session_via_http(url, app_id)

    ack = await client.call(
        "join_session",
        {"app_id": app_id, "session_id": session_id},
        namespace="/events", timeout=5,
    )
    assert ack.get("ok") is True

    received.clear()
    key = bus.session_key(app_id, session_id, "local")
    await bus.publish(key, {
        "type": "approval_request",
        "data": {
            "request_id": "r-1", "tool_name": "Bash", "tool_params": {"cmd": "ls"},
            "risk_level": "medium", "description": "run ls",
            "user_id": "local", "app_id": app_id, "session_id": session_id,
        },
    })

    # Client is in BOTH user:anonymous (auto) and session:X → receives twice
    ok = await _wait_until(
        lambda: sum(1 for e in received if e.get("type") == "approval_request") >= 2,
        timeout=3.0,
    )
    assert ok, (
        f"approval_request should fan out to session AND user rooms, got: "
        f"{[(e.get('type'), e.get('seq')) for e in received]}"
    )


# ════════════════════════════════════════════════════════════════════════
#  7. join_app + events targeted at an app_id reach the room
# ════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_join_app_receives_app_level_events(sio_client, daemon_url):
    client, received = sio_client
    url, fastapi_app = daemon_url
    bus = fastapi_app.state.session_bus

    app_id = "digitorn-chat"
    ack = await client.call("join_app", {"app_id": app_id}, namespace="/events", timeout=5)
    assert ack["ok"] is True
    assert ack["room"] == f"app:{app_id}"

    received.clear()
    # Publish WITHOUT a session_id → routes to app room
    key = f"{app_id}:anonymous:"   # empty session segment
    # Actually the bus parses session segment — use an "" placeholder
    # so the bus routes by app_id only.
    key_obj = f"{app_id}:anonymous:"
    await bus.publish(key_obj, {"type": "status", "data": {"state": "ready"}})

    ok = await _wait_until(
        lambda: any(e.get("type") == "status" for e in received),
        timeout=2.0,
    )
    assert ok, f"app-room publish not received, got: {received}"


# ════════════════════════════════════════════════════════════════════════
#  8. leave_session stops delivery
# ════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_leave_session_stops_delivery(sio_client, daemon_url):
    client, received = sio_client
    url, fastapi_app = daemon_url
    bus = fastapi_app.state.session_bus

    app_id = "digitorn-chat"
    session_id = await _create_session_via_http(url, app_id)

    await client.call("join_session",
                      {"app_id": app_id, "session_id": session_id},
                      namespace="/events", timeout=5)
    await client.call("leave_session", {"session_id": session_id},
                      namespace="/events", timeout=5)

    received.clear()
    key = bus.session_key(app_id, session_id, "local")
    await bus.publish(key, {"type": "token", "data": {"text": "ghost"}})

    # Give it a moment — token should NOT arrive on session channel.
    await asyncio.sleep(0.4)
    session_tokens = [e for e in received if e.get("type") == "token"
                      and e.get("session_id") == session_id]
    assert session_tokens == [], f"leave_session didn't work, got: {session_tokens}"


@pytest.mark.asyncio
async def test_leave_app_returns_ok(sio_client):
    client, _ = sio_client
    await client.call("join_app", {"app_id": "digitorn-chat"},
                      namespace="/events", timeout=5)
    ack = await client.call("leave_app", {"app_id": "digitorn-chat"},
                            namespace="/events", timeout=5)
    assert ack["ok"] is True


# ════════════════════════════════════════════════════════════════════════
#  9. latest_seq query
# ════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_latest_seq_query(sio_client, daemon_url):
    client, _ = sio_client
    _, fastapi_app = daemon_url
    bus = fastapi_app.state.session_bus

    before = bus.user_latest_seq("local")
    await bus.publish(bus.user_key("local"),
                      {"type": "notification", "data": {"x": 1}})
    ack = await client.call("latest_seq", None, namespace="/events", timeout=5)
    assert ack["ok"] is True
    assert ack["latest_seq"] >= before + 1


# ════════════════════════════════════════════════════════════════════════
#  10. send_message handler accepts and dispatches (does not crash)
# ════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_send_message_accepted(sio_client, daemon_url):
    client, _ = sio_client
    url, _ = daemon_url

    app_id = "digitorn-chat"
    session_id = await _create_session_via_http(url, app_id)

    ack = await client.call(
        "send_message",
        {"app_id": app_id, "session_id": session_id, "message": "hello"},
        namespace="/events", timeout=10,
    )
    assert ack["ok"] is True
    assert ack["accepted"] is True


@pytest.mark.asyncio
async def test_send_message_rejects_missing_fields(sio_client):
    client, _ = sio_client
    ack = await client.call(
        "send_message", {"app_id": "digitorn-chat"},
        namespace="/events", timeout=5,
    )
    assert ack["ok"] is False
    assert "required" in ack["error"]


# ════════════════════════════════════════════════════════════════════════
#  11. Notification poller attached — in-memory handler pathway works
# ════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_in_process_handler_receives_every_publish(daemon_url):
    _, fastapi_app = daemon_url
    bus = fastapi_app.state.session_bus

    seen: list[tuple[str, dict]] = []

    async def handler(user_id, env):
        seen.append((user_id, env))

    bus.add_handler(handler)
    try:
        await bus.publish(
            bus.session_key("a", "s", "u"),
            {"type": "token", "data": {"text": "x"}},
        )
        assert any(u == "u" and e["type"] == "token" for u, e in seen)
    finally:
        bus.remove_handler(handler)


@pytest.mark.asyncio
async def test_notification_poller_task_running(daemon_url):
    _, fastapi_app = daemon_url
    app_manager = fastapi_app.state.app_manager
    task = getattr(app_manager, "_notif_poller_task", None)
    assert task is not None, "notification poller task not installed"
    assert not task.done(), f"poller task should be running, state={task!r}"


# ════════════════════════════════════════════════════════════════════════
#  12. Ping works
# ════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_ping(daemon_url):
    """Ping handler is on the default namespace (not /events)."""
    import socketio
    url, _ = daemon_url
    client = socketio.AsyncClient(reconnection=False, logger=False, engineio_logger=False)
    try:
        await client.connect(url, transports=["websocket"], wait_timeout=10)
        await asyncio.sleep(0.1)
        ack = await client.call("ping", None, timeout=5)
        assert ack.get("pong") is True
    finally:
        await client.disconnect()


# ════════════════════════════════════════════════════════════════════════
#  PART 2 — ADVANCED TESTS
# ════════════════════════════════════════════════════════════════════════

# ── Mock LLM Provider ─────────────────────────────────────────────────

from digitorn.modules.llm_provider.providers.base import (
    BaseLLMProvider, ChatMessage, ChatResponse, StreamChunk,
    TokenUsage, ProviderCapabilities, ProviderInfo,
)


class MockLLMProvider(BaseLLMProvider):
    """Deterministic mock that streams tokens then returns a final result.

    Configurable via ``scenario``:
    - ``"simple"`` → streams "Hello world!" token by token, finish_reason="stop"
    - ``"tool_call"`` → streams "Let me check" then emits a Read tool call
    """

    def __init__(self, scenario: str = "simple") -> None:
        super().__init__(provider_id="mock", model="mock-v1")
        self.scenario = scenario
        self.call_count = 0

    async def initialize(self) -> None:
        pass

    async def chat(self, messages, **kwargs) -> ChatResponse:
        self.call_count += 1
        if self.scenario == "tool_call" and self.call_count == 1:
            return ChatResponse(
                content="Let me check",
                model="mock-v1",
                finish_reason="tool_calls",
                usage=TokenUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15),
                tool_calls=[{
                    "id": "call_mock_1",
                    "type": "function",
                    "function": {
                        "name": "filesystem.read",
                        "arguments": {"file_path": "/tmp/test.txt"},
                    },
                }],
            )
        return ChatResponse(
            content="Hello world!",
            model="mock-v1",
            finish_reason="stop",
            usage=TokenUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15),
        )

    async def chat_stream(self, messages, **kwargs):
        self.call_count += 1
        if self.scenario == "tool_call" and self.call_count == 1:
            for tok in ["Let ", "me ", "check"]:
                yield StreamChunk(delta=tok, usage=TokenUsage(
                    prompt_tokens=10, completion_tokens=3, total_tokens=13,
                ))
            yield StreamChunk(
                delta="",
                finish_reason="tool_calls",
                tool_calls=[{
                    "index": 0,
                    "id": "call_mock_1",
                    "name": "filesystem.read",
                    "arguments": '{"file_path": "/tmp/test.txt"}',
                }],
                usage=TokenUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15),
            )
            return

        for tok in ["Hello", " ", "world", "!"]:
            yield StreamChunk(delta=tok, usage=TokenUsage(
                prompt_tokens=10, completion_tokens=3, total_tokens=13,
            ))
        yield StreamChunk(
            delta="",
            finish_reason="stop",
            usage=TokenUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15),
        )

    def get_info(self) -> ProviderInfo:
        return ProviderInfo(
            provider_id="mock",
            backend="mock",
            model="mock-v1",
            capabilities=ProviderCapabilities(
                streaming=True, tool_use=True, vision=False,
                json_mode=False, system_message=True,
                max_context_window=100_000, max_output_tokens=4096,
            ),
        )


async def _inject_mock_provider(fastapi_app, scenario: str = "simple") -> MockLLMProvider:
    """Swap the deployed digitorn-chat provider with a mock.

    Waits up to 30s for the background bootstrap to deploy digitorn-chat.
    """
    app_manager = fastapi_app.state.app_manager

    # Wait for bootstrap to deploy the app (runs as a background task)
    deadline = asyncio.get_event_loop().time() + 30
    deployed = None
    while asyncio.get_event_loop().time() < deadline:
        deployed = app_manager._deployed.get("system::digitorn-chat")
        if deployed is None:
            for key, dep in app_manager._deployed.items():
                if getattr(dep, "app_id", "") == "digitorn-chat":
                    deployed = dep
                    break
        if deployed is not None:
            break
        await asyncio.sleep(0.3)

    assert deployed is not None, (
        f"digitorn-chat not deployed after 30s, keys: {list(app_manager._deployed.keys())}"
    )
    mock = MockLLMProvider(scenario=scenario)
    for ctx in deployed.contexts.values():
        ctx.provider = mock
    return mock


# ════════════════════════════════════════════════════════════════════════
#  13. Full agent turn with mock LLM: send_message → tokens → result
# ════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_full_agent_turn_simple_response(daemon_url):
    """send_message with mock provider → real token/result events over Socket.IO."""
    import socketio as sio_lib
    url, fastapi_app = daemon_url

    mock = await _inject_mock_provider(fastapi_app, "simple")

    app_id = "digitorn-chat"
    session_id = await _create_session_via_http(url, app_id)

    received: list[dict] = []
    client = sio_lib.AsyncClient(reconnection=False, logger=False, engineio_logger=False)

    @client.on("event", namespace="/events")
    async def _ev(env):
        received.append(env)

    try:
        await client.connect(url, namespaces=["/events"], transports=["websocket"])
        await asyncio.sleep(0.15)

        ack = await client.call(
            "join_session",
            {"app_id": app_id, "session_id": session_id},
            namespace="/events", timeout=5,
        )
        assert ack["ok"] is True

        received.clear()
        ack = await client.call(
            "send_message",
            {"app_id": app_id, "session_id": session_id, "message": "say hello"},
            namespace="/events", timeout=10,
        )
        assert ack["ok"] is True

        # Wait for the turn to complete — look for "result" or "turn_complete"
        ok = await _wait_until(
            lambda: any(
                e.get("type") in ("result", "turn_complete")
                and e.get("session_id") == session_id
                for e in received
            ),
            timeout=15.0,
        )
        assert ok, (
            f"agent turn did not complete. Received types: "
            f"{[(e.get('type'), e.get('payload', {}).get('text', '')[:40]) for e in received if e.get('session_id') == session_id]}"
        )

        session_events = [e for e in received if e.get("session_id") == session_id]
        types = [e["type"] for e in session_events]

        # We should see token events and a result/turn_complete
        assert "token" in types, f"no token events, got: {types}"
        assert any(t in ("result", "turn_complete") for t in types), f"no result, got: {types}"

        # Token payloads should contain text fragments
        token_events = [e for e in session_events if e["type"] == "token"]
        # The payload key depends on how manager publishes tokens:
        # it may use "text", "token", "delta", or "data" depending on the bus
        token_texts = []
        for te in token_events:
            p = te.get("payload", {})
            token_texts.append(
                p.get("text", "") or p.get("token", "") or p.get("delta", "")
                or p.get("data", "")
            )
        joined = "".join(str(t) for t in token_texts)
        assert "Hello" in joined or "world" in joined, (
            f"unexpected token text: {joined!r}, raw payloads: "
            f"{[e.get('payload') for e in token_events]}"
        )

        # Verify seq monotonic
        seqs = [e["seq"] for e in session_events]
        assert seqs == sorted(seqs), f"non-monotonic seqs: {seqs}"

    finally:
        await client.disconnect()


@pytest.mark.asyncio
async def test_full_agent_turn_with_tool_call(daemon_url):
    """send_message with tool_call scenario → tool_start + tool_call events."""
    import socketio as sio_lib
    url, fastapi_app = daemon_url

    mock = await _inject_mock_provider(fastapi_app, "tool_call")

    app_id = "digitorn-chat"
    session_id = await _create_session_via_http(url, app_id)

    received: list[dict] = []
    client = sio_lib.AsyncClient(reconnection=False, logger=False, engineio_logger=False)

    @client.on("event", namespace="/events")
    async def _ev(env):
        received.append(env)

    try:
        await client.connect(url, namespaces=["/events"], transports=["websocket"])
        await asyncio.sleep(0.15)

        await client.call(
            "join_session",
            {"app_id": app_id, "session_id": session_id},
            namespace="/events", timeout=5,
        )

        received.clear()
        await client.call(
            "send_message",
            {"app_id": app_id, "session_id": session_id, "message": "read a file"},
            namespace="/events", timeout=10,
        )

        ok = await _wait_until(
            lambda: any(
                e.get("type") in ("result", "turn_complete")
                and e.get("session_id") == session_id
                for e in received
            ),
            timeout=20.0,
        )
        assert ok, (
            f"tool_call turn did not complete. Types: "
            f"{[e.get('type') for e in received if e.get('session_id') == session_id]}"
        )

        session_events = [e for e in received if e.get("session_id") == session_id]
        types = [e["type"] for e in session_events]

        # Should see tokens, tool_start or tool_call, and result
        assert "token" in types, f"no tokens, got: {types}"
        assert any(t in ("tool_start", "tool_call") for t in types), (
            f"no tool events, got: {types}"
        )

    finally:
        await client.disconnect()


# ════════════════════════════════════════════════════════════════════════
#  14. Reconnection with replay catch-up
# ════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_reconnect_replay_catches_missed_events(daemon_url):
    """Client disconnects, events published while offline, reconnects with
    since=N and catches up via join_session replay."""
    import socketio as sio_lib
    url, fastapi_app = daemon_url
    bus = fastapi_app.state.session_bus

    app_id = "digitorn-chat"
    session_id = await _create_session_via_http(url, app_id)

    # Phase 1: connect, join session, note seq
    received_1: list[dict] = []
    client_1 = sio_lib.AsyncClient(reconnection=False, logger=False, engineio_logger=False)

    @client_1.on("event", namespace="/events")
    async def _ev1(env):
        received_1.append(env)

    try:
        await client_1.connect(url, namespaces=["/events"], transports=["websocket"])
        await asyncio.sleep(0.15)

        await client_1.call(
            "join_session",
            {"app_id": app_id, "session_id": session_id},
            namespace="/events", timeout=5,
        )

        # Publish one event so we have a baseline seq
        key = bus.session_key(app_id, session_id, "local")
        await bus.publish(key, {"type": "token", "data": {"text": "baseline"}})
        await _wait_until(
            lambda: any(e.get("type") == "token" for e in received_1),
        )
        baseline_seq = bus.user_latest_seq("local")
    finally:
        await client_1.disconnect()

    # Phase 2: publish events while client is disconnected
    for i in range(5):
        await bus.publish(key, {"type": "token", "data": {"text": f"missed-{i}"}})
    await bus.publish(key, {"type": "result", "data": {"text": "done"}})

    missed_seq = bus.user_latest_seq("local")
    assert missed_seq > baseline_seq, "no events buffered"

    # Phase 3: reconnect and replay
    received_2: list[dict] = []
    client_2 = sio_lib.AsyncClient(reconnection=False, logger=False, engineio_logger=False)

    @client_2.on("event", namespace="/events")
    async def _ev2(env):
        received_2.append(env)

    try:
        await client_2.connect(url, namespaces=["/events"], transports=["websocket"])
        await asyncio.sleep(0.15)

        # Join session with since=baseline_seq → should replay the 6 missed events
        ack = await client_2.call(
            "join_session",
            {"app_id": app_id, "session_id": session_id, "since": baseline_seq},
            namespace="/events", timeout=5,
        )
        assert ack["ok"] is True
        assert ack["latest_seq"] >= missed_seq

        ok = await _wait_until(
            lambda: sum(
                1 for e in received_2
                if e.get("session_id") == session_id and e.get("type") in ("token", "result")
            ) >= 6,
            timeout=3.0,
        )
        assert ok, (
            f"expected 6 replayed events, got: "
            f"{[e.get('type') for e in received_2 if e.get('session_id') == session_id]}"
        )

        # Verify the missed events include our markers
        replayed_tokens = [
            e.get("payload", {}).get("text", "")
            for e in received_2
            if e.get("type") == "token" and e.get("session_id") == session_id
        ]
        for i in range(5):
            assert f"missed-{i}" in replayed_tokens, (
                f"missed-{i} not in replayed tokens: {replayed_tokens}"
            )

    finally:
        await client_2.disconnect()


@pytest.mark.asyncio
async def test_reconnect_replay_via_explicit_replay_command(daemon_url):
    """Use the explicit ``replay`` command instead of join_session since."""
    import socketio as sio_lib
    url, fastapi_app = daemon_url
    bus = fastapi_app.state.session_bus

    app_id = "digitorn-chat"
    session_id = await _create_session_via_http(url, app_id)

    key = bus.session_key(app_id, session_id, "local")
    before_seq = bus.user_latest_seq("local")

    # Publish events
    for i in range(3):
        await bus.publish(key, {"type": "token", "data": {"text": f"r-{i}"}})

    # Connect fresh client, call replay
    received: list[dict] = []
    client = sio_lib.AsyncClient(reconnection=False, logger=False, engineio_logger=False)

    @client.on("event", namespace="/events")
    async def _ev(env):
        received.append(env)

    try:
        await client.connect(url, namespaces=["/events"], transports=["websocket"])
        await asyncio.sleep(0.15)

        ack = await client.call(
            "replay",
            {"since": before_seq, "session_id": session_id},
            namespace="/events", timeout=5,
        )
        assert ack["ok"] is True
        assert ack["replayed"] == 3

        ok = await _wait_until(
            lambda: sum(1 for e in received if e.get("type") == "token") >= 3,
        )
        assert ok
    finally:
        await client.disconnect()


# ════════════════════════════════════════════════════════════════════════
#  15. Multi-user JWT auth + user isolation
# ════════════════════════════════════════════════════════════════════════

@pytest.fixture(scope="module")
def auth_daemon_url():
    """Daemon with auth ENABLED and a known JWT secret."""
    port = _free_port()

    from digitorn.core.config import get_settings, override_settings
    s = get_settings()
    override_settings(s.model_copy(update={
        "database": s.database.model_copy(update={
            "url": "sqlite+aiosqlite:///:memory:",
        }),
        "server": s.server.model_copy(update={
            "auth_enabled": True,
            "jwt_secret": "test_jwt_secret_at_least_32_chars_long!!",
            "sandbox": False,
            "host": "127.0.0.1",
            "port": port,
            "cors_origins": ["*"],
        }),
    }))

    from digitorn.core.server import create_app
    asgi_app = create_app()

    server = UvicornInThread(asgi_app, "127.0.0.1", port)
    server.start()

    fastapi_app = getattr(asgi_app, "other_asgi_app", None) or asgi_app
    url = f"http://127.0.0.1:{port}"
    try:
        yield url, fastapi_app
    finally:
        server.stop()


def _mint_token(user_id: str, roles: list[str] | None = None, permissions: list[str] | None = None) -> str:
    from digitorn.core.auth.jwt import JWTService
    svc = JWTService(secret_key="test_jwt_secret_at_least_32_chars_long!!")
    return svc.generate_access_token(
        user_id=user_id,
        roles=roles or ["user"],
        permissions=permissions or ["*"],
    )


@pytest.mark.asyncio
async def test_jwt_connect_with_valid_token(auth_daemon_url):
    """Client connects with a valid JWT and receives handshake with correct user_id."""
    import socketio as sio_lib
    url, _ = auth_daemon_url
    token = _mint_token("alice")

    received: list[dict] = []
    client = sio_lib.AsyncClient(reconnection=False, logger=False, engineio_logger=False)

    @client.on("event", namespace="/events")
    async def _ev(env):
        received.append(env)

    try:
        await client.connect(
            url, namespaces=["/events"], transports=["websocket"],
            auth={"token": token}, wait_timeout=10,
        )
        await asyncio.sleep(0.2)

        hs = next((e for e in received if e.get("type") == "connected"), None)
        assert hs is not None, f"no handshake, got: {received}"
        assert hs["user_id"] == "alice"
    finally:
        await client.disconnect()


@pytest.mark.asyncio
async def test_jwt_connect_rejected_without_token(auth_daemon_url):
    """Client without token is rejected."""
    import socketio as sio_lib
    url, _ = auth_daemon_url

    client = sio_lib.AsyncClient(reconnection=False, logger=False, engineio_logger=False)
    with pytest.raises(sio_lib.exceptions.ConnectionError):
        await client.connect(
            url, namespaces=["/events"], transports=["websocket"],
            wait_timeout=5,
        )


@pytest.mark.asyncio
async def test_jwt_connect_rejected_with_bad_token(auth_daemon_url):
    """Client with invalid token is rejected."""
    import socketio as sio_lib
    url, _ = auth_daemon_url

    client = sio_lib.AsyncClient(reconnection=False, logger=False, engineio_logger=False)
    with pytest.raises(sio_lib.exceptions.ConnectionError):
        await client.connect(
            url, namespaces=["/events"], transports=["websocket"],
            auth={"token": "this.is.not.valid"},
            wait_timeout=5,
        )


@pytest.mark.asyncio
async def test_jwt_user_isolation_events_only_reach_own_room(auth_daemon_url):
    """Alice's user-level events don't reach Bob, and vice versa."""
    import socketio as sio_lib
    url, fastapi_app = auth_daemon_url
    bus = fastapi_app.state.session_bus

    alice_token = _mint_token("alice")
    bob_token = _mint_token("bob")

    alice_received: list[dict] = []
    bob_received: list[dict] = []

    alice_client = sio_lib.AsyncClient(reconnection=False, logger=False, engineio_logger=False)
    bob_client = sio_lib.AsyncClient(reconnection=False, logger=False, engineio_logger=False)

    @alice_client.on("event", namespace="/events")
    async def _alice_ev(env):
        alice_received.append(env)

    @bob_client.on("event", namespace="/events")
    async def _bob_ev(env):
        bob_received.append(env)

    try:
        await alice_client.connect(
            url, namespaces=["/events"], transports=["websocket"],
            auth={"token": alice_token}, wait_timeout=10,
        )
        await bob_client.connect(
            url, namespaces=["/events"], transports=["websocket"],
            auth={"token": bob_token}, wait_timeout=10,
        )
        await asyncio.sleep(0.2)

        # Clear handshake events
        alice_received.clear()
        bob_received.clear()

        # Publish to Alice's user room
        await bus.publish(
            bus.user_key("alice"),
            {"type": "notification", "data": {"msg": "for alice only"}},
        )
        # Publish to Bob's user room
        await bus.publish(
            bus.user_key("bob"),
            {"type": "notification", "data": {"msg": "for bob only"}},
        )

        await asyncio.sleep(0.5)

        # Alice should see only her notification
        alice_notifs = [
            e for e in alice_received
            if e.get("type") == "notification"
        ]
        assert len(alice_notifs) == 1
        assert alice_notifs[0]["payload"]["msg"] == "for alice only"

        # Bob should see only his notification
        bob_notifs = [
            e for e in bob_received
            if e.get("type") == "notification"
        ]
        assert len(bob_notifs) == 1
        assert bob_notifs[0]["payload"]["msg"] == "for bob only"

    finally:
        await alice_client.disconnect()
        await bob_client.disconnect()


@pytest.mark.asyncio
async def test_jwt_session_isolation_cross_user_join_denied(auth_daemon_url):
    """Alice creates a session; Bob cannot join it."""
    import socketio as sio_lib
    import httpx
    url, _ = auth_daemon_url

    alice_token = _mint_token("alice")
    bob_token = _mint_token("bob")
    app_id = "digitorn-chat"

    # Alice creates a session via HTTP
    async with httpx.AsyncClient(base_url=url, timeout=10.0) as http:
        resp = await http.post(
            f"/api/apps/{app_id}/sessions",
            json={},
            headers={"Authorization": f"Bearer {alice_token}"},
        )
        resp.raise_for_status()
        session_id = resp.json()["data"]["session_id"]

    # Bob tries to join Alice's session via Socket.IO
    bob_client = sio_lib.AsyncClient(reconnection=False, logger=False, engineio_logger=False)
    try:
        await bob_client.connect(
            url, namespaces=["/events"], transports=["websocket"],
            auth={"token": bob_token}, wait_timeout=10,
        )
        await asyncio.sleep(0.15)

        ack = await bob_client.call(
            "join_session",
            {"app_id": app_id, "session_id": session_id},
            namespace="/events", timeout=5,
        )
        assert ack["ok"] is False, "Bob should not be able to join Alice's session"
        assert "denied" in ack.get("error", "").lower() or "not found" in ack.get("error", "").lower()
    finally:
        await bob_client.disconnect()


@pytest.mark.asyncio
async def test_jwt_session_owner_can_join(auth_daemon_url):
    """Alice creates a session and can join it."""
    import socketio as sio_lib
    import httpx
    url, _ = auth_daemon_url

    alice_token = _mint_token("alice")
    app_id = "digitorn-chat"

    async with httpx.AsyncClient(base_url=url, timeout=10.0) as http:
        resp = await http.post(
            f"/api/apps/{app_id}/sessions",
            json={},
            headers={"Authorization": f"Bearer {alice_token}"},
        )
        resp.raise_for_status()
        session_id = resp.json()["data"]["session_id"]

    alice_client = sio_lib.AsyncClient(reconnection=False, logger=False, engineio_logger=False)
    try:
        await alice_client.connect(
            url, namespaces=["/events"], transports=["websocket"],
            auth={"token": alice_token}, wait_timeout=10,
        )
        await asyncio.sleep(0.15)

        ack = await alice_client.call(
            "join_session",
            {"app_id": app_id, "session_id": session_id},
            namespace="/events", timeout=5,
        )
        assert ack["ok"] is True
        assert ack["room"] == f"session:{session_id}"
    finally:
        await alice_client.disconnect()


# ════════════════════════════════════════════════════════════════════════
#  PART 3 — ALL EVENT TYPES END-TO-END
#
#  For every event type documented in the Flutter integration spec,
#  we publish it through the real SocketIOBus and verify a real
#  Socket.IO client receives the correct envelope (type, kind,
#  payload shape, seq, session_id).
# ════════════════════════════════════════════════════════════════════════


async def _wait_for_app_deployed(fastapi_app, app_id="digitorn-chat", timeout=30):
    """Wait until bootstrap has deployed the app."""
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        mgr = fastapi_app.state.app_manager
        for key, dep in mgr._deployed.items():
            if getattr(dep, "app_id", "") == app_id:
                return dep
        await asyncio.sleep(0.3)
    return None


async def _session_with_client(url, fastapi_app):
    """Helper: create a session + connect a client + join the session room.
    Returns (client, received, bus, key, session_id, cleanup_coro).
    """
    import socketio as sio_lib
    bus = fastapi_app.state.session_bus
    app_id = "digitorn-chat"

    # Wait for bootstrap to finish deploying the app
    await _wait_for_app_deployed(fastapi_app, app_id)

    session_id = await _create_session_via_http(url, app_id)

    received: list[dict] = []
    client = sio_lib.AsyncClient(reconnection=False, logger=False, engineio_logger=False)

    @client.on("event", namespace="/events")
    async def _ev(env):
        received.append(env)

    await client.connect(url, namespaces=["/events"], transports=["websocket"], wait_timeout=10)
    await asyncio.sleep(0.15)

    ack = await client.call(
        "join_session",
        {"app_id": app_id, "session_id": session_id},
        namespace="/events", timeout=5,
    )
    assert ack["ok"] is True

    received.clear()
    key = bus.session_key(app_id, session_id, "local")
    return client, received, bus, key, session_id


# ── 16. thinking_started ──────────────────────────────────────────────

@pytest.mark.asyncio
async def test_event_thinking_started(daemon_url):
    url, fastapi_app = daemon_url
    client, received, bus, key, sid = await _session_with_client(url, fastapi_app)
    try:
        await bus.publish(key, {"type": "thinking_started", "data": {}})
        ok = await _wait_until(
            lambda: any(e.get("type") == "thinking_started" and e.get("session_id") == sid for e in received),
        )
        assert ok, f"thinking_started not received, got: {[e.get('type') for e in received]}"
        ev = next(e for e in received if e["type"] == "thinking_started")
        assert ev["kind"] == "session"
        assert ev["seq"] > 0
        assert isinstance(ev["payload"], dict)
    finally:
        await client.disconnect()


# ── 17. thinking_delta ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_event_thinking_delta(daemon_url):
    url, fastapi_app = daemon_url
    client, received, bus, key, sid = await _session_with_client(url, fastapi_app)
    try:
        await bus.publish(key, {"type": "thinking_delta", "data": {"delta": "I should check..."}})
        ok = await _wait_until(
            lambda: any(e.get("type") == "thinking_delta" and e.get("session_id") == sid for e in received),
        )
        assert ok, f"thinking_delta not received, got: {[e.get('type') for e in received]}"
        ev = next(e for e in received if e["type"] == "thinking_delta")
        assert ev["kind"] == "session"
        assert ev["payload"]["delta"] == "I should check..."
    finally:
        await client.disconnect()


# ── 18. thinking (complete block) ─────────────────────────────────────

@pytest.mark.asyncio
async def test_event_thinking_complete(daemon_url):
    url, fastapi_app = daemon_url
    client, received, bus, key, sid = await _session_with_client(url, fastapi_app)
    try:
        await bus.publish(key, {
            "type": "thinking",
            "data": {"text": "I analyzed the code and found the issue in line 42.\nThe variable is uninitialized."},
        })
        ok = await _wait_until(
            lambda: any(e.get("type") == "thinking" and e.get("session_id") == sid for e in received),
        )
        assert ok, f"thinking not received, got: {[e.get('type') for e in received]}"
        ev = next(e for e in received if e["type"] == "thinking")
        assert ev["kind"] == "session"
        assert "line 42" in ev["payload"]["text"]
    finally:
        await client.disconnect()


# ── 19. tool_start ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_event_tool_start(daemon_url):
    url, fastapi_app = daemon_url
    client, received, bus, key, sid = await _session_with_client(url, fastapi_app)
    try:
        await bus.publish(key, {
            "type": "tool_start",
            "data": {
                "id": "call_123", "name": "filesystem.read",
                "params": {"file_path": "/tmp/test.py"},
                "label": "Read", "detail": "/tmp/test.py",
                "display": {"icon": "file", "title": "Read"},
            },
        })
        ok = await _wait_until(
            lambda: any(e.get("type") == "tool_start" and e.get("session_id") == sid for e in received),
        )
        assert ok, f"tool_start not received, got: {[e.get('type') for e in received]}"
        ev = next(e for e in received if e["type"] == "tool_start")
        assert ev["kind"] == "session"
        assert ev["payload"]["name"] == "filesystem.read"
        assert ev["payload"]["label"] == "Read"
        assert ev["payload"]["id"] == "call_123"
    finally:
        await client.disconnect()


# ── 20. stream_done ──────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_event_stream_done(daemon_url):
    url, fastapi_app = daemon_url
    client, received, bus, key, sid = await _session_with_client(url, fastapi_app)
    try:
        await bus.publish(key, {"type": "stream_done", "data": {}})
        ok = await _wait_until(
            lambda: any(e.get("type") == "stream_done" and e.get("session_id") == sid for e in received),
        )
        assert ok, f"stream_done not received, got: {[e.get('type') for e in received]}"
        ev = next(e for e in received if e["type"] == "stream_done")
        assert ev["kind"] == "session"
    finally:
        await client.disconnect()


# ── 21. memory_update ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_event_memory_update(daemon_url):
    url, fastapi_app = daemon_url
    client, received, bus, key, sid = await _session_with_client(url, fastapi_app)
    try:
        await bus.publish(key, {
            "type": "memory_update",
            "data": {"action": "remember", "result": {"stored": True}, "name": "Remember"},
        })
        ok = await _wait_until(
            lambda: any(e.get("type") == "memory_update" and e.get("session_id") == sid for e in received),
        )
        assert ok, f"memory_update not received, got: {[e.get('type') for e in received]}"
        ev = next(e for e in received if e["type"] == "memory_update")
        assert ev["kind"] == "session"
        assert ev["payload"]["action"] == "remember"
        assert ev["payload"]["name"] == "Remember"
    finally:
        await client.disconnect()


# ── 22. terminal_output ──────────────────────────────────────────────

@pytest.mark.asyncio
async def test_event_terminal_output(daemon_url):
    url, fastapi_app = daemon_url
    client, received, bus, key, sid = await _session_with_client(url, fastapi_app)
    try:
        await bus.publish(key, {
            "type": "terminal_output",
            "data": {"stdout": "total 42\ndrwxr-xr-x 2 user user 4096\n", "stderr": ""},
        })
        ok = await _wait_until(
            lambda: any(e.get("type") == "terminal_output" and e.get("session_id") == sid for e in received),
        )
        assert ok, f"terminal_output not received, got: {[e.get('type') for e in received]}"
        ev = next(e for e in received if e["type"] == "terminal_output")
        assert ev["kind"] == "session"
        assert "total 42" in ev["payload"]["stdout"]
        assert ev["payload"]["stderr"] == ""
    finally:
        await client.disconnect()


# ── 23. agent_event ──────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_event_agent_event(daemon_url):
    url, fastapi_app = daemon_url
    client, received, bus, key, sid = await _session_with_client(url, fastapi_app)
    try:
        await bus.publish(key, {
            "type": "agent_event",
            "data": {"action": "spawn_agent", "result": {"agent_id": "a-1"}, "name": "Agent"},
        })
        ok = await _wait_until(
            lambda: any(e.get("type") == "agent_event" and e.get("session_id") == sid for e in received),
        )
        assert ok, f"agent_event not received, got: {[e.get('type') for e in received]}"
        ev = next(e for e in received if e["type"] == "agent_event")
        assert ev["kind"] == "session"
        assert ev["payload"]["action"] == "spawn_agent"
        assert ev["payload"]["result"]["agent_id"] == "a-1"
    finally:
        await client.disconnect()


# ── 24. out_token ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_event_out_token(daemon_url):
    url, fastapi_app = daemon_url
    client, received, bus, key, sid = await _session_with_client(url, fastapi_app)
    try:
        await bus.publish(key, {"type": "out_token", "data": {"count": 15}})
        ok = await _wait_until(
            lambda: any(e.get("type") == "out_token" and e.get("session_id") == sid for e in received),
        )
        assert ok, f"out_token not received, got: {[e.get('type') for e in received]}"
        ev = next(e for e in received if e["type"] == "out_token")
        # out_token not in _EVENT_KIND_MAP → defaults to "session"
        assert ev["kind"] == "session"
        assert ev["payload"]["count"] == 15
    finally:
        await client.disconnect()


# ── 25. in_token ─────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_event_in_token(daemon_url):
    url, fastapi_app = daemon_url
    client, received, bus, key, sid = await _session_with_client(url, fastapi_app)
    try:
        await bus.publish(key, {"type": "in_token", "data": {"count": 320}})
        ok = await _wait_until(
            lambda: any(e.get("type") == "in_token" and e.get("session_id") == sid for e in received),
        )
        assert ok, f"in_token not received, got: {[e.get('type') for e in received]}"
        ev = next(e for e in received if e["type"] == "in_token")
        assert ev["kind"] == "session"
        assert ev["payload"]["count"] == 320
    finally:
        await client.disconnect()


# ── 26. hook ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_event_hook(daemon_url):
    url, fastapi_app = daemon_url
    client, received, bus, key, sid = await _session_with_client(url, fastapi_app)
    try:
        await bus.publish(key, {
            "type": "hook",
            "data": {
                "hook_id": "auto_compact",
                "action_type": "compact_context",
                "phase": "executed",
                "details": {"reason": "context_pressure > 0.8"},
            },
        })
        ok = await _wait_until(
            lambda: any(e.get("type") == "hook" and e.get("session_id") == sid for e in received),
        )
        assert ok, f"hook not received, got: {[e.get('type') for e in received]}"
        ev = next(e for e in received if e["type"] == "hook")
        assert ev["kind"] == "session"
        assert ev["payload"]["hook_id"] == "auto_compact"
        assert ev["payload"]["action_type"] == "compact_context"
    finally:
        await client.disconnect()


# ── 27. hook_notification ────────────────────────────────────────────

@pytest.mark.asyncio
async def test_event_hook_notification(daemon_url):
    url, fastapi_app = daemon_url
    client, received, bus, key, sid = await _session_with_client(url, fastapi_app)
    try:
        await bus.publish(key, {
            "type": "hook_notification",
            "data": {"title": "Context compacted", "message": "Reduced from 85% to 40%", "level": "info"},
        })
        ok = await _wait_until(
            lambda: any(e.get("type") == "hook_notification" and e.get("session_id") == sid for e in received),
        )
        assert ok, f"hook_notification not received, got: {[e.get('type') for e in received]}"
        ev = next(e for e in received if e["type"] == "hook_notification")
        assert ev["kind"] == "session"
        assert ev["payload"]["title"] == "Context compacted"
        assert ev["payload"]["level"] == "info"
    finally:
        await client.disconnect()


# ── 28. abort ────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_event_abort(daemon_url):
    url, fastapi_app = daemon_url
    client, received, bus, key, sid = await _session_with_client(url, fastapi_app)
    try:
        await bus.publish(key, {"type": "abort", "data": {"reason": "user_requested"}})
        ok = await _wait_until(
            lambda: any(e.get("type") == "abort" and e.get("session_id") == sid for e in received),
        )
        assert ok, f"abort not received, got: {[e.get('type') for e in received]}"
        ev = next(e for e in received if e["type"] == "abort")
        # abort not in _EVENT_KIND_MAP → defaults to "session"
        assert ev["kind"] == "session"
    finally:
        await client.disconnect()


# ── 29. Full sequence: thinking → tokens → tool → result ─────────────
#    Simulates a complete agent turn's event flow via direct bus publishes.

@pytest.mark.asyncio
async def test_full_event_sequence_all_types(daemon_url):
    """Publish a realistic sequence of ALL event types as the manager would
    during a real agent turn. Verify the client receives every single one
    in the correct order with correct kinds."""
    url, fastapi_app = daemon_url
    client, received, bus, key, sid = await _session_with_client(url, fastapi_app)
    try:
        # Phase 1: Thinking
        await bus.publish(key, {"type": "thinking_started", "data": {}})
        await bus.publish(key, {"type": "thinking_delta", "data": {"delta": "Let me analyze "}})
        await bus.publish(key, {"type": "thinking_delta", "data": {"delta": "this problem..."}})
        await bus.publish(key, {
            "type": "thinking",
            "data": {"text": "Let me analyze this problem...\nI need to read the file first."},
        })

        # Phase 2: Token streaming
        await bus.publish(key, {"type": "status", "data": {"phase": "generating"}})
        for tok in ["I'll ", "read ", "the ", "file."]:
            await bus.publish(key, {"type": "token", "data": {"delta": tok}})
        await bus.publish(key, {"type": "out_token", "data": {"count": 4}})
        await bus.publish(key, {"type": "in_token", "data": {"count": 150}})
        await bus.publish(key, {"type": "stream_done", "data": {}})

        # Phase 3: Tool execution
        await bus.publish(key, {
            "type": "tool_start",
            "data": {"id": "call_seq_1", "name": "filesystem.read", "params": {"file_path": "/tmp/x.py"},
                     "label": "Read", "detail": "/tmp/x.py", "display": {}},
        })
        await bus.publish(key, {
            "type": "tool_call",
            "data": {"id": "call_seq_1", "name": "Read", "params": {"file_path": "/tmp/x.py"},
                     "result": "print('hello')", "ok": True, "error": None,
                     "label": "Read", "detail": "/tmp/x.py"},
        })
        await bus.publish(key, {
            "type": "terminal_output",
            "data": {"stdout": "", "stderr": ""},
        })
        await bus.publish(key, {
            "type": "memory_update",
            "data": {"action": "remember", "result": {"stored": True}, "name": "Remember"},
        })
        await bus.publish(key, {
            "type": "agent_event",
            "data": {"action": "spawn_agent", "result": {"agent_id": "sub-1"}, "name": "Agent"},
        })

        # Phase 4: Hook
        await bus.publish(key, {
            "type": "hook",
            "data": {"hook_id": "h1", "action_type": "log", "phase": "executed", "details": {}},
        })
        await bus.publish(key, {
            "type": "hook_notification",
            "data": {"title": "Done", "message": "All good", "level": "info"},
        })

        # Phase 5: Result
        await bus.publish(key, {
            "type": "result",
            "data": {"content": "The file contains a hello world script.", "tokens": 42},
        })

        # Wait for the final result event
        ok = await _wait_until(
            lambda: any(e.get("type") == "result" and e.get("session_id") == sid for e in received),
            timeout=5.0,
        )
        assert ok, f"result not received. Got types: {[e.get('type') for e in received if e.get('session_id') == sid]}"

        # Collect session events only
        session_events = [e for e in received if e.get("session_id") == sid]
        types = [e["type"] for e in session_events]

        # Verify ALL expected types are present
        expected_types = {
            "thinking_started", "thinking_delta", "thinking",
            "status", "token", "out_token", "in_token", "stream_done",
            "tool_start", "tool_call", "terminal_output",
            "memory_update", "agent_event",
            "hook", "hook_notification",
            "result",
        }
        missing = expected_types - set(types)
        assert not missing, f"missing event types: {missing}, got: {types}"

        # Verify seq is monotonically increasing
        seqs = [e["seq"] for e in session_events]
        assert seqs == sorted(seqs), f"non-monotonic seqs: {seqs}"
        assert len(set(seqs)) == len(seqs), f"duplicate seqs: {seqs}"

        # Verify kinds are correct per _EVENT_KIND_MAP
        for ev in session_events:
            if ev["type"] == "error":
                assert ev["kind"] == "error"
            elif ev["type"] == "approval_request":
                assert ev["kind"] == "approval"
            elif ev["type"] in ("notification", "notification_result"):
                assert ev["kind"] == "background_activation"
            elif ev["type"] == "status":
                assert ev["kind"] == "status"
            else:
                assert ev["kind"] == "session", (
                    f"type={ev['type']} has kind={ev['kind']}, expected 'session'"
                )

    finally:
        await client.disconnect()


# ── 30. Replay includes ALL event types ──────────────────────────────

@pytest.mark.asyncio
async def test_replay_includes_all_event_types(daemon_url):
    """Publish multiple event types, disconnect, reconnect with since=N,
    verify replay returns ALL types — not just token/result."""
    import socketio as sio_lib
    url, fastapi_app = daemon_url
    bus = fastapi_app.state.session_bus

    app_id = "digitorn-chat"
    session_id = await _create_session_via_http(url, app_id)
    key = bus.session_key(app_id, session_id, "local")

    baseline_seq = bus.user_latest_seq("local")

    # Publish a variety of event types while no client is listening
    event_payloads = [
        {"type": "thinking_started", "data": {}},
        {"type": "thinking_delta", "data": {"delta": "hmm"}},
        {"type": "thinking", "data": {"text": "I think therefore I am"}},
        {"type": "token", "data": {"delta": "Hello"}},
        {"type": "tool_start", "data": {"id": "c1", "name": "Bash", "params": {"cmd": "ls"}, "label": "Bash", "detail": "ls", "display": {}}},
        {"type": "tool_call", "data": {"id": "c1", "name": "Bash", "ok": True}},
        {"type": "terminal_output", "data": {"stdout": "file.txt", "stderr": ""}},
        {"type": "memory_update", "data": {"action": "set_goal", "result": {}, "name": "SetGoal"}},
        {"type": "agent_event", "data": {"action": "spawn_agent", "result": {}, "name": "Agent"}},
        {"type": "out_token", "data": {"count": 10}},
        {"type": "in_token", "data": {"count": 200}},
        {"type": "stream_done", "data": {}},
        {"type": "hook", "data": {"hook_id": "h", "action_type": "log", "phase": "done", "details": {}}},
        {"type": "hook_notification", "data": {"title": "X", "message": "Y", "level": "info"}},
        {"type": "abort", "data": {"reason": "test"}},
        {"type": "status", "data": {"phase": "idle"}},
        {"type": "error", "data": {"error": "test_error", "code": "TEST", "category": "internal"}},
        {"type": "result", "data": {"content": "done", "tokens": 5}},
    ]
    for ev in event_payloads:
        await bus.publish(key, ev)

    # Connect a new client and replay
    received: list[dict] = []
    client = sio_lib.AsyncClient(reconnection=False, logger=False, engineio_logger=False)

    @client.on("event", namespace="/events")
    async def _ev(env):
        received.append(env)

    try:
        await client.connect(url, namespaces=["/events"], transports=["websocket"], wait_timeout=10)
        await asyncio.sleep(0.15)

        ack = await client.call(
            "replay",
            {"since": baseline_seq, "session_id": session_id},
            namespace="/events", timeout=5,
        )
        assert ack["ok"] is True
        assert ack["replayed"] == len(event_payloads)

        ok = await _wait_until(
            lambda: sum(1 for e in received if e.get("session_id") == session_id) >= len(event_payloads),
            timeout=5.0,
        )
        assert ok, (
            f"expected {len(event_payloads)} replayed, got "
            f"{sum(1 for e in received if e.get('session_id') == session_id)}"
        )

        replayed_types = {e["type"] for e in received if e.get("session_id") == session_id}
        expected = {ev["type"] for ev in event_payloads}
        missing = expected - replayed_types
        assert not missing, f"replay missing types: {missing}, got: {replayed_types}"

    finally:
        await client.disconnect()


# ════════════════════════════════════════════════════════════════════════
#  PART 4 — PREVIEW / WIDGET / WORKBENCH EVENT TYPES VIA SOCKET.IO
#
#  Verifies that preview, widget, and workbench events flow through
#  the SocketIOBus to real Socket.IO clients — no SSE required.
# ════════════════════════════════════════════════════════════════════════


# ── 31. Preview events via direct bus publish ─────────────────────────

@pytest.mark.asyncio
async def test_preview_state_changed_via_bus(daemon_url):
    """preview:state_changed reaches the session room."""
    url, fastapi_app = daemon_url
    client, received, bus, key, sid = await _session_with_client(url, fastapi_app)
    try:
        await bus.publish(key, {
            "type": "preview:state_changed",
            "data": {"key": "current_step", "value": "build", "preview_seq": 1},
        })
        ok = await _wait_until(
            lambda: any(e.get("type") == "preview:state_changed" and e.get("session_id") == sid for e in received),
        )
        assert ok, f"preview:state_changed not received, got: {[e.get('type') for e in received]}"
        ev = next(e for e in received if e["type"] == "preview:state_changed")
        assert ev["kind"] == "session"
        assert ev["payload"]["key"] == "current_step"
        assert ev["payload"]["value"] == "build"
    finally:
        await client.disconnect()


@pytest.mark.asyncio
async def test_preview_resource_set_via_bus(daemon_url):
    """preview:resource_set reaches the session room with channel/id/payload."""
    url, fastapi_app = daemon_url
    client, received, bus, key, sid = await _session_with_client(url, fastapi_app)
    try:
        await bus.publish(key, {
            "type": "preview:resource_set",
            "data": {
                "channel": "nodes", "id": "node-1",
                "payload": {"label": "Start", "type": "state", "status": "idle"},
                "preview_seq": 1,
            },
        })
        ok = await _wait_until(
            lambda: any(e.get("type") == "preview:resource_set" and e.get("session_id") == sid for e in received),
        )
        assert ok, f"preview:resource_set not received"
        ev = next(e for e in received if e["type"] == "preview:resource_set")
        assert ev["payload"]["channel"] == "nodes"
        assert ev["payload"]["id"] == "node-1"
        assert ev["payload"]["payload"]["label"] == "Start"
    finally:
        await client.disconnect()


@pytest.mark.asyncio
async def test_preview_resource_patched_via_bus(daemon_url):
    url, fastapi_app = daemon_url
    client, received, bus, key, sid = await _session_with_client(url, fastapi_app)
    try:
        await bus.publish(key, {
            "type": "preview:resource_patched",
            "data": {"channel": "nodes", "id": "node-1", "patch": {"status": "running"}, "payload": {"status": "running"}, "preview_seq": 2},
        })
        ok = await _wait_until(
            lambda: any(e.get("type") == "preview:resource_patched" for e in received),
        )
        assert ok
        ev = next(e for e in received if e["type"] == "preview:resource_patched")
        assert ev["payload"]["patch"]["status"] == "running"
    finally:
        await client.disconnect()


@pytest.mark.asyncio
async def test_preview_resource_deleted_via_bus(daemon_url):
    url, fastapi_app = daemon_url
    client, received, bus, key, sid = await _session_with_client(url, fastapi_app)
    try:
        await bus.publish(key, {
            "type": "preview:resource_deleted",
            "data": {"channel": "nodes", "id": "node-1", "preview_seq": 3},
        })
        ok = await _wait_until(
            lambda: any(e.get("type") == "preview:resource_deleted" for e in received),
        )
        assert ok
        ev = next(e for e in received if e["type"] == "preview:resource_deleted")
        assert ev["payload"]["channel"] == "nodes"
        assert ev["payload"]["id"] == "node-1"
    finally:
        await client.disconnect()


@pytest.mark.asyncio
async def test_preview_resource_bulk_set_via_bus(daemon_url):
    url, fastapi_app = daemon_url
    client, received, bus, key, sid = await _session_with_client(url, fastapi_app)
    try:
        await bus.publish(key, {
            "type": "preview:resource_bulk_set",
            "data": {
                "channel": "files",
                "items": {"main.py": {"content": "print('hi')"}, "app.css": {"content": "body{}"}},
                "replace": True,
                "preview_seq": 1,
            },
        })
        ok = await _wait_until(
            lambda: any(e.get("type") == "preview:resource_bulk_set" for e in received),
        )
        assert ok
        ev = next(e for e in received if e["type"] == "preview:resource_bulk_set")
        assert ev["payload"]["channel"] == "files"
        assert "main.py" in ev["payload"]["items"]
    finally:
        await client.disconnect()


@pytest.mark.asyncio
async def test_preview_state_patched_via_bus(daemon_url):
    url, fastapi_app = daemon_url
    client, received, bus, key, sid = await _session_with_client(url, fastapi_app)
    try:
        await bus.publish(key, {
            "type": "preview:state_patched",
            "data": {"patch": {"progress": 75, "title": "Building..."}, "preview_seq": 1},
        })
        ok = await _wait_until(
            lambda: any(e.get("type") == "preview:state_patched" for e in received),
        )
        assert ok
        ev = next(e for e in received if e["type"] == "preview:state_patched")
        assert ev["payload"]["patch"]["progress"] == 75
    finally:
        await client.disconnect()


@pytest.mark.asyncio
async def test_preview_channel_cleared_via_bus(daemon_url):
    url, fastapi_app = daemon_url
    client, received, bus, key, sid = await _session_with_client(url, fastapi_app)
    try:
        await bus.publish(key, {
            "type": "preview:channel_cleared",
            "data": {"channel": "nodes", "preview_seq": 4},
        })
        ok = await _wait_until(
            lambda: any(e.get("type") == "preview:channel_cleared" for e in received),
        )
        assert ok
        ev = next(e for e in received if e["type"] == "preview:channel_cleared")
        assert ev["payload"]["channel"] == "nodes"
    finally:
        await client.disconnect()


@pytest.mark.asyncio
async def test_preview_cleared_via_bus(daemon_url):
    url, fastapi_app = daemon_url
    client, received, bus, key, sid = await _session_with_client(url, fastapi_app)
    try:
        await bus.publish(key, {"type": "preview:cleared", "data": {"preview_seq": 5}})
        ok = await _wait_until(
            lambda: any(e.get("type") == "preview:cleared" for e in received),
        )
        assert ok
    finally:
        await client.disconnect()


# ── 32. Widget events via direct bus publish ──────────────────────────

@pytest.mark.asyncio
async def test_widget_render_via_bus(daemon_url):
    url, fastapi_app = daemon_url
    client, received, bus, key, sid = await _session_with_client(url, fastapi_app)
    try:
        await bus.publish(key, {
            "type": "widget:render",
            "data": {
                "widget_id": "w-1", "zone": "workspace", "target": "main",
                "tree": {"type": "card", "props": {"title": "Status"}},
                "widget_seq": 1,
            },
        })
        ok = await _wait_until(
            lambda: any(e.get("type") == "widget:render" for e in received),
        )
        assert ok
        ev = next(e for e in received if e["type"] == "widget:render")
        assert ev["kind"] == "session"
        assert ev["payload"]["widget_id"] == "w-1"
        assert ev["payload"]["zone"] == "workspace"
    finally:
        await client.disconnect()


@pytest.mark.asyncio
async def test_widget_update_via_bus(daemon_url):
    url, fastapi_app = daemon_url
    client, received, bus, key, sid = await _session_with_client(url, fastapi_app)
    try:
        await bus.publish(key, {
            "type": "widget:update",
            "data": {"widget_id": "w-1", "patch": {"title": "Updated"}, "state": {"count": 5}, "widget_seq": 2},
        })
        ok = await _wait_until(
            lambda: any(e.get("type") == "widget:update" for e in received),
        )
        assert ok
        ev = next(e for e in received if e["type"] == "widget:update")
        assert ev["payload"]["widget_id"] == "w-1"
        assert ev["payload"]["patch"]["title"] == "Updated"
    finally:
        await client.disconnect()


@pytest.mark.asyncio
async def test_widget_close_via_bus(daemon_url):
    url, fastapi_app = daemon_url
    client, received, bus, key, sid = await _session_with_client(url, fastapi_app)
    try:
        await bus.publish(key, {"type": "widget:close", "data": {"widget_id": "w-1", "widget_seq": 3}})
        ok = await _wait_until(
            lambda: any(e.get("type") == "widget:close" for e in received),
        )
        assert ok
        ev = next(e for e in received if e["type"] == "widget:close")
        assert ev["payload"]["widget_id"] == "w-1"
    finally:
        await client.disconnect()


@pytest.mark.asyncio
async def test_widget_error_via_bus(daemon_url):
    url, fastapi_app = daemon_url
    client, received, bus, key, sid = await _session_with_client(url, fastapi_app)
    try:
        await bus.publish(key, {
            "type": "widget:error",
            "data": {"widget_id": "w-1", "binding": "chart", "message": "No data", "widget_seq": 4},
        })
        ok = await _wait_until(
            lambda: any(e.get("type") == "widget:error" for e in received),
        )
        assert ok
        ev = next(e for e in received if e["type"] == "widget:error")
        assert ev["payload"]["message"] == "No data"
    finally:
        await client.disconnect()


@pytest.mark.asyncio
async def test_widget_state_via_bus(daemon_url):
    url, fastapi_app = daemon_url
    client, received, bus, key, sid = await _session_with_client(url, fastapi_app)
    try:
        await bus.publish(key, {
            "type": "widget:state",
            "data": {"state": {"form": {"name": "test"}}, "widget_seq": 5},
        })
        ok = await _wait_until(
            lambda: any(e.get("type") == "widget:state" for e in received),
        )
        assert ok
    finally:
        await client.disconnect()


@pytest.mark.asyncio
async def test_widget_cleared_via_bus(daemon_url):
    url, fastapi_app = daemon_url
    client, received, bus, key, sid = await _session_with_client(url, fastapi_app)
    try:
        await bus.publish(key, {"type": "widget:cleared", "data": {"widget_seq": 6}})
        ok = await _wait_until(
            lambda: any(e.get("type") == "widget:cleared" for e in received),
        )
        assert ok
    finally:
        await client.disconnect()


# ── 33. Workbench events (REMOVED) ───────────────────────────────────
# Workbench was deprecated in favor of the preview module's `files`
# channel. File lifecycle now flows through `preview:resource_set`,
# `preview:resource_patched`, `preview:resource_deleted`. See
# tools/behavior_tests.py WSP01-22 for the new contract.


# ── 34. Credential events via bus ─────────────────────────────────────

@pytest.mark.asyncio
async def test_credential_required_via_bus(daemon_url):
    url, fastapi_app = daemon_url
    client, received, bus, key, sid = await _session_with_client(url, fastapi_app)
    try:
        await bus.publish(key, {
            "type": "credential_required",
            "data": {"code": "credential_required", "provider": "openai", "message": "API key needed"},
        })
        ok = await _wait_until(
            lambda: any(e.get("type") == "credential_required" for e in received),
        )
        assert ok
        ev = next(e for e in received if e["type"] == "credential_required")
        assert ev["kind"] == "session"
        assert ev["payload"]["provider"] == "openai"
    finally:
        await client.disconnect()


# ── 35. Full preview sequence: state + resources + clear ──────────────

@pytest.mark.asyncio
async def test_full_preview_sequence_all_types(daemon_url):
    """Simulate a complete preview lifecycle via bus and verify all types arrive."""
    url, fastapi_app = daemon_url
    client, received, bus, key, sid = await _session_with_client(url, fastapi_app)
    try:
        events = [
            {"type": "preview:state_changed", "data": {"key": "mode", "value": "edit", "preview_seq": 1}},
            {"type": "preview:state_patched", "data": {"patch": {"title": "App"}, "preview_seq": 2}},
            {"type": "preview:resource_set", "data": {"channel": "nodes", "id": "n1", "payload": {"label": "A"}, "preview_seq": 3}},
            {"type": "preview:resource_set", "data": {"channel": "edges", "id": "e1", "payload": {"source": "n1", "target": "n2"}, "preview_seq": 4}},
            {"type": "preview:resource_patched", "data": {"channel": "nodes", "id": "n1", "patch": {"status": "done"}, "payload": {"status": "done"}, "preview_seq": 5}},
            {"type": "preview:resource_deleted", "data": {"channel": "edges", "id": "e1", "preview_seq": 6}},
            {"type": "preview:resource_bulk_set", "data": {"channel": "files", "items": {"a.py": {"content": "x"}}, "replace": False, "preview_seq": 7}},
            {"type": "preview:channel_cleared", "data": {"channel": "files", "preview_seq": 8}},
            {"type": "preview:cleared", "data": {"preview_seq": 9}},
        ]
        for ev in events:
            await bus.publish(key, ev)

        ok = await _wait_until(
            lambda: sum(1 for e in received if e.get("type", "").startswith("preview:") and e.get("session_id") == sid) >= len(events),
            timeout=5.0,
        )
        assert ok, (
            f"expected {len(events)} preview events, got "
            f"{sum(1 for e in received if e.get('type', '').startswith('preview:') and e.get('session_id') == sid)}"
        )

        preview_types = {e["type"] for e in received if e.get("type", "").startswith("preview:") and e.get("session_id") == sid}
        expected = {ev["type"] for ev in events}
        missing = expected - preview_types
        assert not missing, f"missing preview types: {missing}"

        # All preview events should have kind=session
        for ev in received:
            if ev.get("type", "").startswith("preview:") and ev.get("session_id") == sid:
                assert ev["kind"] == "session", f"{ev['type']} has kind={ev['kind']}"
    finally:
        await client.disconnect()


# ── 36. Full widget sequence ──────────────────────────────────────────

@pytest.mark.asyncio
async def test_full_widget_sequence_all_types(daemon_url):
    url, fastapi_app = daemon_url
    client, received, bus, key, sid = await _session_with_client(url, fastapi_app)
    try:
        events = [
            {"type": "widget:render", "data": {"widget_id": "w-1", "zone": "workspace", "tree": {}, "widget_seq": 1}},
            {"type": "widget:update", "data": {"widget_id": "w-1", "patch": {"title": "X"}, "widget_seq": 2}},
            {"type": "widget:state", "data": {"state": {"a": 1}, "widget_seq": 3}},
            {"type": "widget:error", "data": {"widget_id": "w-1", "message": "fail", "widget_seq": 4}},
            {"type": "widget:close", "data": {"widget_id": "w-1", "widget_seq": 5}},
            {"type": "widget:cleared", "data": {"widget_seq": 6}},
        ]
        for ev in events:
            await bus.publish(key, ev)

        ok = await _wait_until(
            lambda: sum(1 for e in received if e.get("type", "").startswith("widget:") and e.get("session_id") == sid) >= len(events),
            timeout=5.0,
        )
        assert ok

        widget_types = {e["type"] for e in received if e.get("type", "").startswith("widget:") and e.get("session_id") == sid}
        expected = {ev["type"] for ev in events}
        missing = expected - widget_types
        assert not missing, f"missing widget types: {missing}"
    finally:
        await client.disconnect()


# ── 37. Preview module end-to-end: call actions → events on Socket.IO ─

@pytest.mark.asyncio
async def test_preview_module_actions_emit_to_socketio(daemon_url):
    """Call real PreviewModule actions and verify the Socket.IO bridge emits events."""
    url, fastapi_app = daemon_url
    bus = fastapi_app.state.session_bus

    app_id = "digitorn-chat"
    session_id = await _create_session_via_http(url, app_id)

    # Get the deployed app's preview module (if loaded)
    app_manager = fastapi_app.state.app_manager
    deployed = None
    for key, dep in app_manager._deployed.items():
        if getattr(dep, "app_id", "") == "digitorn-chat":
            deployed = dep
            break

    if deployed is None:
        pytest.skip("digitorn-chat not deployed")

    preview_mod = deployed.modules.get("preview") if hasattr(deployed, "modules") else None
    if preview_mod is None:
        # digitorn-chat may not have preview module — test via direct bus instead
        pytest.skip("preview module not loaded in digitorn-chat")

    # Wire the module for this session
    preview_mod.set_active_session(session_id, user_id="local")

    # Verify the bus is wired
    assert preview_mod._event_bus is not None, "event_bus not wired into preview module"
    assert preview_mod._bus_app_id == app_id

    # Connect client and join session
    import socketio as sio_lib
    received: list[dict] = []
    client = sio_lib.AsyncClient(reconnection=False, logger=False, engineio_logger=False)

    @client.on("event", namespace="/events")
    async def _ev(env):
        received.append(env)

    try:
        await client.connect(url, namespaces=["/events"], transports=["websocket"], wait_timeout=10)
        await asyncio.sleep(0.15)
        ack = await client.call(
            "join_session", {"app_id": app_id, "session_id": session_id},
            namespace="/events", timeout=5,
        )
        assert ack["ok"] is True
        received.clear()

        # Call real module actions
        from digitorn.modules.preview.module import SetStateParams, SetResourceParams, ClearParams
        await preview_mod.set_state(SetStateParams(key="step", value="init"))
        await preview_mod.set_resource(SetResourceParams(channel="nodes", id="n1", payload={"label": "Start"}))
        await preview_mod.clear(ClearParams())

        # Wait for events to arrive
        ok = await _wait_until(
            lambda: sum(1 for e in received if e.get("type", "").startswith("preview:") and e.get("session_id") == session_id) >= 3,
            timeout=5.0,
        )
        assert ok, (
            f"expected 3 preview events from module actions, got: "
            f"{[e.get('type') for e in received if e.get('session_id') == session_id]}"
        )

        types = [e["type"] for e in received if e.get("type", "").startswith("preview:") and e.get("session_id") == session_id]
        assert "preview:state_changed" in types, f"missing state_changed, got: {types}"
        assert "preview:resource_set" in types, f"missing resource_set, got: {types}"
        assert "preview:cleared" in types, f"missing cleared, got: {types}"

    finally:
        await client.disconnect()


# ── 38. Replay includes preview/widget events ────────────────────────

@pytest.mark.asyncio
async def test_replay_includes_preview_and_widget_events(daemon_url):
    """Publish preview + widget events offline, reconnect, replay catches all."""
    import socketio as sio_lib
    url, fastapi_app = daemon_url
    bus = fastapi_app.state.session_bus

    app_id = "digitorn-chat"
    session_id = await _create_session_via_http(url, app_id)
    key = bus.session_key(app_id, session_id, "local")

    baseline = bus.user_latest_seq("local")

    # Publish mixed events while no client is listening
    events = [
        {"type": "token", "data": {"delta": "Hi"}},
        {"type": "preview:state_changed", "data": {"key": "mode", "value": "run", "preview_seq": 1}},
        {"type": "preview:resource_set", "data": {"channel": "nodes", "id": "n1", "payload": {"label": "X"}, "preview_seq": 2}},
        {"type": "widget:render", "data": {"widget_id": "w-1", "zone": "inline", "tree": {}, "widget_seq": 1}},
        {"type": "workbench_write", "data": {"buffer": "/test.py", "type": "text", "chars": 10, "lines": 1}},
        {"type": "result", "data": {"content": "done"}},
    ]
    for ev in events:
        await bus.publish(key, ev)

    # Connect and replay
    received: list[dict] = []
    client = sio_lib.AsyncClient(reconnection=False, logger=False, engineio_logger=False)

    @client.on("event", namespace="/events")
    async def _ev(env):
        received.append(env)

    try:
        await client.connect(url, namespaces=["/events"], transports=["websocket"], wait_timeout=10)
        await asyncio.sleep(0.15)

        ack = await client.call(
            "replay", {"since": baseline, "session_id": session_id},
            namespace="/events", timeout=5,
        )
        assert ack["ok"] is True
        assert ack["replayed"] == len(events)

        ok = await _wait_until(
            lambda: sum(1 for e in received if e.get("session_id") == session_id) >= len(events),
            timeout=5.0,
        )
        assert ok

        replayed_types = {e["type"] for e in received if e.get("session_id") == session_id}
        expected = {ev["type"] for ev in events}
        missing = expected - replayed_types
        assert not missing, f"replay missing: {missing}, got: {replayed_types}"

    finally:
        await client.disconnect()


# ── 39. Full mixed sequence: agent + preview + widget + workbench ─────

@pytest.mark.asyncio
async def test_full_mixed_sequence_agent_preview_widget_workbench(daemon_url):
    """Simulate a real session with interleaved agent, preview, widget,
    and workbench events. Verify everything arrives in order."""
    url, fastapi_app = daemon_url
    client, received, bus, key, sid = await _session_with_client(url, fastapi_app)
    try:
        events = [
            # Agent thinking
            {"type": "thinking_started", "data": {}},
            {"type": "thinking", "data": {"text": "I'll build the canvas..."}},
            # Agent streams tokens
            {"type": "token", "data": {"delta": "Building "}},
            {"type": "token", "data": {"delta": "canvas..."}},
            # Preview: agent pushes nodes
            {"type": "preview:state_changed", "data": {"key": "mode", "value": "building", "preview_seq": 1}},
            {"type": "preview:resource_set", "data": {"channel": "nodes", "id": "auth", "payload": {"label": "Auth Module", "status": "running"}, "preview_seq": 2}},
            {"type": "preview:resource_set", "data": {"channel": "edges", "id": "auth->db", "payload": {"source": "auth", "target": "db"}, "preview_seq": 3}},
            # Tool call
            {"type": "tool_start", "data": {"id": "c1", "name": "filesystem.write", "params": {"file_path": "/app.py"}, "label": "Write", "detail": "/app.py", "display": {}}},
            {"type": "tool_call", "data": {"id": "c1", "name": "Write", "ok": True, "result": {}}},
            # Workbench update
            {"type": "workbench_write", "data": {"buffer": "/app.py", "type": "text", "chars": 500, "lines": 25}},
            # Widget render
            {"type": "widget:render", "data": {"widget_id": "status", "zone": "workspace", "tree": {"type": "card"}, "widget_seq": 1}},
            # Preview: mark node done
            {"type": "preview:resource_patched", "data": {"channel": "nodes", "id": "auth", "patch": {"status": "done"}, "payload": {"status": "done"}, "preview_seq": 4}},
            # Result
            {"type": "result", "data": {"content": "Canvas built!", "tokens": 100}},
        ]
        for ev in events:
            await bus.publish(key, ev)

        ok = await _wait_until(
            lambda: any(e.get("type") == "result" and e.get("session_id") == sid for e in received),
            timeout=5.0,
        )
        assert ok

        session_events = [e for e in received if e.get("session_id") == sid]
        types = [e["type"] for e in session_events]

        expected_types = {
            "thinking_started", "thinking", "token",
            "tool_start", "tool_call", "workbench_write",
            "preview:state_changed", "preview:resource_set", "preview:resource_patched",
            "widget:render", "result",
        }
        missing = expected_types - set(types)
        assert not missing, f"missing types in mixed sequence: {missing}, got: {types}"

        # Seq monotonic
        seqs = [e["seq"] for e in session_events]
        assert seqs == sorted(seqs), f"non-monotonic seqs: {seqs}"
        assert len(set(seqs)) == len(seqs), f"duplicate seqs: {seqs}"

    finally:
        await client.disconnect()
