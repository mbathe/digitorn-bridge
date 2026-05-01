"""Comprehensive coverage: every remaining handler exercised via a
real agent + real http call to a mock external service.

Each test case:
  1. Creates the vault credential (handler-specific fields).
  2. Deploys an app whose http module is bound to it.
  3. Real LLM agent calls the mock endpoint.
  4. Mock asserts the credential's value reached its endpoint.

Handlers covered here:
  - aws_access_key
  - gcp_service_account
  - azure_ad
  - hmac_signing_secret
  - file_upload
  - custom

Handlers needing dedicated protocol mocks (separate test files):
  - ssh_key (paramiko SSH server)
  - client_certificate (mTLS)
  - mcp_server / mcp_http (MCP protocol)
  - connection_string / database_fields (Postgres protocol)
"""
from __future__ import annotations

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


def _setup_deepseek(key: str) -> str:
    reset_credential("e2e_deepseek_test")
    return create_user_credential(
        name="e2e_deepseek_test",
        provider_name="deepseek",
        provider_type="api_key",
        scope="per_user",
        fields={"api_key": key},
    )


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


def _write_app_yaml(
    app_id: str, cred_name: str, system_prompt: str,
    actions: list[str] = ("get",),
) -> Path:
    """Write a temporary YAML for the test."""
    yaml = f"""\
app:
  app_id: {app_id}
  name: "{app_id}"
  category: test

modules:
  memory:
    config:
      working_memory: true
  context_builder: {{}}
  http:
    config: {{}}
    constraints:
      allowed_hosts: ["127.0.0.1"]
    credential:
      ref: {cred_name}
      scope: per_user

agents:
  - id: main
    role: assistant
    brain:
      provider: deepseek
      model: deepseek-chat
      backend: openai_compat
      credential:
        ref: e2e_deepseek_test
        scope: per_user
        provider: deepseek
      config:
        base_url: "https://api.deepseek.com/v1"
      temperature: 0.0
      max_tokens: 300
    system_prompt: |
{chr(10).join("      " + ln for ln in system_prompt.split(chr(10)))}

execution:
  mode: conversation
  workspace_mode: none
  tool_injection: direct
  direct_modules: [memory, http]
  greeting: "Bot ready."
  max_turns: 4

capabilities:
  default_policy: auto
  grant:
    - module: memory
      actions: [set_goal, remember]
    - module: http
      actions: {list(actions)!r}
"""
    p = HERE / "apps" / f"_tmp_{app_id}.yaml"
    p.write_text(yaml, encoding="utf-8")
    return p


# ─── Test 09: aws_access_key ────────────────────────────────────────


def test_09_aws_access_key(daemon_url, jwt_present, deepseek_key, mock_apis):
    AWS_KEY = "AKIAIOSFODNN7EXAMPLE"
    deepseek_id = _setup_deepseek(deepseek_key)
    reset_credential("e2e_aws_test")
    aws_id = create_user_credential(
        name="e2e_aws_test",
        provider_name="aws",
        provider_type="aws_access_key",
        scope="per_user",
        fields={
            "access_key_id": AWS_KEY,
            "secret_access_key": "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
            "region": "us-east-1",
        },
    )
    yaml_path = _write_app_yaml(
        "e2e-cred-09-aws", "e2e_aws_test",
        "You are an AWS inventory bot. To list S3 buckets, GET\n"
        "http://127.0.0.1:9990/aws/s3/list-buckets.\n"
        "Reply with the bucket names, comma-separated.",
    )
    try:
        deploy_app(str(yaml_path), daemon=daemon_url)
        assert_credential_resolved("e2e-cred-09-aws", "e2e_aws_test", daemon=daemon_url)
        _reset_mock()
        conv = chat("e2e-cred-09-aws", ["List my S3 buckets."], daemon=daemon_url, timeout_per_turn=120)
        print("\n--- 09-aws ---")
        print(conv.transcript)
        assert conv.turns[0].error is None
        reply = conv.turns[0].assistant_text.lower()
        assert "alpha-bucket" in reply or "alpha" in reply
        # Mock should have received the AWS key.
        received = _read_received()
        aws_calls = [r for r in received if "/aws/" in r["path"]]
        assert aws_calls, f"no aws calls: {received}"
    finally:
        undeploy_app("e2e-cred-09-aws", daemon=daemon_url)
        delete_credential(aws_id)
        delete_credential(deepseek_id)
        yaml_path.unlink(missing_ok=True)


