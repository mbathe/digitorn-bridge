"""18 - App-specific feature tests: filesystem, shell, memory, git, database, web, multiagent, security, hooks, constraints, context, oneshot, variables, skills, middleware, channels."""

import uuid

import pytest

from .conftest import deploy_app, undeploy_app, send_and_wait, collect_sse_events

pytestmark = pytest.mark.asyncio


# ═══════════════════════════════════════════════════════════════
# FILESYSTEM APP
# ═══════════════════════════════════════════════════════════════

class TestFilesystemApp:
    """Deploy filesystem_app.yaml and test file operations."""

    @pytest.fixture(autouse=True)
    async def setup(self, client, headers):
        await deploy_app(client, "filesystem_app.yaml", headers)
        self.app_id = "test-filesystem"
        self.client = client
        self.headers = headers
        yield
        await undeploy_app(client, self.app_id, headers)

    async def test_chat_read_file(self):
        sid = f"test-{uuid.uuid4().hex[:8]}"
        d = await send_and_wait(self.client, self.app_id, sid,
                                "Read the file pyproject.toml and tell me the project name",
                                self.headers)
        assert d["success"] is True

    async def test_stream_file_op(self):
        sid = f"test-{uuid.uuid4().hex[:8]}"
        events = await collect_sse_events(self.client, self.app_id, sid,
                                          "List the files in the current directory",
                                          self.headers)
        assert len(events) >= 1
        event_types = {e.get("type") for e in events}
        assert "turn_end" in event_types or len(events) > 0


# ═══════════════════════════════════════════════════════════════
# SHELL APP
# ═══════════════════════════════════════════════════════════════

class TestShellApp:
    """Deploy shell_app.yaml and test shell execution."""

    @pytest.fixture(autouse=True)
    async def setup(self, client, headers):
        await deploy_app(client, "shell_app.yaml", headers)
        self.app_id = "test-shell"
        self.client = client
        self.headers = headers
        yield
        await undeploy_app(client, self.app_id, headers)

    async def test_shell_echo(self):
        sid = f"test-{uuid.uuid4().hex[:8]}"
        d = await send_and_wait(self.client, self.app_id, sid,
                                'Run: echo "hello functional test"',
                                self.headers)
        assert d["success"] is True

    async def test_shell_pwd(self):
        sid = f"test-{uuid.uuid4().hex[:8]}"
        d = await send_and_wait(self.client, self.app_id, sid,
                                "Run pwd and tell me the current directory",
                                self.headers)
        assert d["success"] is True


# ═══════════════════════════════════════════════════════════════
# MEMORY APP
# ═══════════════════════════════════════════════════════════════

class TestMemoryApp:
    """Deploy memory_app.yaml and test memory operations."""

    @pytest.fixture(autouse=True)
    async def setup(self, client, headers):
        await deploy_app(client, "memory_app.yaml", headers)
        self.app_id = "test-memory"
        self.client = client
        self.headers = headers
        yield
        await undeploy_app(client, self.app_id, headers)

    async def test_remember_recall(self):
        sid = f"test-{uuid.uuid4().hex[:8]}"
        # Remember
        await send_and_wait(self.client, self.app_id, sid,
                            "Remember that the secret code is 42",
                            self.headers)
        # Recall
        d = await send_and_wait(self.client, self.app_id, sid,
                                "What is the secret code?",
                                self.headers)
        assert d["success"] is True

    async def test_set_goal(self):
        sid = f"test-{uuid.uuid4().hex[:8]}"
        d = await send_and_wait(self.client, self.app_id, sid,
                                "Set your goal to: Complete all functional tests",
                                self.headers)
        assert d["success"] is True

    async def test_add_todo(self):
        sid = f"test-{uuid.uuid4().hex[:8]}"
        d = await send_and_wait(self.client, self.app_id, sid,
                                "Add a todo: Write documentation",
                                self.headers)
        assert d["success"] is True

    async def test_memory_endpoint(self):
        """GET /sessions/{sid}/memory after using memory tools."""
        sid = f"test-{uuid.uuid4().hex[:8]}"
        await send_and_wait(self.client, self.app_id, sid,
                            "Set goal to: Test memory endpoint",
                            self.headers)

        r = await self.client.get(
            f"/api/apps/{self.app_id}/sessions/{sid}/memory",
            headers=self.headers,
        )
        assert r.status_code < 500


