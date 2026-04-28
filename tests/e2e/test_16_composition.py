"""E2E Tests: App composition - call_app, pipeline."""

import pytest
from tests.e2e.conftest import *


class TestCallApp:
    def test_call_app_available(self, client, ensure_deployed):
        r = client.chat("What tools do you have for calling other apps?")
        assert_success(r)
        assert_has_content(r)

    def test_search_call_app(self, client, ensure_deployed):
        r = client.chat("Search for a tool called 'call_app'.")
        assert_success(r)
        assert_used_tools(r)


class TestPipelineAPI:
    def test_pipeline_endpoint_no_steps(self, client, ensure_deployed):
        resp = httpx.post(
            f"{client.daemon_url}/api/apps/{client.app_id}/pipeline",
            json={"input": "test", "steps": []},
            timeout=10.0,
        )
        assert resp.status_code == 400