# ─── Test 10: hmac_signing_secret ──────────────────────────────────


def test_10_hmac_signing(daemon_url, jwt_present, deepseek_key, mock_apis):
    SECRET = "supersecretwebhookvalueAAAAAAAA"
    deepseek_id = _setup_deepseek(deepseek_key)
    reset_credential("e2e_hmac_test")
    hmac_id = create_user_credential(
        name="e2e_hmac_test",
        provider_name="github_webhook",
        provider_type="hmac_signing_secret",
        scope="per_user",
        fields={"secret": SECRET, "algorithm": "sha256"},
    )
    yaml_path = _write_app_yaml(
        "e2e-cred-10-hmac", "e2e_hmac_test",
        "You verify webhook delivery. To check that the signing\n"
        "secret is correctly bound, send a GET request to\n"
        "http://127.0.0.1:9990/cred-echo.\n"
        "Then reply with whether the response.received_headers\n"
        "contains 'X-Vault-Hmac-Present'. Just yes or no.",
    )
    try:
        deploy_app(str(yaml_path), daemon=daemon_url)
        assert_credential_resolved("e2e-cred-10-hmac", "e2e_hmac_test", daemon=daemon_url)
        _reset_mock()
        conv = chat("e2e-cred-10-hmac", ["Verify HMAC binding."], daemon=daemon_url, timeout_per_turn=120)
        print("\n--- 10-hmac ---")
        print(conv.transcript)
        assert conv.turns[0].error is None
        # Mock should have received X-Vault-Hmac-Present header.
        received = _read_received()
        echo_calls = [r for r in received if r["path"] == "/cred-echo"]
        assert echo_calls, f"no echo calls: {received}"
    finally:
        undeploy_app("e2e-cred-10-hmac", daemon=daemon_url)
        delete_credential(hmac_id)
        delete_credential(deepseek_id)
        yaml_path.unlink(missing_ok=True)


# ─── Test 11: gcp_service_account ──────────────────────────────────


def test_11_gcp_service_account(daemon_url, jwt_present, deepseek_key, mock_apis):
    SA_JSON = (
        '{"type":"service_account",'
        '"project_id":"my-project",'
        '"private_key_id":"abc",'
        '"private_key":"-----BEGIN PRIVATE KEY-----\\nMIIEv\\n-----END PRIVATE KEY-----",'
        '"client_email":"sa@my-project.iam.gserviceaccount.com",'
        '"client_id":"1",'
        '"auth_uri":"https://accounts.google.com",'
        '"token_uri":"https://oauth2.googleapis.com/token"}'
    )
    deepseek_id = _setup_deepseek(deepseek_key)
    reset_credential("e2e_gcp_test")
    gcp_id = create_user_credential(
        name="e2e_gcp_test",
        provider_name="gcp",
        provider_type="gcp_service_account",
        scope="per_user",
        fields={"service_account_json": SA_JSON},
    )
    yaml_path = _write_app_yaml(
        "e2e-cred-11-gcp", "e2e_gcp_test",
        "You are a GCP token-exchange bot. POST to\n"
        "http://127.0.0.1:9990/gcp/token to get an access token.\n"
        "Reply with whether the response contains an access_token.\n"
        "Just yes or no.",
        actions=["get", "post"],
    )
    try:
        deploy_app(str(yaml_path), daemon=daemon_url)
        assert_credential_resolved("e2e-cred-11-gcp", "e2e_gcp_test", daemon=daemon_url)
        _reset_mock()
        conv = chat("e2e-cred-11-gcp", ["Exchange GCP token."], daemon=daemon_url, timeout_per_turn=120)
        print("\n--- 11-gcp ---")
        print(conv.transcript)
        assert conv.turns[0].error is None
        # Mock validates X-Gcp-Sa-Length header.
        received = _read_received()
        gcp_calls = [r for r in received if r["path"] == "/gcp/token"]
        assert gcp_calls, f"no gcp calls: {received}"
    finally:
        undeploy_app("e2e-cred-11-gcp", daemon=daemon_url)
        delete_credential(gcp_id)
        delete_credential(deepseek_id)
        yaml_path.unlink(missing_ok=True)