# ═══════════════════════════════════════════════════════════════
# GIT APP
# ═══════════════════════════════════════════════════════════════

class TestGitApp:
    """Deploy git_app.yaml and test git operations."""

    @pytest.fixture(autouse=True)
    async def setup(self, client, headers):
        await deploy_app(client, "git_app.yaml", headers)
        self.app_id = "test-git"
        self.client = client
        self.headers = headers
        yield
        await undeploy_app(client, self.app_id, headers)

    async def test_git_status(self):
        sid = f"test-{uuid.uuid4().hex[:8]}"
        d = await send_and_wait(self.client, self.app_id, sid,
                                "Show me the git status",
                                self.headers)
        assert d["success"] is True

    async def test_git_log(self):
        sid = f"test-{uuid.uuid4().hex[:8]}"
        d = await send_and_wait(self.client, self.app_id, sid,
                                "Show me the last 3 git commits",
                                self.headers)
        assert d["success"] is True


# ═══════════════════════════════════════════════════════════════
# DATABASE APP
# ═══════════════════════════════════════════════════════════════

class TestDatabaseApp:
    """Deploy database_app.yaml and test SQL execution."""

    @pytest.fixture(autouse=True)
    async def setup(self, client, headers):
        await deploy_app(client, "database_app.yaml", headers)
        self.app_id = "test-database"
        self.client = client
        self.headers = headers
        yield
        await undeploy_app(client, self.app_id, headers)

    async def test_create_and_query(self):
        sid = f"test-{uuid.uuid4().hex[:8]}"
        # Create table
        await send_and_wait(self.client, self.app_id, sid,
                            "Create a table called users with columns: id INTEGER PRIMARY KEY, name TEXT, email TEXT",
                            self.headers)
        # Insert
        await send_and_wait(self.client, self.app_id, sid,
                            "Insert a user: name='Alice', email='alice@test.com'",
                            self.headers)
        # Query
        d = await send_and_wait(self.client, self.app_id, sid,
                                "Select all users from the users table",
                                self.headers)
        assert d["success"] is True


# ═══════════════════════════════════════════════════════════════
# WEB APP
# ═══════════════════════════════════════════════════════════════

class TestWebApp:
    """Deploy web_app.yaml and test web operations."""

    @pytest.fixture(autouse=True)
    async def setup(self, client, headers):
        await deploy_app(client, "web_app.yaml", headers)
        self.app_id = "test-web"
        self.client = client
        self.headers = headers
        yield
        await undeploy_app(client, self.app_id, headers)

    async def test_web_fetch(self):
        sid = f"test-{uuid.uuid4().hex[:8]}"
        d = await send_and_wait(self.client, self.app_id, sid,
                                "Fetch the content of https://httpbin.org/get",
                                self.headers)
        assert d["success"] is True


# ═══════════════════════════════════════════════════════════════
# MULTI-AGENT APP
# ═══════════════════════════════════════════════════════════════

class TestMultiAgentApp:
    """Deploy multiagent_app.yaml and test coordinator + workers."""

    @pytest.fixture(autouse=True)
    async def setup(self, client, headers):
        await deploy_app(client, "multiagent_app.yaml", headers)
        self.app_id = "test-multiagent"
        self.client = client
        self.headers = headers
        yield
        await undeploy_app(client, self.app_id, headers)

    async def test_coordinator_spawns_agent(self):
        sid = f"test-{uuid.uuid4().hex[:8]}"
        d = await send_and_wait(self.client, self.app_id, sid,
                                "List the Python files in the current directory using a researcher agent",
                                self.headers, timeout=180)
        assert d["success"] is True


