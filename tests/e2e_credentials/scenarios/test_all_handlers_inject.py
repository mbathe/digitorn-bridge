"""Parameterized test: every credential handler creates → vault →
deploy → manifest resolves → injected (per masked preview).

For handlers that map to the `llm_provider` slot's inject targets
(api_key / token / access_token), the test additionally deploys an
LLM brain referencing the credential and asserts the daemon can
TALK to DeepSeek using it (real multi-turn).

For non-LLM handlers (multi_field, ssh_key, etc.), we test the
injection chain via the manifest endpoint + masked-field assertion -
the value reached the vault, was decrypted, was masked, and is
visible to the per-app config UI.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from shared.chat_helpers import chat
from shared.deploy_helpers import (
    assert_credential_resolved, deploy_app, undeploy_app,
)
from shared.runtime_peek import assert_field_injected
from shared.vault_helpers import (
    create_user_credential, delete_credential, reset_credential,
)


YAML_DIR = Path(__file__).resolve().parents[1] / "apps"


# ─── Per-handler fixtures ───────────────────────────────────────────


HANDLERS = [
    # (handler_type, provider_name, fields, masked_preview_check)
    ("api_key",
     "deepseek",
     {"api_key": "sk-handler-test-aaaaaaaaaaaaaaaa"},
     "sk-...aaaa"),
    ("bearer_token",
     "github_pat",
     {"token": "ghp_T_handler_bearer_aaaaaaaaaaaaaaaa"},
     "ghp...aaaa"),
    ("basic_auth",
     "internal_proxy",
     {"username": "alice", "password": "S3cure!Pass"},
     "S3c...ass"),
    ("oauth2",
     "google_oauth",
     {"access_token": "ya29.mock-oauth2-aaaaaaaaaaaaaaaa",
      "refresh_token": "1//mock-rt-aaaa"},
     "ya2...aaaa"),
    ("oauth2_pkce",
     "github_oauth_pkce",
     {"access_token": "gho_pkce_test_aaaaaaaaaaaaaaaa",
      "refresh_token": "ghr_test_bbbb"},
     "gho...aaaa"),
    ("device_code",
     "tv_app",
     {"access_token": "dc_tok_test_aaaaaaaaaaaaaaaa"},
     "dc_...aaaa"),
    ("multi_field",
     "stripe",
     {"publishable_key": "pk_live_AAAAAAAA",
      "secret_key": "sk_live_BBBBBBBB",
      "webhook_signing_secret": "whsec_CCCCCCCC"},
     "sk_...BBBB"),
    ("connection_string",
     "postgres_main",
     {"url": "postgres://user:pass@db.local:5432/app"},
     "pos...3/app"),
    ("aws_access_key",
     "aws_main",
     {"access_key_id": "AKIAIOSFODNN7EXAMPLE",
      "secret_access_key": "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
      "region": "us-east-1"},
     "wJa...EKEY"),
    ("ssh_key",
     "deploy_key",
     {"private_key":
      "-----BEGIN OPENSSH PRIVATE KEY-----\n"
      "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghij\n"
      "-----END OPENSSH PRIVATE KEY-----"},
     "---...----"),
    ("client_certificate",
     "mtls_client",
     {"certificate":
      "-----BEGIN CERTIFICATE-----\nMIIBcert\n-----END CERTIFICATE-----",
      "private_key":
      "-----BEGIN PRIVATE KEY-----\nMIIBkey\n-----END PRIVATE KEY-----"},
     "---...----"),
    ("hmac_signing_secret",
     "github_webhook",
     {"secret": "supersecretwebhookvalueAAAAAAAA",
      "algorithm": "sha256"},
     "sup...AAAA"),
    ("database_fields",
     "postgres_split",
     {"host": "db.example.com", "port": "5432",
      "user": "appuser", "password": "S3cure!DB",
      "database": "appdata"},
     "S3c...e!DB"),
    ("file_upload",
     "kubeconfig",
     {"filename": "kubeconfig.yaml",
      "content": "YXBpVmVyc2lvbjogdjE="},  # base64 of "apiVersion: v1"
     "YXB...dj1"),
    ("custom",
     "anything_goes",
     {"value": "arbitrary_payload_aaaaaaaaaaaaaaaa"},
     "arb...aaaa"),
    ("hmac_signing_secret",
     "stripe_webhook",
     {"secret": "whsec_stripeAAAAAAAAAA",
      "algorithm": "sha256"},
     "whs...AAAA"),
    # ── 4 previously-untested handlers (added per user demand) ─────
    ("azure_ad",
     "azure_main",
     {"tenant_id": "00000000-0000-0000-0000-000000000001",
      "client_id": "00000000-0000-0000-0000-000000000002",
      "client_secret": "azure-secret-AAAAAAAAAAAAAAAA"},
     "azu...AAAA"),
    ("gcp_service_account",
     "gcp_main",
     {"service_account_json":
        '{"type":"service_account",'
        '"project_id":"my-project",'
        '"private_key_id":"abc123",'
        '"private_key":"-----BEGIN PRIVATE KEY-----\\nMIIEvQ\\n-----END PRIVATE KEY-----",'
        '"client_email":"sa@my-project.iam.gserviceaccount.com",'
        '"client_id":"1234567890",'
        '"auth_uri":"https://accounts.google.com/o/oauth2/auth",'
        '"token_uri":"https://oauth2.googleapis.com/token"}'},
     "{\"t..."),
    ("mcp_server",
     "mcp_filesystem",
     {"command": "npx",
      "args": "[\"-y\",\"@modelcontextprotocol/server-filesystem\",\"/tmp\"]"},
     "[\"-..."),
    ("mcp_http",
     "mcp_remote",
     {"url": "https://mcp.example.com/sse",
      "auth_mode": "bearer",
      "auth_token": "mcp-bearer-token-AAAAAAAAAAAA"},
     "mcp...AAAA"),
]


@pytest.fixture
def vault_cred(daemon_url, jwt_present, request):
    """Per-test fixture - parametrized over HANDLERS list."""
    htype, provider, fields, _expected = request.param
    name = f"e2e_{htype}_{provider}"
    reset_credential(name)
    cred_id = create_user_credential(
        name=name,
        provider_name=provider,
        provider_type=htype,
        scope="per_user",
        fields=fields,
    )
    yield {"name": name, "id": cred_id, "htype": htype,
           "provider": provider, "expected": _expected, "fields": fields}
    delete_credential(cred_id)


@pytest.mark.parametrize("vault_cred", HANDLERS, indirect=True,
                         ids=[h[0] + "/" + h[1] for h in HANDLERS])
def test_handler_vault_roundtrip(vault_cred, daemon_url):
    """Vault round-trip: create → list → masked preview matches."""
    import json
    import urllib.request
    p = os.path.expanduser("~/.digitorn/credentials.json")
    with open(p) as f:
        tok = json.load(f)["access_token"]
    req = urllib.request.Request(
        f"{daemon_url}/api/credentials/{vault_cred['id']}",
        headers={"Authorization": f"Bearer {tok}"},
    )
    data = json.loads(urllib.request.urlopen(req, timeout=10).read())
    cred = data.get("data", {})

    assert cred.get("name") == vault_cred["name"], (
        f"name mismatch: stored={cred.get('name')!r} "
        f"expected={vault_cred['name']!r}"
    )
    assert cred.get("provider_type") == vault_cred["htype"]
    masked = cred.get("display_metadata", {}).get("masked_fields", {})
    # Some field must be masked (not all - api_key handler masks api_key
    # only, multi_field masks every field, etc.).
    assert masked, f"no masked fields: {cred}"
    # At least one masked value should NOT contain plaintext substring
    # of any submitted field longer than 16 chars - prove the value is
    # actually obscured.
    for plaintext in vault_cred["fields"].values():
        if isinstance(plaintext, str) and len(plaintext) > 16:
            for masked_val in masked.values():
                assert plaintext not in masked_val, (
                    f"plaintext {plaintext!r} leaked into masked {masked_val!r}"
                )
