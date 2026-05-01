"""Scenario 02 - bearer_token handler bound to LLM brain via the
extended `llm_provider` slot inject map (token -> config.api_key).

Validates:
  * The `bearer_token` handler can be used for LLM provider auth -
    same wire format as api_key but different vault metadata.
  * Real multi-turn chat against DeepSeek using the bearer-typed
    credential.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from shared.chat_helpers import chat
from shared.deploy_helpers import (
    assert_credential_resolved, deploy_app, undeploy_app,
)
from shared.vault_helpers import (
    create_user_credential, delete_credential, reset_credential,
)


APP_YAML = (
    Path(__file__).resolve().parents[1]
    / "apps" / "02_chatbot_bearer_token.yaml"
)
APP_ID = "e2e-cred-02-bearer"
CRED_NAME = "e2e_deepseek_bearer"


@pytest.fixture
def deepseek_key() -> str:
    key = os.environ.get("DIGITORN_DEEPSEEK_KEY", "").strip()
    if not key:
        repo_root = Path(__file__).resolve().parents[3]
        env_file = repo_root / ".env"
        if env_file.is_file():
            for line in env_file.read_text(encoding="utf-8").splitlines():
                if line.startswith("DEEPSEEK_API_KEY="):
                    key = line.split("=", 1)[1].strip().strip('"').strip("'")
                    break
    if not key:
        pytest.skip("no real DeepSeek key for bearer_token chat test")
    return key


@pytest.fixture
def vault_credential(daemon_url, deepseek_key):
    """Create a bearer_token credential with the DeepSeek key in the
    `token` field (instead of `api_key`)."""
    reset_credential(CRED_NAME)
    cred_id = create_user_credential(
        name=CRED_NAME,
        provider_name="deepseek",
        provider_type="bearer_token",
        scope="per_user",
        fields={"token": deepseek_key},
    )
    yield cred_id
    delete_credential(cred_id)


@pytest.fixture
def deployed_app(daemon_url, vault_credential):
    deploy_app(str(APP_YAML), daemon=daemon_url)
    entry = assert_credential_resolved(APP_ID, CRED_NAME, daemon=daemon_url)
    assert entry["resolved_status"] in ("filled", "valid")
    yield APP_ID
    undeploy_app(APP_ID, daemon=daemon_url)


def test_bearer_token_real_chat(deployed_app, daemon_url):
    """Real chat: 2 turns with memory recall."""
    conv = chat(
        deployed_app,
        [
            "Remember the codeword 'aurora'.",
            "What was the codeword? One word.",
        ],
        daemon=daemon_url,
        timeout_per_turn=120,
    )

    print("\n=== Conversation ===")
    print(conv.transcript)
    print("=" * 50)

    assert len(conv.turns) == 2
    for i, t in enumerate(conv.turns):
        assert t.error is None, f"turn {i+1}: {t.error}"
        assert t.assistant_text, f"turn {i+1}: empty"

    # Turn 2: recall.
    answer = conv.turns[1].assistant_text.lower()
    assert "aurora" in answer, (
        f"expected 'aurora' in turn 2, got: {answer!r}"
    )
