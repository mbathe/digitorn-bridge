"""12 — Requirements API: list, install."""

import pytest

pytestmark = pytest.mark.asyncio


class TestRequirements:
    """GET/POST /api/requires"""

    async def test_list_requirements(self, client, headers):
        r = await client.get("/api/requires", headers=headers)
        assert r.status_code < 500
        d = r.json()
        assert isinstance(d, (dict, list))

    async def test_install_nonexistent(self, client, headers):
        """Install a nonexistent requirement should fail gracefully."""
        r = await client.post("/api/requires/install", json={
            "name": "nonexistent-package-xyz-12345",
        }, headers=headers)
        assert r.status_code < 500
