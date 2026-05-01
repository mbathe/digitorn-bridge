"""Scenario 06 - Stripe payment assistant.

The agent has http module + a vault credential of type `multi_field`
(Stripe-style). The http module's new credential_slot auto-injects
`secret_key` -> `Authorization: Bearer <secret_key>` on every outgoing
HTTP call. The agent has no idea what the key is.

Validates:
  * `multi_field` handler injects via the http module's slot.
  * The agent uses the http tool to call a (mock) Stripe API.
  * The mock confirms the auth header carried our vault secret.
  * Multi-turn flow: list customers, then create a charge.
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
APP_YAML = HERE / "apps" / "06_stripe_payment_assistant.yaml"
APP_ID = "e2e-cred-06-stripe"
STRIPE_CRED = "e2e_stripe_test"
DEEPSEEK_CRED = "e2e_deepseek_test"
MOCK_PORT = 9990
SECRET_KEY = "sk_test_LIVE_MOCK_SECRET_aaaaaaaaaaaaa"


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
    """Start the external_apis mock server (Stripe + GitHub + ...)."""
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
    yield f"http://127.0.0.1:{MOCK_PORT}"
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


@pytest.fixture
def vault_setup(daemon_url, jwt_present, deepseek_key, mock_apis):
    """Create both credentials: deepseek brain + stripe http."""
    reset_credential(DEEPSEEK_CRED)
    reset_credential(STRIPE_CRED)
    deepseek_id = create_user_credential(
        name=DEEPSEEK_CRED,
        provider_name="deepseek",
        provider_type="api_key",
        scope="per_user",
        fields={"api_key": deepseek_key},
    )
    stripe_id = create_user_credential(
        name=STRIPE_CRED,
        provider_name="stripe",
        provider_type="multi_field",
        scope="per_user",
        fields={
            "publishable_key": "pk_test_PUBLIC_AAAAAA",
            "secret_key": SECRET_KEY,
            "webhook_signing_secret": "whsec_TEST_BBBBBB",
        },
    )
    yield {"deepseek_id": deepseek_id, "stripe_id": stripe_id}
    delete_credential(deepseek_id)
    delete_credential(stripe_id)


@pytest.fixture
def deployed_app(vault_setup, daemon_url):
    deploy_app(str(APP_YAML), daemon=daemon_url)
    # Both credentials should resolve.
    e1 = assert_credential_resolved(APP_ID, DEEPSEEK_CRED, daemon=daemon_url)
    e2 = assert_credential_resolved(APP_ID, STRIPE_CRED, daemon=daemon_url)
    print(f"   deepseek manifest entry resolved: {e1.get('block')}")
    print(f"   stripe   manifest entry resolved: {e2.get('block')}")
    yield APP_ID
    undeploy_app(APP_ID, daemon=daemon_url)


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


def test_stripe_agent_real_chat(deployed_app, daemon_url):
    """Agent uses http with vault-injected Stripe secret to list +
    charge customers. Mock asserts the right auth header arrived."""
    _reset_mock()
    conv = chat(
        deployed_app,
        [
            "How many customers do I have on Stripe? List their names.",
            "Create a $42.50 USD charge on customer cus_002 with description 'test charge'.",
        ],
        daemon=daemon_url,
        timeout_per_turn=180,
    )

    print("\n=== Stripe agent transcript ===")
    print(conv.transcript)
    print("=" * 50)

    # Assertions on the agent's behaviour.
    assert len(conv.turns) == 2
    for i, t in enumerate(conv.turns):
        assert t.error is None, f"turn {i+1}: {t.error}"
        assert t.assistant_text, f"turn {i+1}: empty"

    # Turn 1: agent must have listed customers; reply mentions Alice/Bob/Charlie or count=3.
    reply_1 = conv.turns[0].assistant_text.lower()
    has_count = "3" in reply_1 or "three" in reply_1
    has_name = any(n in reply_1 for n in ("alice", "bob", "charlie"))
    assert has_count or has_name, (
        f"turn 1 didn't list customers: {reply_1!r}"
    )

    # The mock should have received GET /stripe/v1/customers with
    # the vault secret_key as Bearer.
    received = _read_received()
    customer_calls = [r for r in received if "/stripe/v1/customers" in r["path"]]
    assert customer_calls, (
        f"mock didn't receive /stripe/v1/customers call. received={received}"
    )
    auth_seen = customer_calls[0]["auth"]
    assert auth_seen.startswith("Bearer "), (
        f"expected Bearer auth on Stripe call, got: {auth_seen!r}"
    )
    assert SECRET_KEY in auth_seen, (
        f"vault secret_key not in auth header. Got: {auth_seen!r}"
    )

    # Turn 2: agent should have called POST /stripe/v1/charges.
    charge_calls = [r for r in received if r["path"] == "/stripe/v1/charges"]
    assert charge_calls, (
        f"mock didn't receive POST /stripe/v1/charges. received={received}"
    )
    assert charge_calls[0]["method"] == "POST"
    # The same secret_key should be on the POST too.
    assert SECRET_KEY in charge_calls[0]["auth"], (
        f"vault secret_key not on POST charge. Got: {charge_calls[0]}"
    )
