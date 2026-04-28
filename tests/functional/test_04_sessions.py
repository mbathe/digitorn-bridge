"""04 - Session CRUD: list, get, delete, fork, history, compact, abort."""

import uuid

import pytest

from .conftest import deploy_app, undeploy_app, send_and_wait

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def session_app(client, headers):
    await deploy_app(client, "minimal.yaml", headers)
    yield "test-minimal"
    await undeploy_app(client, "test-minimal", headers)


async def _create_session(client, headers, app_id, message="hello"):
    """Helper: send a chat message to create a session, return session_id."""
    sid = f"test-{uuid.uuid4().hex[:8]}"
    await send_and_wait(client, app_id, sid, message, headers)
    return sid


class TestSessionList:
    """GET /api/apps/{app_id}/sessions"""

    async def test_list_empty(self, client, headers, session_app):
        r = await client.get(f"/api/apps/{session_app}/sessions", headers=headers)
        d = r.json()
        assert d["success"] is True
        # data is {"sessions": [...], "total": N, "limit": 50, "offset": 0}
        assert "sessions" in d["data"]
        assert isinstance(d["data"]["sessions"], list)

    async def test_list_after_chat(self, client, headers, session_app):
        sid = await _create_session(client, headers, session_app)
        r = await client.get(f"/api/apps/{session_app}/sessions", headers=headers)
        d = r.json()
        assert d["success"] is True
        session_ids = [s.get("session_id", "") for s in d["data"]["sessions"]]
        assert sid in session_ids

    async def test_list_pagination(self, client, headers, session_app):
        r = await client.get(
            f"/api/apps/{session_app}/sessions",
            params={"limit": 2, "offset": 0},
            headers=headers,
        )
        d = r.json()
        assert d["success"] is True


class TestSessionGet:
    """GET /api/apps/{app_id}/sessions/{session_id}"""

    async def test_get_session(self, client, headers, session_app):
        sid = await _create_session(client, headers, session_app)
        r = await client.get(
            f"/api/apps/{session_app}/sessions/{sid}",
            headers=headers,
        )
        d = r.json()
        assert d["success"] is True

    async def test_get_nonexistent(self, client, headers, session_app):
        r = await client.get(
            f"/api/apps/{session_app}/sessions/nonexistent-xyz",
            headers=headers,
        )
        assert r.status_code == 404 or r.json().get("success") is False


class TestSessionHistory:
    """GET /api/apps/{app_id}/sessions/{session_id}/history"""

    async def test_history(self, client, headers, session_app):
        sid = await _create_session(client, headers, session_app)
        r = await client.get(
            f"/api/apps/{session_app}/sessions/{sid}/history",
            headers=headers,
        )
        d = r.json()
        assert d["success"] is True
        assert "messages" in d["data"] or isinstance(d["data"], list)


class TestSessionDelete:
    """DELETE /api/apps/{app_id}/sessions/{session_id}"""

    async def test_delete(self, client, headers, session_app):
        sid = await _create_session(client, headers, session_app)
        r = await client.delete(
            f"/api/apps/{session_app}/sessions/{sid}",
            headers=headers,
        )
        d = r.json()
        assert d["success"] is True

        # Verify deleted
        r2 = await client.get(
            f"/api/apps/{session_app}/sessions/{sid}",
            headers=headers,
        )
        assert r2.status_code == 404 or r2.json().get("success") is False

    async def test_delete_nonexistent(self, client, headers, session_app):
        r = await client.delete(
            f"/api/apps/{session_app}/sessions/nonexistent",
            headers=headers,
        )
        assert r.status_code == 404 or r.json().get("success") is False


class TestSessionActions:
    """POST compact, fork, abort"""

    async def test_compact(self, client, headers, session_app):
        sid = await _create_session(client, headers, session_app)
        r = await client.post(
            f"/api/apps/{session_app}/sessions/{sid}/compact",
            headers=headers,
        )
        d = r.json()
        # Should succeed or report nothing to compact
        assert r.status_code < 500

    async def test_fork(self, client, headers, session_app):
        sid = await _create_session(client, headers, session_app)
        r = await client.post(
            f"/api/apps/{session_app}/sessions/{sid}/fork",
            headers=headers,
        )
        d = r.json()
        assert r.status_code < 500

    async def test_abort_no_active(self, client, headers, session_app):
        sid = await _create_session(client, headers, session_app)
        r = await client.post(
            f"/api/apps/{session_app}/sessions/{sid}/abort",
            headers=headers,
        )
        # Should succeed or say nothing to abort
        assert r.status_code < 500

    async def test_undo(self, client, headers, session_app):
        sid = await _create_session(client, headers, session_app)
        r = await client.post(
            f"/api/apps/{session_app}/sessions/{sid}/undo",
            headers=headers,
        )
        assert r.status_code < 500


class TestSessionMemoryWorkspace:
    """GET memory and workspace state."""

    async def test_memory_snapshot(self, client, headers, session_app):
        sid = await _create_session(client, headers, session_app)
        r = await client.get(
            f"/api/apps/{session_app}/sessions/{sid}/memory",
            headers=headers,
        )
        assert r.status_code < 500

    async def test_workspace_state(self, client, headers, session_app):
        sid = await _create_session(client, headers, session_app)
        r = await client.get(
            f"/api/apps/{session_app}/sessions/{sid}/workspace",
            headers=headers,
        )
        assert r.status_code < 500
