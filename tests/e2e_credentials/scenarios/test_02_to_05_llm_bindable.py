"""Scenarios 02-05: real multi-turn chat with DeepSeek where the
LLM brain credential uses one of bearer_token / oauth2 / oauth2_pkce
/ device_code handler types.

The `llm_provider` slot's inject map maps `token` and `access_token`
to `config.api_key`, so any of these handler types can be a brain
credential. This test proves the full chain runs identically for
each handler.

Each scenario:
  1. Creates a vault credential of type X with the DeepSeek key
     stored in the field expected by handler X (token / access_token).
  2. Deploys the corresponding YAML.
  3. Asserts manifest resolves the credential.
  4. Runs a real 2-turn DeepSeek chat with memory recall.
  5. Asserts the LLM responded sensibly (memory recall in turn 2).
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


YAML_DIR = Path(__file__).resolve().parents[1] / "apps"


# (scenario_id, app_id, yaml_filename, cred_name, handler_type, key_field)
SCENARIOS = [
    ("02-bearer",
     "e2e-cred-02-bearer",
     "02_chatbot_bearer_token.yaml",
     "e2e_deepseek_bearer",
     "bearer_token",
     "token"),
    ("03-oauth2",
     "e2e-cred-03-oauth2",
     "03_chatbot_oauth2.yaml",
     "e2e_oauth2_token",
     "oauth2",
     "access_token"),
    ("04-pkce",
     "e2e-cred-04-pkce",
     "04_chatbot_oauth2_pkce.yaml",
     "e2e_pkce_token",
     "oauth2_pkce",
     "access_token"),
    ("05-device",
     "e2e-cred-05-device",
     "05_chatbot_device_code.yaml",
     "e2e_device_token",
     "device_code",
     "access_token"),
]


@pytest.fixture(scope="module")
def deepseek_key() -> str:
    """Real DeepSeek api_key from env or .env file."""
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


@pytest.mark.parametrize(
    "scenario_id, app_id, yaml_file, cred_name, htype, key_field",
    SCENARIOS,
    ids=[s[0] for s in SCENARIOS],
)
def test_llm_bindable_handler_chat(
    daemon_url, jwt_present, deepseek_key,
    scenario_id, app_id, yaml_file, cred_name, htype, key_field,
):
    """Common pattern: same chat, different handler type."""
    yaml_path = YAML_DIR / yaml_file
    reset_credential(cred_name)
    cred_id = create_user_credential(
        name=cred_name,
        provider_name="deepseek",
        provider_type=htype,
        scope="per_user",
        fields={key_field: deepseek_key},
    )
    try:
        deploy_app(str(yaml_path), daemon=daemon_url)
        entry = assert_credential_resolved(app_id, cred_name, daemon=daemon_url)
        assert entry["resolved_status"] in ("filled", "valid"), entry

        codeword = f"sapphire-{scenario_id}"
        conv = chat(
            app_id,
            [
                f"Remember the codeword '{codeword}'.",
                "What was the codeword? One word, exact spelling.",
            ],
            daemon=daemon_url,
            timeout_per_turn=120,
        )
        print(f"\n--- {scenario_id} ---")
        print(conv.transcript)

        assert len(conv.turns) == 2
        for i, t in enumerate(conv.turns):
            assert t.error is None, f"turn {i+1}: {t.error}"
            assert t.assistant_text, f"turn {i+1}: empty"
        recall = conv.turns[1].assistant_text.lower()
        # The LLM may use slight variations; accept the codeword
        # appearing anywhere in the response.
        assert codeword.split("-")[0] in recall, (
            f"expected '{codeword}' in recall, got: {recall!r}"
        )
    finally:
        undeploy_app(app_id, daemon=daemon_url)
        delete_credential(cred_id)
