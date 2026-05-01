"""Scenario 01 - api_key handler, real LLM, multi-turn conversation.

Validates:
  * Vault credential created via the store (per_user, name=e2e_deepseek_test).
  * Compiled brain references it via `credential:` block.
  * Manifest endpoint resolves the ref to the vault entry.
  * Session-time injector hot-swaps the live provider's api_key.
  * The agent replies with REAL DeepSeek output (not a stub) across
    multiple turns - including memory of prior context.

This proves: api_key handler works end-to-end with a true conversation.

Skipped when no `DIGITORN_DEEPSEEK_KEY` env var is set (we need a
real key to talk to deepseek.com).
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
    create_user_credential, delete_credential, fetch_existing_field,
    reset_credential,
)


APP_YAML = Path(__file__).resolve().parents[1] / "apps" / "01_chatbot_api_key.yaml"
APP_ID = "e2e-cred-01-chatbot"
CRED_NAME = "e2e_deepseek_test"


@pytest.fixture
def deepseek_key() -> str:
    """Real DeepSeek api_key. Resolution order:
      1. ``DIGITORN_DEEPSEEK_KEY`` env var (explicit override).
      2. ``DEEPSEEK_API_KEY`` from the project ``.env`` file.
      3. The system_wide vault credential `provider_name=deepseek`.
    Skips when none is available."""
    key = os.environ.get("DIGITORN_DEEPSEEK_KEY", "").strip()
    if not key:
        # Read .env at the repo root.
        repo_root = Path(__file__).resolve().parents[3]
        env_file = repo_root / ".env"
        if env_file.is_file():
            for line in env_file.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                if k.strip() == "DEEPSEEK_API_KEY":
                    key = v.strip().strip('"').strip("'")
                    break
    if not key:
        key = fetch_existing_field(
            provider_name="deepseek", field="api_key", scope="system_wide",
        ) or ""
    if not key:
        pytest.skip(
            "no real DeepSeek api_key available - "
            "set DIGITORN_DEEPSEEK_KEY env var OR add DEEPSEEK_API_KEY "
            "to .env, OR add a system_wide deepseek credential.",
        )
    return key


@pytest.fixture
def vault_credential(daemon_url, jwt_present, deepseek_key):
    """Create the per_user vault credential the YAML references,
    yield its id, drop it after the test."""
    reset_credential(CRED_NAME)
    cred_id = create_user_credential(
        name=CRED_NAME,
        provider_name="deepseek",
        provider_type="api_key",
        scope="per_user",
        fields={
            "api_key": deepseek_key,
            "base_url": "https://api.deepseek.com/v1",
        },
    )
    yield cred_id
    delete_credential(cred_id)


@pytest.fixture
def deployed_app(daemon_url, vault_credential):
    """Deploy the YAML and assert credential resolves."""
    deploy_app(str(APP_YAML), daemon=daemon_url)
    entry = assert_credential_resolved(APP_ID, CRED_NAME, daemon=daemon_url)
    assert entry["resolved_status"] in ("filled", "valid"), entry
    yield APP_ID
    undeploy_app(APP_ID, daemon=daemon_url)


def test_api_key_real_chat_multi_turn(deployed_app, daemon_url):
    """Real multi-turn conversation. The agent must answer correctly
    across 3 turns, including recalling info from turn 1 in turn 3."""
    conv = chat(
        deployed_app,
        [
            "My favourite color is teal. Just acknowledge.",
            "What is 17 multiplied by 23? Reply with just the number.",
            "What was my favourite colour again? One word answer.",
        ],
        daemon=daemon_url,
        timeout_per_turn=120,
    )

    print("\n=== Conversation transcript ===")
    print(conv.transcript)
    print("=" * 50)

    # Assertions on the real responses.
    assert len(conv.turns) == 3, f"expected 3 turns, got {len(conv.turns)}"
    for i, t in enumerate(conv.turns):
        assert t.error is None, f"turn {i+1} errored: {t.error}"
        assert t.assistant_text, f"turn {i+1} got empty response"

    # Turn 2: math.
    answer_2 = conv.turns[1].assistant_text.strip()
    assert "391" in answer_2, (
        f"turn 2 expected '391' (17*23), got: {answer_2!r}"
    )

    # Turn 3: memory recall.
    answer_3 = conv.turns[2].assistant_text.lower()
    assert "teal" in answer_3, (
        f"turn 3 expected memory recall of 'teal', got: {answer_3!r}"
    )