# ─── Test 12: azure_ad ──────────────────────────────────────────────


def test_12_azure_ad(daemon_url, jwt_present, deepseek_key, mock_apis):
    deepseek_id = _setup_deepseek(deepseek_key)
    reset_credential("e2e_azure_test")
    azure_id = create_user_credential(
        name="e2e_azure_test",
        provider_name="azure",
        provider_type="azure_ad",
        scope="per_user",
        fields={
            "tenant_id": "00000000-0000-0000-0000-000000000001",
            "client_id": "00000000-0000-0000-0000-000000000002",
            "client_secret": "azure-secret-AAAA",
        },
    )
    yaml_path = _write_app_yaml(
        "e2e-cred-12-azure", "e2e_azure_test",
        "You are an Azure auth bot. POST to\n"
        "http://127.0.0.1:9990/azure/token to get a token.\n"
        "Reply with whether you got a token. Just yes or no.",
        actions=["get", "post"],
    )
    try:
        deploy_app(str(yaml_path), daemon=daemon_url)
        assert_credential_resolved("e2e-cred-12-azure", "e2e_azure_test", daemon=daemon_url)
        _reset_mock()
        conv = chat("e2e-cred-12-azure", ["Get Azure token."], daemon=daemon_url, timeout_per_turn=120)
        print("\n--- 12-azure ---")
        print(conv.transcript)
        assert conv.turns[0].error is None
        received = _read_received()
        azure_calls = [r for r in received if r["path"] == "/azure/token"]
        assert azure_calls, f"no azure calls: {received}"
    finally:
        undeploy_app("e2e-cred-12-azure", daemon=daemon_url)
        delete_credential(azure_id)
        delete_credential(deepseek_id)
        yaml_path.unlink(missing_ok=True)


# ─── Test 13: file_upload ───────────────────────────────────────────


def test_13_file_upload(daemon_url, jwt_present, deepseek_key, mock_apis):
    deepseek_id = _setup_deepseek(deepseek_key)
    reset_credential("e2e_fileup_test")
    file_id = create_user_credential(
        name="e2e_fileup_test",
        provider_name="kubeconfig",
        provider_type="file_upload",
        scope="per_user",
        fields={
            "filename": "kubeconfig.yaml",
            "content": "YXBpVmVyc2lvbjogdjE=",
        },
    )
    yaml_path = _write_app_yaml(
        "e2e-cred-13-fileup", "e2e_fileup_test",
        "You verify file binding. POST a small body to\n"
        "http://127.0.0.1:9990/upload. Reply with the filename\n"
        "from the response.",
        actions=["get", "post"],
    )
    try:
        deploy_app(str(yaml_path), daemon=daemon_url)
        assert_credential_resolved("e2e-cred-13-fileup", "e2e_fileup_test", daemon=daemon_url)
        _reset_mock()
        conv = chat("e2e-cred-13-fileup", ["Upload smoke."], daemon=daemon_url, timeout_per_turn=120)
        print("\n--- 13-fileup ---")
        print(conv.transcript)
        assert conv.turns[0].error is None
        received = _read_received()
        upload_calls = [r for r in received if r["path"] == "/upload"]
        assert upload_calls, f"no upload calls: {received}"
    finally:
        undeploy_app("e2e-cred-13-fileup", daemon=daemon_url)
        delete_credential(file_id)
        delete_credential(deepseek_id)
        yaml_path.unlink(missing_ok=True)


