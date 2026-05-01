"""Scenarios 07 (bearer_token, GitHub) + 08 (basic_auth, internal proxy).

Both use the http module's credential_slot to auto-inject auth on
outgoing calls. Same mock server (external_apis on port 9990) hosts
the GitHub-style + Basic-auth-protected endpoints.

Validates:
  * `bearer_token` handler injects -> Authorization: Bearer <token>.
  * `basic_auth` handler injects -> Authorization: Basic base64(u:p).
  * Mock server records the auth header and we assert the right
    secret arrived.
"""
from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest

from shared.chat_helpers import chat
from shared.deploy_helpers import (
    assert_credential_resolved, deploy_app, undeploy_app,
)
from shared.vault_helpers import (
    create_user_credential, delete_credential, reset_credential,
)


HERE = Path(__file__).resolve().parents[1]
MOCK_PORT = 9990
GITHUB_TOKEN = "ghp_T_E2E_GITHUB_TEST_TOKEN_aaaaaaaaaaaa"
BASIC_USER = "alice"
BASIC_PASS = "S3cure!Pass"


def _port_open(port: int, host: str = "127.0.0.1") -> bool:
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(0.3)
    try:
        s.connect((host, port))
        return True
    except OSError:
        return False
    finally:
        s.close()


@pytest.fixture(scope="module")
def mock_apis():
    proc = subprocess.Popen(
        [sys.executable, str(HERE / "mocks" / "external_apis.py")],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    deadline = time.time() + 10
    while time.time() < deadline:
        if _port_open(MOCK_PORT):
            break
        time.sleep(0.2)
    yield
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()


@pytest.fixture(scope="module")
def deepseek_key() -> str:
    key = os.environ.get("DIGITORN_DEEPSEEK_KEY", "").strip()
    if not key:
        env_file = Path(__file__).resolve().parents[3] / ".env"
        if env_file.is_file():
            for line in env_file.read_text(encoding="utf-8").splitlines():
                if line.startswith("DEEPSEEK_API_KEY="):
                    key = line.split("=", 1)[1].strip().strip('"').strip("'")
                    break
    if not key:
        pytest.skip("no real DeepSeek key for chat tests")
    return key


def _read_received() -> list[dict]:
    with urllib.request.urlopen(
        f"http://127.0.0.1:{MOCK_PORT}/__received", timeout=5,
    ) as r:
        return json.loads(r.read())["received"]


def _reset_mock() -> None:
    req = urllib.request.Request(
        f"http://127.0.0.1:{MOCK_PORT}/__reset", method="POST",
    )
    urllib.request.urlopen(req, timeout=5).read()


def _setup_deepseek_cred(key: str) -> str:
    reset_credential("e2e_deepseek_test")
    return create_user_credential(
        name="e2e_deepseek_test",
        provider_name="deepseek",
        provider_type="api_key",
        scope="per_user",
        fields={"api_key": key},
    )


# ─── Scenario 07: GitHub bearer_token ──────────────────────────────


@pytest.fixture
def vault_github(daemon_url, jwt_present, deepseek_key, mock_apis):
    deepseek_id = _setup_deepseek_cred(deepseek_key)
    reset_credential("e2e_github_pat")
    github_id = create_user_credential(
        name="e2e_github_pat",
        provider_name="github_pat",
        provider_type="bearer_token",
        scope="per_user",
        fields={"token": GITHUB_TOKEN},
    )
    yield {"deepseek": deepseek_id, "github": github_id}
    delete_credential(deepseek_id)
    delete_credential(github_id)


def test_07_github_bearer_chat(daemon_url, vault_github):
    deploy_app(str(HERE / "apps" / "07_github_devops.yaml"), daemon=daemon_url)
    e1 = assert_credential_resolved(
        "e2e-cred-07-github", "e2e_github_pat", daemon=daemon_url,
    )
    print(f"   github resolved on {e1['block']}")
    _reset_mock()
    try:
        conv = chat(
            "e2e-cred-07-github",
            ["List my GitHub repos. Just the names + their primary language."],
            daemon=daemon_url,
            timeout_per_turn=120,
        )
        print("\n--- 07-github ---")
        print(conv.transcript)
        assert conv.turns[0].error is None, conv.turns[0].error
        reply = conv.turns[0].assistant_text.lower()
        # Should mention at least 2 of the 3 mock repos.
        names = sum(1 for n in ("alpha", "beta", "gamma") if n in reply)
        assert names >= 2, f"agent didn't list repos: {reply!r}"

        # Mock should have received the GitHub PAT as Bearer.
        received = _read_received()
        gh_calls = [r for r in received if r["path"].startswith("/github")]
        assert gh_calls, f"mock got no /github calls. all={received}"
        bearer = gh_calls[0]["auth"]
        assert GITHUB_TOKEN in bearer, (
            f"vault PAT not in auth: {bearer!r}"
        )
    finally:
        undeploy_app("e2e-cred-07-github", daemon=daemon_url)


# ─── Scenario 08: basic_auth proxy ─────────────────────────────────


@pytest.fixture
def vault_basic(daemon_url, jwt_present, deepseek_key, mock_apis):
    deepseek_id = _setup_deepseek_cred(deepseek_key)
    reset_credential("e2e_basic_creds")
    basic_id = create_user_credential(
        name="e2e_basic_creds",
        provider_name="internal_proxy",
        provider_type="basic_auth",
        scope="per_user",
        fields={"username": BASIC_USER, "password": BASIC_PASS},
    )
    yield {"deepseek": deepseek_id, "basic": basic_id}
    delete_credential(deepseek_id)
    delete_credential(basic_id)


def test_08_basic_auth_chat(daemon_url, vault_basic):
    deploy_app(str(HERE / "apps" / "08_basic_auth_proxy.yaml"), daemon=daemon_url)
    e1 = assert_credential_resolved(
        "e2e-cred-08-basic", "e2e_basic_creds", daemon=daemon_url,
    )
    print(f"   basic resolved on {e1['block']}")
    _reset_mock()
    try:
        conv = chat(
            "e2e-cred-08-basic",
            ["Check the proxy status. Just give me the message."],
            daemon=daemon_url,
            timeout_per_turn=120,
        )
        print("\n--- 08-basic ---")
        print(conv.transcript)
        assert conv.turns[0].error is None, conv.turns[0].error
        # Mock returns 200 with message="Authenticated.".
        assert "authenticated" in conv.turns[0].assistant_text.lower(), (
            f"agent reply: {conv.turns[0].assistant_text!r}"
        )

        # Verify Basic auth header arrived.
        received = _read_received()
        proxy_calls = [r for r in received if r["path"] == "/proxy/me"]
        assert proxy_calls, f"mock got no /proxy/me. all={received}"
        auth = proxy_calls[0]["auth"]
        assert auth.startswith("Basic "), f"expected Basic auth: {auth!r}"
        # Decode base64 and verify it's user:password.
        encoded = auth.split(" ", 1)[1]
        decoded = base64.b64decode(encoded).decode()
        assert decoded == f"{BASIC_USER}:{BASIC_PASS}", (
            f"basic decoded mismatch: {decoded!r}"
        )
    finally:
        undeploy_app("e2e-cred-08-basic", daemon=daemon_url)
