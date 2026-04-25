"""E2E tests — SessionStore: CRUD, TTL, indexing, fork, messages.

Covers:
- ConversationSession creation and message management
- SessionStore put/get/delete lifecycle
- TTL-based expiration
- Per-app listing and cleanup
- Per-user listing
- Session forking
- Message save/load persistence
- Input validation (invalid IDs)
- Secondary index maintenance
"""
from __future__ import annotations

import time

import pytest

from digitorn.core.app.sessions import ConversationSession, SessionStore


class _MemBackend:
    """Minimal in-memory KeyValueBackend for testing without disk."""
    def __init__(self):
        self._store: dict = {}
        self._expires: dict[str, float] = {}

    def get(self, key, default=None):
        if key in self._expires:
            if time.time() > self._expires[key]:
                self._store.pop(key, None)
                del self._expires[key]
                return default
        return self._store.get(key, default)

    def set(self, key, value, expire=None, **kwargs):
        self._store[key] = value
        if expire:
            self._expires[key] = time.time() + expire

    def delete(self, key):
        existed = key in self._store
        self._store.pop(key, None)
        self._expires.pop(key, None)
        return existed

    def close(self):
        pass


# ── ConversationSession ──────────────────────────────────────────────────


class TestConversationSession:
    def test_creation(self):
        s = ConversationSession(session_id="s1", app_id="app1")
        assert s.session_id == "s1"
        assert s.app_id == "app1"
        assert s.user_id == "local"
        assert s.messages == []

    def test_add_system_only_first(self):
        s = ConversationSession(session_id="s1", app_id="app1")
        s.add_system("You are helpful.")
        s.add_system("This should not be added.")
        assert len(s.messages) == 1
        assert s.messages[0]["role"] == "system"

    def test_add_user_sets_title(self):
        s = ConversationSession(session_id="s1", app_id="app1")
        s.add_user("Hello world, this is my first message")
        assert s.title == "Hello world, this is my first message"
        assert len(s.messages) == 1
        assert s.messages[0]["role"] == "user"

    def test_add_user_title_only_first(self):
        s = ConversationSession(session_id="s1", app_id="app1")
        s.add_user("First message")
        s.add_user("Second message")
        assert s.title == "First message"

    def test_add_user_updates_last_active(self):
        s = ConversationSession(session_id="s1", app_id="app1")
        before = s.last_active
        time.sleep(0.01)
        s.add_user("msg")
        assert s.last_active > before

    def test_add_assistant(self):
        s = ConversationSession(session_id="s1", app_id="app1")
        s.add_assistant("I can help!")
        assert s.messages[-1]["role"] == "assistant"

    def test_summary(self):
        s = ConversationSession(session_id="s1", app_id="app1", user_id="u1")
        s.add_user("hi")
        s.add_assistant("hello")
        summary = s.summary()
        assert summary["session_id"] == "s1"
        assert summary["app_id"] == "app1"
        assert summary["user_id"] == "u1"
        assert summary["message_count"] == 2
        assert summary["title"] == "hi"

    def test_title_truncated_at_80_chars(self):
        s = ConversationSession(session_id="s1", app_id="app1")
        long_msg = "x" * 200
        s.add_user(long_msg)
        assert len(s.title) == 80


# ── SessionStore — basic CRUD ────────────────────────────────────────────


class TestSessionStoreCRUD:
    def _store(self) -> SessionStore:
        return SessionStore(backend=_MemBackend(), ttl=3600)

    def test_put_and_get(self):
        store = self._store()
        session = ConversationSession(session_id="s1", app_id="app1")
        session.add_user("Hello")
        store.put(session)

        retrieved = store.get("app1", "s1")
        assert retrieved is not None
        assert retrieved.session_id == "s1"
        assert len(retrieved.messages) == 1

    def test_get_missing(self):
        store = self._store()
        assert store.get("app1", "nope") is None

    def test_delete(self):
        store = self._store()
        session = ConversationSession(session_id="s1", app_id="app1")
        store.put(session)
        assert store.delete("app1", "s1") is True
        assert store.get("app1", "s1") is None

    def test_delete_missing(self):
        store = self._store()
        assert store.delete("app1", "nope") is False

    def test_put_overwrites(self):
        store = self._store()
        s1 = ConversationSession(session_id="s1", app_id="app1")
        s1.add_user("v1")
        store.put(s1)

        s1_v2 = ConversationSession(session_id="s1", app_id="app1")
        s1_v2.add_user("v2")
        store.put(s1_v2)

        retrieved = store.get("app1", "s1")
        assert retrieved.messages[0]["content"] == "v2"


# ── SessionStore — per-app operations ────────────────────────────────────


