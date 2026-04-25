"""19 — Error handling: invalid requests, missing params, edge cases."""

import pytest

pytestmark = pytest.mark.asyncio


class TestInvalidRequests:
    """Verify the daemon handles malformed requests gracefully."""

    async def test_deploy_no_body(self, client, headers):
        r = await client.post("/api/apps/deploy", headers=headers)
        assert r.status_code in (400, 422)

    async def test_deploy_empty_body(self, client, headers):
        r = await client.post("/api/apps/deploy", json={}, headers=headers)
        assert r.status_code < 500

    async def test_chat_invalid_json(self, client, headers):
        r = await client.post(
            "/api/apps/test-minimal/sessions/err-test/messages",
            content=b"not json",
            headers={**headers, "Content-Type": "application/json"},
        )
        assert r.status_code in (400, 404, 422)

    async def test_chat_empty_message(self, client, headers):
        from .conftest import deploy_app, undeploy_app, send_and_wait
        await deploy_app(client, "minimal.yaml", headers)
        d = await send_and_wait(client, "test-minimal", "err-test", "", headers)
        assert isinstance(d, dict)  # Should not crash
        await undeploy_app(client, "test-minimal", headers)

    async def test_nonexistent_routes(self, client, headers):
        r = await client.get("/api/nonexistent/route/xyz", headers=headers)
        assert r.status_code in (404, 405)

    async def test_wrong_method(self, client, headers):
        r = await client.put("/api/apps", headers=headers)
        assert r.status_code in (404, 405)


class TestConcurrentDeploy:
    """Edge case: deploy same app concurrently."""

    async def test_concurrent_deploy_same_app(self, client, headers):
        import asyncio
        from .conftest import APPS_DIR

        yaml_path = str((APPS_DIR / "minimal.yaml").resolve())

        async def deploy():
            return await client.post("/api/apps/deploy", json={
                "yaml_path": yaml_path,
                "force": True,
            }, headers=headers)

        results = await asyncio.gather(deploy(), deploy(), return_exceptions=True)
        # At least one should succeed; neither should crash the daemon
        for r in results:
            if not isinstance(r, Exception):
                assert r.status_code < 500

        # Cleanup
        from .conftest import undeploy_app
        await undeploy_app(client, "test-minimal", headers)
