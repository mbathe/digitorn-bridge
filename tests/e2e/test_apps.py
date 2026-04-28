"""E2E App Tests - deploy real YAML apps and test them with DeepSeek.

Each test deploys a YAML app, sends a message, and verifies the response.
Tests are independent - each app is deployed fresh.
"""

import os
import pytest
import httpx
from pathlib import Path

DAEMON = os.environ.get("DIGITORN_TEST_DAEMON", "http://127.0.0.1:8000")
KEY = os.environ.get("DEEPSEEK_API_KEY", "***REMOVED***")
TIMEOUT = 300.0
APPS_DIR = Path(__file__).parent / "apps"


def _deploy(yaml_path: str) -> dict:
    content = Path(yaml_path).read_text()
    content = content.replace("DEEPSEEK_KEY", KEY)
    tmp = f"/tmp/e2e_{Path(yaml_path).stem}.yaml"
    Path(tmp).write_text(content)
    resp = httpx.post(
        f"{DAEMON}/api/apps/deploy",
        json={"yaml_path": tmp, "force": True},
        timeout=30.0,
    )
    return resp.json()


def _chat(app_id: str, message: str, session_id: str = "") -> dict:
    import uuid
    sid = session_id or f"e2e-{uuid.uuid4().hex[:8]}"
    resp = httpx.post(
        f"{DAEMON}/api/apps/{app_id}/chat",
        json={"session_id": sid, "message": message},
        timeout=TIMEOUT,
    )
    return resp.json()


def _run(app_id: str, input_text: str) -> dict:
    resp = httpx.post(
        f"{DAEMON}/api/apps/{app_id}/run",
        json={"input": input_text},
        timeout=TIMEOUT,
    )
    return resp.json()


def _ok(r: dict) -> bool:
    return r.get("success", False)


def _content(r: dict) -> str:
    return r.get("data", {}).get("content", "")


def _tools(r: dict) -> int:
    return r.get("data", {}).get("tool_calls_count", 0)


@pytest.fixture(scope="session", autouse=True)
def check_daemon():
    try:
        r = httpx.get(f"{DAEMON}/health", timeout=5.0)
        if r.status_code != 200:
            pytest.skip("Daemon not running")
    except Exception:
        pytest.skip("Daemon not running")


class TestApp01MinimalChat:
    def setup_method(self):
        _deploy(str(APPS_DIR / "01-minimal-chat.yaml"))

    def test_simple_response(self):
        r = _chat("e2e-01-minimal", "Say hello")
        assert _ok(r) and _content(r)

    def test_no_tools_needed(self):
        r = _chat("e2e-01-minimal", "What is 3+3?")
        assert _ok(r) and "6" in _content(r)
        assert _tools(r) == 0


class TestApp02TextAnalyzer:
    def setup_method(self):
        _deploy(str(APPS_DIR / "02-text-analyzer.yaml"))

    def test_one_shot(self):
        r = _run("e2e-02-analyzer", "Hello world. This is a test sentence.")
        assert _ok(r)
        assert _content(r)


class TestApp03FileReader:
    def setup_method(self):
        _deploy(str(APPS_DIR / "03-file-reader.yaml"))

    def test_read_file(self):
        r = _chat("e2e-03-reader", "Read pyproject.toml and tell me the project name.")
        assert _ok(r) and _tools(r) > 0
        assert "digitorn" in _content(r).lower()

    def test_list_files(self):
        r = _chat("e2e-03-reader", "List files in the root directory.")
        assert _ok(r) and _tools(r) > 0

    def test_grep(self):
        r = _chat("e2e-03-reader", "Search for 'def main' in packages/digitorn/core/server.py")
        assert _ok(r) and _tools(r) > 0


class TestApp04GitInspector:
    def setup_method(self):
        _deploy(str(APPS_DIR / "04-git-inspector.yaml"))

    def test_status(self):
        r = _chat("e2e-04-git", "Show git status.")
        assert _ok(r) and _tools(r) > 0

    def test_log(self):
        r = _chat("e2e-04-git", "Show the last 3 commits.")
        assert _ok(r) and _tools(r) > 0

    def test_branches(self):
        r = _chat("e2e-04-git", "List branches.")
        assert _ok(r) and _tools(r) > 0


class TestApp05ShellRunner:
    def setup_method(self):
        _deploy(str(APPS_DIR / "05-shell-runner.yaml"))

    def test_run_command(self):
        r = _chat("e2e-05-shell", "Run 'python --version'")
        assert _ok(r) and _tools(r) > 0
        assert "python" in _content(r).lower()

    def test_which(self):
        r = _chat("e2e-05-shell", "Check if git is installed.")
        assert _ok(r) and _tools(r) > 0


class TestApp06Memory:
    def setup_method(self):
        _deploy(str(APPS_DIR / "06-memory-agent.yaml"))

    def test_set_goal(self):
        r = _chat("e2e-06-memory", "Set your goal to: analyze security", session_id="mem-1")
        assert _ok(r) and _tools(r) > 0

    def test_add_fact(self):
        r = _chat("e2e-06-memory", "Remember this fact: Python 3.12 is used", session_id="mem-2")
        assert _ok(r) and _tools(r) > 0