class TestSessionStorePerApp:
    def _store(self) -> SessionStore:
        return SessionStore(backend=_MemBackend(), ttl=3600)

    def test_list_for_app(self):
        store = self._store()
        store.put(ConversationSession(session_id="s1", app_id="app1"))
        store.put(ConversationSession(session_id="s2", app_id="app1"))
        store.put(ConversationSession(session_id="s3", app_id="app2"))

        app1_sessions = store.list_for_app("app1")
        assert len(app1_sessions) == 2
        ids = {s["session_id"] for s in app1_sessions}
        assert ids == {"s1", "s2"}

    def test_list_for_app_empty(self):
        store = self._store()
        assert store.list_for_app("nope") == []

    def test_delete_for_app(self):
        store = self._store()
        store.put(ConversationSession(session_id="s1", app_id="app1"))
        store.put(ConversationSession(session_id="s2", app_id="app1"))
        store.put(ConversationSession(session_id="s3", app_id="app2"))

        count = store.delete_for_app("app1")
        assert count == 2
        assert store.get("app1", "s1") is None
        assert store.get("app1", "s2") is None
        assert store.get("app2", "s3") is not None


# ── SessionStore — per-user operations ───────────────────────────────────


class TestSessionStorePerUser:
    def _store(self) -> SessionStore:
        return SessionStore(backend=_MemBackend(), ttl=3600)

    def test_list_for_user(self):
        store = self._store()
        store.put(ConversationSession(session_id="s1", app_id="app1", user_id="u1"))
        store.put(ConversationSession(session_id="s2", app_id="app1", user_id="u1"))
        store.put(ConversationSession(session_id="s3", app_id="app1", user_id="u2"))

        u1 = store.list_for_user("app1", "u1")
        assert len(u1) == 2

        u2 = store.list_for_user("app1", "u2")
        assert len(u2) == 1

    def test_get_with_user_id(self):
        store = self._store()
        store.put(ConversationSession(session_id="s1", app_id="app1", user_id="u1"))
        assert store.get("app1", "s1", "u1") is not None
        assert store.get("app1", "s1", "u2") is None


# ── SessionStore — messages persistence ──────────────────────────────────


class TestSessionStoreMessages:
    def _store(self) -> SessionStore:
        return SessionStore(backend=_MemBackend(), ttl=3600)

    def test_save_and_load_messages(self):
        store = self._store()
        msgs = [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello"},
        ]
        store.save_messages("app1", "s1", msgs)
        loaded = store.load_messages("app1", "s1")
        assert loaded == msgs

    def test_load_missing_returns_none(self):
        store = self._store()
        assert store.load_messages("app1", "nope") is None


# ── SessionStore — fork ──────────────────────────────────────────────────


class TestSessionStoreFork:
    def _store(self) -> SessionStore:
        return SessionStore(backend=_MemBackend(), ttl=3600)

    def test_fork_copies_session(self):
        store = self._store()
        original = ConversationSession(session_id="s1", app_id="app1")
        original.add_user("Hello")
        original.add_assistant("Hi there")
        store.put(original)

        result = store.fork("app1", "s1", "s2")
        assert result is True

        forked = store.get("app1", "s2")
        assert forked is not None
        assert forked.session_id == "s2"
        assert len(forked.messages) == 2

    def test_fork_copies_persisted_messages(self):
        store = self._store()
        store.put(ConversationSession(session_id="s1", app_id="app1"))
        store.save_messages("app1", "s1", [{"role": "user", "content": "msg"}])

        store.fork("app1", "s1", "s2")
        loaded = store.load_messages("app1", "s2")
        assert loaded == [{"role": "user", "content": "msg"}]

    def test_fork_missing_source(self):
        store = self._store()
        assert store.fork("app1", "nope", "s2") is False


# ── SessionStore — validation ────────────────────────────────────────────


class TestSessionStoreValidation:
    def test_validate_id_empty(self):
        with pytest.raises(ValueError, match="Invalid"):
            SessionStore._validate_id("")

    def test_validate_id_too_long(self):
        with pytest.raises(ValueError, match="Invalid"):
            SessionStore._validate_id("a" * 257)

    def test_validate_id_special_chars(self):
        with pytest.raises(ValueError, match="Invalid"):
            SessionStore._validate_id("hello world!")

    def test_validate_id_valid(self):
        assert SessionStore._validate_id("my-session_v1.0") == "my-session_v1.0"


# ── SessionStore — close and stats ───────────────────────────────────────


class TestSessionStoreStats:
    def test_close(self):
        store = SessionStore(backend=_MemBackend())
        store.close()  # Should not raise

    def test_stats(self):
        store = SessionStore(backend=_MemBackend(), idle_ttl=1800, absolute_ttl=7200)
        stats = store.stats
        assert stats["idle_ttl"] == 1800
        assert stats["absolute_ttl"] == 7200