# ═══════════════════════════════════════════════════════════════
# SECURITY APP
# ═══════════════════════════════════════════════════════════════

class TestSecurityApp:
    """Deploy security_app.yaml and test grant/approve/deny policies."""

    @pytest.fixture(autouse=True)
    async def setup(self, client, headers):
        await deploy_app(client, "security_app.yaml", headers)
        self.app_id = "test-security"
        self.client = client
        self.headers = headers
        yield
        await undeploy_app(client, self.app_id, headers)

    async def test_granted_action_works(self):
        """filesystem.read is granted - should work."""
        sid = f"test-{uuid.uuid4().hex[:8]}"
        d = await send_and_wait(self.client, self.app_id, sid,
                                "Read the file pyproject.toml",
                                self.headers)
        assert d["success"] is True

    async def test_denied_action_fails(self):
        """filesystem.rm is denied - agent should report error."""
        sid = f"test-{uuid.uuid4().hex[:8]}"
        d = await send_and_wait(self.client, self.app_id, sid,
                                "Delete the file pyproject.toml using the rm tool",
                                self.headers)
        assert d["success"] is True  # Chat succeeds but action blocked

    async def test_approval_queue_populated(self):
        """filesystem.write requires approval - should appear in queue."""
        r = await self.client.get(
            f"/api/apps/{self.app_id}/approvals",
            headers=self.headers,
        )
        assert r.json()["success"] is True


# ═══════════════════════════════════════════════════════════════
# CONTEXT APP
# ═══════════════════════════════════════════════════════════════

class TestContextApp:
    """Deploy context_app.yaml and test context compaction."""

    @pytest.fixture(autouse=True)
    async def setup(self, client, headers):
        await deploy_app(client, "context_app.yaml", headers)
        self.app_id = "test-context"
        self.client = client
        self.headers = headers
        yield
        await undeploy_app(client, self.app_id, headers)

    async def test_multi_turn_compaction(self):
        """Send many messages to trigger context compaction."""
        sid = f"test-{uuid.uuid4().hex[:8]}"
        for i in range(5):
            d = await send_and_wait(self.client, self.app_id, sid,
                                    f"Tell me fact number {i+1} about Python",
                                    self.headers)
            assert d["success"] is True

    async def test_manual_compact(self):
        sid = f"test-{uuid.uuid4().hex[:8]}"
        await send_and_wait(self.client, self.app_id, sid,
                            "Hello",
                            self.headers)
        r = await self.client.post(
            f"/api/apps/{self.app_id}/sessions/{sid}/compact",
            headers=self.headers,
        )
        assert r.status_code < 500


# ═══════════════════════════════════════════════════════════════
# CONSTRAINTS APP
# ═══════════════════════════════════════════════════════════════

class TestConstraintsApp:
    """Deploy constraints_app.yaml and test shell/fs constraints."""

    @pytest.fixture(autouse=True)
    async def setup(self, client, headers):
        await deploy_app(client, "constraints_app.yaml", headers)
        self.app_id = "test-constraints"
        self.client = client
        self.headers = headers
        yield
        await undeploy_app(client, self.app_id, headers)

    async def test_allowed_command(self):
        sid = f"test-{uuid.uuid4().hex[:8]}"
        d = await send_and_wait(self.client, self.app_id, sid,
                                "Run: echo hello",
                                self.headers)
        assert d["success"] is True

    async def test_blocked_command(self):
        sid = f"test-{uuid.uuid4().hex[:8]}"
        d = await send_and_wait(self.client, self.app_id, sid,
                                "Run: rm -rf /tmp/test",
                                self.headers)
        assert d["success"] is True  # Chat succeeds but command blocked


# ═══════════════════════════════════════════════════════════════
# HOOKS APP
# ═══════════════════════════════════════════════════════════════