class TestApp07LockedDown:
    def setup_method(self):
        _deploy(str(APPS_DIR / "07-locked-down.yaml"))

    def test_read_allowed(self):
        r = _chat("e2e-07-locked", "Read docs/index.md")
        assert _ok(r)

    def test_write_denied(self):
        r = _chat("e2e-07-locked", "Create a file called test.txt")
        assert _ok(r)
        c = _content(r).lower()
        assert "denied" in c or "cannot" in c or "not allowed" in c or "permission" in c or not _tools(r)


class TestApp08MultiAgent:
    def setup_method(self):
        _deploy(str(APPS_DIR / "08-multi-agent.yaml"))

    def test_spawn_explore(self):
        r = _chat("e2e-08-multi", "Find all Python files in packages/digitorn/core/ that contain 'class'")
        assert _ok(r) and _tools(r) > 0

    def test_delegate(self):
        r = _chat("e2e-08-multi", "Spawn an explore agent to list all YAML files in examples/")
        assert _ok(r) and _tools(r) > 0


class TestApp09Database:
    def setup_method(self):
        _deploy(str(APPS_DIR / "09-database.yaml"))

    def test_connect_sqlite(self):
        r = _chat("e2e-09-db", "Connect to an in-memory SQLite database.")
        assert _ok(r) and _tools(r) > 0


class TestApp10HTTP:
    def setup_method(self):
        _deploy(str(APPS_DIR / "10-http.yaml"))

    def test_get_request(self):
        r = _chat("e2e-10-http", "Make a GET request to https://httpbin.org/get")
        assert _ok(r) and _tools(r) > 0


class TestApp11WebSearch:
    def setup_method(self):
        _deploy(str(APPS_DIR / "11-web-search.yaml"))

    def test_search(self):
        r = _chat("e2e-11-web", "Search for 'Python FastAPI tutorial'")
        assert _ok(r) and _tools(r) > 0


class TestApp12CodeReviewer:
    def setup_method(self):
        _deploy(str(APPS_DIR / "12-code-reviewer.yaml"))

    def test_review_file(self):
        r = _chat("e2e-12-reviewer", "Review packages/digitorn/core/pipeline.py")
        assert _ok(r) and _tools(r) > 0


class TestApp13TestRunner:
    def setup_method(self):
        _deploy(str(APPS_DIR / "13-test-runner.yaml"))

    def test_run_tests(self):
        r = _chat("e2e-13-runner", "Run 'python -m pytest tests/test_pipeline.py -q' and report results.")
        assert _ok(r) and _tools(r) > 0
        assert "passed" in _content(r).lower()


class TestApp14FullStack:
    def setup_method(self):
        _deploy(str(APPS_DIR / "14-full-stack.yaml"))

    def test_multi_tool(self):
        r = _chat("e2e-14-fullstack", "Read pyproject.toml, show git status, and list tests/")
        assert _ok(r) and _tools(r) >= 2

    def test_memory_and_tools(self):
        sid = "fullstack-1"
        r1 = _chat("e2e-14-fullstack", "Set goal: analyze the project. Then list files.", session_id=sid)
        assert _ok(r1) and _tools(r1) > 0


class TestApp15OneShot:
    def setup_method(self):
        _deploy(str(APPS_DIR / "15-one-shot-json.yaml"))

    def test_json_output(self):
        r = _run("e2e-15-json", "Analyze this: Digitorn is a YAML framework for AI agents.")
        assert _ok(r) and _content(r)


class TestApp17RestrictedShell:
    def setup_method(self):
        _deploy(str(APPS_DIR / "17-restricted-shell.yaml"))

    def test_which_allowed(self):
        r = _chat("e2e-17-restricted", "Check if python is installed.")
        assert _ok(r) and _tools(r) > 0

    def test_run_denied(self):
        r = _chat("e2e-17-restricted", "Run 'ls'")
        assert _ok(r)
        c = _content(r).lower()
        assert "denied" in c or "cannot" in c or "not allowed" in c or "don't have" in c or _tools(r) == 0


class TestApp19NoModules:
    def setup_method(self):
        _deploy(str(APPS_DIR / "19-no-modules.yaml"))

    def test_pure_chat(self):
        r = _chat("e2e-19-nomod", "What is the capital of France?")
        assert _ok(r) and "paris" in _content(r).lower()
        assert _tools(r) == 0

    def test_no_tool_access(self):
        r = _chat("e2e-19-nomod", "Read pyproject.toml")
        assert _ok(r)
        assert _tools(r) == 0


class TestApp20MaxFeatures:
    def setup_method(self):
        _deploy(str(APPS_DIR / "20-max-features.yaml"))

    def test_all_tools_available(self):
        r = _chat("e2e-20-max", "List your tool categories.")
        assert _ok(r) and _tools(r) > 0

    def test_complex_workflow(self):
        r = _chat("e2e-20-max", "Read pyproject.toml, show git log -3, and set a goal to understand the project.")
        assert _ok(r) and _tools(r) >= 2