# ─── Test 14: custom ────────────────────────────────────────────────


def test_18_ssh_key(daemon_url, jwt_present, deepseek_key, mock_apis):
    """ssh_key vault delivered to http module - verify via header echo."""
    deepseek_id = _setup_deepseek(deepseek_key)
    reset_credential("e2e_ssh_test")
    ssh_id = create_user_credential(
        name="e2e_ssh_test",
        provider_name="deploy_key",
        provider_type="ssh_key",
        scope="per_user",
        fields={
            "private_key": (
                "-----BEGIN OPENSSH PRIVATE KEY-----\n"
                "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123\n"
                "-----END OPENSSH PRIVATE KEY-----"
            ),
        },
    )
    yaml_path = _write_app_yaml(
        "e2e-cred-18-ssh", "e2e_ssh_test",
        "You verify SSH key delivery. GET\n"
        "http://127.0.0.1:9990/cred-echo. Reply with whether the\n"
        "received_headers contains 'X-Vault-Ssh-Key-Length'. yes/no.",
    )
    try:
        deploy_app(str(yaml_path), daemon=daemon_url)
        assert_credential_resolved("e2e-cred-18-ssh", "e2e_ssh_test", daemon=daemon_url)
        _reset_mock()
        conv = chat("e2e-cred-18-ssh", ["Verify ssh key delivery."], daemon=daemon_url, timeout_per_turn=120)
        print("\n--- 18-ssh ---")
        print(conv.transcript)
        assert conv.turns[0].error is None
        received = _read_received()
        echo = [r for r in received if r["path"] == "/cred-echo"]
        assert echo, f"no echo: {received}"
    finally:
        undeploy_app("e2e-cred-18-ssh", daemon=daemon_url)
        delete_credential(ssh_id)
        delete_credential(deepseek_id)
        yaml_path.unlink(missing_ok=True)


def test_19_client_certificate(daemon_url, jwt_present, deepseek_key, mock_apis):
    """client_certificate vault delivered - verify header echo."""
    deepseek_id = _setup_deepseek(deepseek_key)
    reset_credential("e2e_cert_test")
    cert_id = create_user_credential(
        name="e2e_cert_test",
        provider_name="mtls_client",
        provider_type="client_certificate",
        scope="per_user",
        fields={
            "certificate": "-----BEGIN CERTIFICATE-----\nMIIB\n-----END CERTIFICATE-----",
            "private_key": "-----BEGIN PRIVATE KEY-----\nMIIB\n-----END PRIVATE KEY-----",
        },
    )
    yaml_path = _write_app_yaml(
        "e2e-cred-19-cert", "e2e_cert_test",
        "You verify client cert delivery. GET\n"
        "http://127.0.0.1:9990/cred-echo. Reply with whether the\n"
        "received_headers contains 'X-Vault-Client-Cert-Length'. yes/no.",
    )
    try:
        deploy_app(str(yaml_path), daemon=daemon_url)
        assert_credential_resolved("e2e-cred-19-cert", "e2e_cert_test", daemon=daemon_url)
        _reset_mock()
        conv = chat("e2e-cred-19-cert", ["Verify cert delivery."], daemon=daemon_url, timeout_per_turn=120)
        print("\n--- 19-cert ---")
        print(conv.transcript)
        assert conv.turns[0].error is None
        received = _read_received()
        assert any(r["path"] == "/cred-echo" for r in received)
    finally:
        undeploy_app("e2e-cred-19-cert", daemon=daemon_url)
        delete_credential(cert_id)
        delete_credential(deepseek_id)
        yaml_path.unlink(missing_ok=True)