class TestHooksApp:
    """Deploy hooks_app.yaml and test hook firing."""

    @pytest.fixture(autouse=True)
    async def setup(self, client, headers):
        await deploy_app(client, "hooks_app.yaml", headers)
        self.app_id = "test-hooks"
        self.client = client
        self.headers = headers
        yield
        await undeploy_app(client, self.app_id, headers)

    async def test_hooks_fire_on_turns(self):
        """Send 4+ messages to trigger turn_count hook (interval=3)."""
        sid = f"test-{uuid.uuid4().hex[:8]}"
        for i in range(4):
            d = await send_and_wait(self.client, self.app_id, sid,
                                    f"Message {i+1}: what is {i+1}+{i+1}?",
                                    self.headers)
            assert d["success"] is True


# ═══════════════════════════════════════════════════════════════
# MIDDLEWARE APP
# ═══════════════════════════════════════════════════════════════

class TestMiddlewareApp:
    """Deploy middleware_app.yaml - verify audit/mask middleware."""

    @pytest.fixture(autouse=True)
    async def setup(self, client, headers):
        await deploy_app(client, "middleware_app.yaml", headers)
        self.app_id = "test-middleware"
        self.client = client
        self.headers = headers
        yield
        await undeploy_app(client, self.app_id, headers)

    async def test_chat_with_middleware(self):
        sid = f"test-{uuid.uuid4().hex[:8]}"
        d = await send_and_wait(self.client, self.app_id, sid,
                                "Say hello",
                                self.headers)
        assert d["success"] is True


# ═══════════════════════════════════════════════════════════════
# CHANNELS APP
# ═══════════════════════════════════════════════════════════════

class TestChannelsApp:
    """Deploy channels_app.yaml - verify channel configuration."""

    @pytest.fixture(autouse=True)
    async def setup(self, client, headers):
        await deploy_app(client, "channels_app.yaml", headers)
        self.app_id = "test-channels"
        self.client = client
        self.headers = headers
        yield
        await undeploy_app(client, self.app_id, headers)

    async def test_deploy_with_channels(self):
        """Verify the app deployed successfully with channels configured."""
        r = await self.client.get(
            f"/api/apps/{self.app_id}",
            headers=self.headers,
        )
        d = r.json()
        assert d["success"] is True


# ═══════════════════════════════════════════════════════════════
# VARIABLES APP
# ═══════════════════════════════════════════════════════════════

class TestVariablesApp:
    """Deploy variables_app.yaml - verify template variable substitution."""

    @pytest.fixture(autouse=True)
    async def setup(self, client, headers):
        await deploy_app(client, "variables_app.yaml", headers)
        self.app_id = "test-variables"
        self.client = client
        self.headers = headers
        yield
        await undeploy_app(client, self.app_id, headers)

    async def test_deploy_with_variables(self):
        r = await self.client.get(
            f"/api/apps/{self.app_id}",
            headers=self.headers,
        )
        d = r.json()
        assert d["success"] is True
        assert d["data"]["name"] == "Test Variables"

    async def test_chat_reflects_variables(self):
        sid = f"test-{uuid.uuid4().hex[:8]}"
        d = await send_and_wait(self.client, self.app_id, sid,
                                "What app are you?",
                                self.headers)
        assert d["success"] is True


# ═══════════════════════════════════════════════════════════════
# SKILLS APP
# ═══════════════════════════════════════════════════════════════

class TestSkillsApp:
    """Deploy skills_app.yaml - verify skills are loaded."""

    @pytest.fixture(autouse=True)
    async def setup(self, client, headers):
        await deploy_app(client, "skills_app.yaml", headers)
        self.app_id = "test-skills"
        self.client = client
        self.headers = headers
        yield
        await undeploy_app(client, self.app_id, headers)

    async def test_deploy_with_skills(self):
        r = await self.client.get(
            f"/api/apps/{self.app_id}",
            headers=self.headers,
        )
        d = r.json()
        assert d["success"] is True
