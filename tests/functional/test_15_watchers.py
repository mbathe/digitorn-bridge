"""15 - Watchers: create, list, pause, resume, delete."""

import pytest

from .conftest import deploy_app, undeploy_app

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def watcher_app(client, headers):
    await deploy_app(client, "filesystem_app.yaml", headers)
    yield "test-filesystem"
    await undeploy_app(client, "test-filesystem", headers)


class TestWatchers:
    """CRUD /api/apps/{app_id}/watchers"""

    async def test_list_empty(self, client, headers, watcher_app):
        r = await client.get(
            f"/api/apps/{watcher_app}/watchers",
            headers=headers,
        )
        assert r.status_code < 500

    async def test_create_watcher(self, client, headers, watcher_app):
        r = await client.post(f"/api/apps/{watcher_app}/watchers", json={
            "pattern": "*.py",
            "path": ".",
            "event_types": ["modify", "create"],
        }, headers=headers)
        assert r.status_code < 500

    async def test_get_nonexistent(self, client, headers, watcher_app):
        r = await client.get(
            f"/api/apps/{watcher_app}/watchers/nonexistent-id",
            headers=headers,
        )
        assert r.status_code < 500

    async def test_delete_nonexistent(self, client, headers, watcher_app):
        r = await client.delete(
            f"/api/apps/{watcher_app}/watchers/nonexistent-id",
            headers=headers,
        )
        assert r.status_code < 500

    async def test_pause_nonexistent(self, client, headers, watcher_app):
        r = await client.post(
            f"/api/apps/{watcher_app}/watchers/nonexistent-id/pause",
            headers=headers,
        )
        assert r.status_code < 500

    async def test_resume_nonexistent(self, client, headers, watcher_app):
        r = await client.post(
            f"/api/apps/{watcher_app}/watchers/nonexistent-id/resume",
            headers=headers,
        )
        assert r.status_code < 500