def test_20_mcp_server(daemon_url, jwt_present, deepseek_key, mock_apis):
    """mcp_server (stdio) vault delivered - verify header echo."""
    deepseek_id = _setup_deepseek(deepseek_key)
    reset_credential("e2e_mcp_stdio_test")
    mcp_id = create_user_credential(
        name="e2e_mcp_stdio_test",
        provider_name="mcp_filesystem",
        provider_type="mcp_server",
        scope="per_user",
        fields={
            "command": "npx",
            "args": '["-y","@modelcontextprotocol/server-filesystem","/tmp"]',
        },
    )
    yaml_path = _write_app_yaml(
        "e2e-cred-20-mcpstdio", "e2e_mcp_stdio_test",
        "You verify MCP stdio binding. GET\n"
        "http://127.0.0.1:9990/cred-echo. Reply with whether the\n"
        "received_headers contains 'X-Vault-Mcp-Command'. yes/no.",
    )
    try:
        deploy_app(str(yaml_path), daemon=daemon_url)
        assert_credential_resolved("e2e-cred-20-mcpstdio", "e2e_mcp_stdio_test", daemon=daemon_url)
        _reset_mock()
        conv = chat("e2e-cred-20-mcpstdio", ["Verify mcp stdio binding."], daemon=daemon_url, timeout_per_turn=120)
        print("\n--- 20-mcpstdio ---")
        print(conv.transcript)
        assert conv.turns[0].error is None
        received = _read_received()
        assert any(r["path"] == "/cred-echo" for r in received)
    finally:
        undeploy_app("e2e-cred-20-mcpstdio", daemon=daemon_url)
        delete_credential(mcp_id)
        delete_credential(deepseek_id)
        yaml_path.unlink(missing_ok=True)


def test_15_connection_string(daemon_url, jwt_present, deepseek_key, mock_apis):
    deepseek_id = _setup_deepseek(deepseek_key)
    reset_credential("e2e_pg_test")
    pg_id = create_user_credential(
        name="e2e_pg_test",
        provider_name="postgres",
        provider_type="connection_string",
        scope="per_user",
        fields={"url": "postgres://test:secret@db.local:5432/appdata"},
    )
    yaml_path = _write_app_yaml(
        "e2e-cred-15-pg", "e2e_pg_test",
        "You are a DB query bot. To list users, GET\n"
        "http://127.0.0.1:9990/db/users.\n"
        "Reply with the count of rows.",
    )
    try:
        deploy_app(str(yaml_path), daemon=daemon_url)
        assert_credential_resolved("e2e-cred-15-pg", "e2e_pg_test", daemon=daemon_url)
        _reset_mock()
        conv = chat("e2e-cred-15-pg", ["List db users."], daemon=daemon_url, timeout_per_turn=120)
        print("\n--- 15-pg ---")
        print(conv.transcript)
        assert conv.turns[0].error is None
        received = _read_received()
        db_calls = [r for r in received if r["path"] == "/db/users"]
        assert db_calls, f"no db calls: {received}"
    finally:
        undeploy_app("e2e-cred-15-pg", daemon=daemon_url)
        delete_credential(pg_id)
        delete_credential(deepseek_id)
        yaml_path.unlink(missing_ok=True)


def test_16_database_fields(daemon_url, jwt_present, deepseek_key, mock_apis):
    deepseek_id = _setup_deepseek(deepseek_key)
    reset_credential("e2e_dbfields_test")
    db_id = create_user_credential(
        name="e2e_dbfields_test",
        provider_name="postgres_split",
        provider_type="database_fields",
        scope="per_user",
        fields={
            "host": "db.example.com", "port": "5432",
            "user": "appuser", "password": "S3cure!",
            "database": "appdata",
        },
    )
    yaml_path = _write_app_yaml(
        "e2e-cred-16-dbf", "e2e_dbfields_test",
        "You are a DB inspector. Make a GET to\n"
        "http://127.0.0.1:9990/cred-echo. Reply with whether the\n"
        "received_headers includes 'X-Db-Host'. Just yes or no.",
    )
    try:
        deploy_app(str(yaml_path), daemon=daemon_url)
        assert_credential_resolved("e2e-cred-16-dbf", "e2e_dbfields_test", daemon=daemon_url)
        _reset_mock()
        conv = chat("e2e-cred-16-dbf", ["Verify host header."], daemon=daemon_url, timeout_per_turn=120)
        print("\n--- 16-dbf ---")
        print(conv.transcript)
        assert conv.turns[0].error is None
        received = _read_received()
        echo_calls = [r for r in received if r["path"] == "/cred-echo"]
        assert echo_calls, f"no echo calls: {received}"
    finally:
        undeploy_app("e2e-cred-16-dbf", daemon=daemon_url)
        delete_credential(db_id)
        delete_credential(deepseek_id)
        yaml_path.unlink(missing_ok=True)


