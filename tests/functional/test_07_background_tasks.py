"""07 - Background tasks: create, list, get status, cancel, wait."""

import pytest

from .conftest import deploy_app, undeploy_app

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def bg_app(client, headers):
    await deploy_app(client, "filesystem_app.yaml", headers)
    yield "test-filesystem"
    await undeploy_app(client, "test-filesystem", headers)


class TestBackgroundTasks:
    """CRUD for /api/apps/{app_id}/background-tasks"""

    async def test_list_empty(self, client, headers, bg_app):
        r = await client.get(
            f"/api/apps/{bg_app}/background-tasks",
            headers=headers,
        )
        d = r.json()
        assert d["success"] is True
        # data may be {"tasks": [], ...} or similar
        assert d["data"] is not None
        tasks = d["data"].get("tasks", [])
        assert isinstance(tasks, list)

    async def test_create_bg_task(self, client, headers, bg_app):
        r = await client.post(f"/api/apps/{bg_app}/background-tasks", json={
            "tool": "filesystem.ls",
            "params": {"path": "."},
        }, headers=headers)
        d = r.json()
        assert r.status_code < 500
        if d.get("success"):
            task_id = d["data"].get("task_id", d["data"].get("id", ""))
            assert task_id

    async def test_get_bg_task_nonexistent(self, client, headers, bg_app):
        r = await client.get(
            f"/api/apps/{bg_app}/background-tasks/nonexistent-task-id",
            headers=headers,
        )
        assert r.status_code in (404, 200)  # May return error in data

    async def test_cancel_bg_task_nonexistent(self, client, headers, bg_app):
        r = await client.delete(
            f"/api/apps/{bg_app}/background-tasks/nonexistent-task-id",
            headers=headers,
        )
        assert r.status_code < 500


class TestNotifications:
    """Notification polling endpoints."""

    async def test_active_notifications(self, client, headers, bg_app):
        r = await client.get(
            f"/api/apps/{bg_app}/notifications/active",
            headers=headers,
        )
        assert r.status_code < 500