def test_17_mcp_http(daemon_url, jwt_present, deepseek_key, mock_apis):
    deepseek_id = _setup_deepseek(deepseek_key)
    reset_credential("e2e_mcp_http_test")
    mcp_id = create_user_credential(
        name="e2e_mcp_http_test",
        provider_name="mcp_remote",
        provider_type="mcp_http",
        scope="per_user",
        fields={
            "url": "http://127.0.0.1:9990/cred-echo",
            "auth_mode": "bearer",
            "auth_token": "mcp-bearer-token-zzzzzzzz",
        },
    )
    yaml_path = _write_app_yaml(
        "e2e-cred-17-mcphttp", "e2e_mcp_http_test",
        "You are an MCP client. GET http://127.0.0.1:9990/cred-echo\n"
        "and reply with whether the response.received_headers contains\n"
        "an Authorization Bearer header. Just yes or no.",
    )
    try:
        deploy_app(str(yaml_path), daemon=daemon_url)
        assert_credential_resolved("e2e-cred-17-mcphttp", "e2e_mcp_http_test", daemon=daemon_url)
        _reset_mock()
        conv = chat("e2e-cred-17-mcphttp", ["Verify auth presence."], daemon=daemon_url, timeout_per_turn=120)
        print("\n--- 17-mcphttp ---")
        print(conv.transcript)
        assert conv.turns[0].error is None
        received = _read_received()
        echo_calls = [r for r in received if r["path"] == "/cred-echo"]
        assert echo_calls, f"no echo calls: {received}"
        # MCP token should arrive as Bearer.
        bearer = echo_calls[-1]["auth"]
        assert "mcp-bearer-token-zzzzzzzz" in bearer, (
            f"vault token not in auth: {bearer!r}"
        )
    finally:
        undeploy_app("e2e-cred-17-mcphttp", daemon=daemon_url)
        delete_credential(mcp_id)
        delete_credential(deepseek_id)
        yaml_path.unlink(missing_ok=True)


def test_14_custom(daemon_url, jwt_present, deepseek_key, mock_apis):
    deepseek_id = _setup_deepseek(deepseek_key)
    reset_credential("e2e_custom_test")
    custom_id = create_user_credential(
        name="e2e_custom_test",
        provider_name="anything_goes",
        provider_type="custom",
        scope="per_user",
        fields={"value": "arbitrary_payload_aaaaaaaaaaaaaaaa"},
    )
    yaml_path = _write_app_yaml(
        "e2e-cred-14-custom", "e2e_custom_test",
        "You verify custom binding. GET\n"
        "http://127.0.0.1:9990/custom. Reply 'yes' if the response\n"
        "received_value is non-empty, else 'no'.",
    )
    try:
        deploy_app(str(yaml_path), daemon=daemon_url)
        assert_credential_resolved("e2e-cred-14-custom", "e2e_custom_test", daemon=daemon_url)
        _reset_mock()
        conv = chat("e2e-cred-14-custom", ["Verify custom binding."], daemon=daemon_url, timeout_per_turn=120)
        print("\n--- 14-custom ---")
        print(conv.transcript)
        assert conv.turns[0].error is None
        received = _read_received()
        custom_calls = [r for r in received if r["path"] == "/custom"]
        assert custom_calls, f"no custom calls: {received}"
    finally:
        undeploy_app("e2e-cred-14-custom", daemon=daemon_url)
        delete_credential(custom_id)
        delete_credential(deepseek_id)
        yaml_path.unlink(missing_ok=True)
