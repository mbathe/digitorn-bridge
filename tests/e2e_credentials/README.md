# End-to-End Credential Tests

Tests every credential handler against the real daemon, with real
DeepSeek conversations + real HTTP consumer calls for handlers that
the agent uses through the `http` module.

**41/41 tests pass — all 19 handler types covered end-to-end.**

## How to run

```bash
# 1. Start the daemon (in another terminal)
DIGITORN_MASTER_KEY='<32-byte b64url>' \
  py -3.12 -m digitorn.core.server start --port 8765

# 2. Make sure you have a fresh user JWT in ~/.digitorn/credentials.json
# (login against your auth service, e.g.
#  curl -X POST https://auth.digitorn.ai/auth/login \
#    -H 'Content-Type: application/json' \
#    -d '{"username":"<user>","password":"<pass>"}' \
#    > ~/.digitorn/credentials.json)

# 3. Set DEEPSEEK_API_KEY in .env at repo root, OR
#    DIGITORN_DEEPSEEK_KEY in env

# 4. Run all tests
py -3.12 -m pytest tests/e2e_credentials -v
```

The mock external services (Stripe / GitHub / AWS / GCP / Azure / DB
/ HMAC / file upload / cred-echo) are spun up automatically by the
pytest fixtures on port 9990. The mock SSH server is on 9989.

## Test layout

```text
tests/e2e_credentials/
├── README.md
├── conftest.py                       ← daemon + jwt + mock-server fixtures
├── apps/                             ← real YAML apps deployed to the daemon
│   ├── 01_chatbot_api_key.yaml
│   ├── 02_chatbot_bearer_token.yaml
│   ├── 03_chatbot_oauth2.yaml
│   ├── 04_chatbot_oauth2_pkce.yaml
│   ├── 05_chatbot_device_code.yaml
│   ├── 06_stripe_assistant.yaml
│   ├── 07_github_assistant.yaml
│   ├── 08_basic_auth_proxy.yaml
│   └── 09_aws_inventory.yaml
├── mocks/                            ← in-process mock servers
│   ├── external_apis.py              ← aiohttp on :9990 (12+ endpoints)
│   └── ssh_server.py                 ← paramiko on :9989
├── scenarios/                        ← pytest scenarios
│   ├── test_01_chatbot_api_key.py             (real chat)
│   ├── test_02_to_05_llm_bindable.py          (4 real chats, parametrized)
│   ├── test_06_stripe_payment_assistant.py    (real chat, real http call)
│   ├── test_07_github_and_08_basic_auth.py    (real chat, real http call)
│   ├── test_09_remaining_handlers.py          (12 real chats, real http calls)
│   └── test_all_handlers_inject.py            (20 vault round-trips)
└── shared/                           ← reusable test helpers
    ├── chat_helpers.py               ← multi-turn chat driver
    ├── deploy_helpers.py             ← deploy + manifest assertions
    ├── vault_helpers.py              ← vault create/delete/fetch
    └── runtime_peek.py               ← introspect deployed config
```

## What gets tested

### Tier A — Real agent consuming the credential (21 tests)

The agent's brain (LLM-bindable handlers) or http module (everything
else) is bound to a vault credential. Every test runs a real 2-3
turn conversation against `api.deepseek.com` and asserts the
credential reached the wire.

#### LLM brain (5 handlers, real DeepSeek conversation)

| Handler        | Field consumed | Test                              |
|----------------|----------------|-----------------------------------|
| `api_key`      | `api_key`      | `test_01_chatbot_api_key.py`      |
| `bearer_token` | `token`        | `test_02_to_05_llm_bindable.py`   |
| `oauth2`       | `access_token` | `test_02_to_05_llm_bindable.py`   |
| `oauth2_pkce`  | `access_token` | `test_02_to_05_llm_bindable.py`   |
| `device_code`  | `access_token` | `test_02_to_05_llm_bindable.py`   |

The LLM does math + recalls a fact across turns. Total: 5 chats.

#### http module (14 handlers, real HTTP consumer call)

The `http` module declares `credential_slots` covering all 14
remaining handlers. The deployed YAML binds the slot, the agent runs
a real chat, calls `http.get` / `http.post`, and the mock server
asserts the vault-injected auth header arrived.

| Handler               | Header / proof                              | Test                                    |
|-----------------------|---------------------------------------------|-----------------------------------------|
| `multi_field`         | Stripe `Authorization: Bearer sk_test_...`  | `test_06_stripe_payment_assistant.py`   |
| `bearer_token` (PAT)  | GitHub `Authorization: Bearer ghp_...`      | `test_07_github_and_08_basic_auth.py`   |
| `basic_auth`          | `Authorization: Basic <b64>`                | `test_07_github_and_08_basic_auth.py`   |
| `aws_access_key`      | `X-Aws-Access-Key-Id`, `X-Aws-Region`       | `test_09_remaining_handlers.py`         |
| `gcp_service_account` | `X-Gcp-Sa-Length`                           | `test_09_remaining_handlers.py`         |
| `azure_ad`            | `X-Azure-Tenant-Id`, `X-Azure-Client-Id`    | `test_09_remaining_handlers.py`         |
| `hmac_signing_secret` | `X-Vault-Hmac-Present`                      | `test_09_remaining_handlers.py`         |
| `connection_string`   | `X-Postgres-Url` / `Authorization: Bearer`  | `test_09_remaining_handlers.py`         |
| `database_fields`     | `X-Db-Host`, `X-Db-Name`                    | `test_09_remaining_handlers.py`         |
| `ssh_key`             | `X-Vault-Ssh-Key-Length`                    | `test_09_remaining_handlers.py`         |
| `client_certificate`  | `X-Vault-Client-Cert-Length`                | `test_09_remaining_handlers.py`         |
| `mcp_server`          | `X-Vault-Mcp-Command`                       | `test_09_remaining_handlers.py`         |
| `mcp_http`            | `Authorization: Bearer <token>`             | `test_09_remaining_handlers.py`         |
| `file_upload`         | `X-Vault-File-Name`                         | `test_09_remaining_handlers.py`         |
| `custom`              | `X-Vault-Custom`                            | `test_09_remaining_handlers.py`         |

Each scenario:

1. Creates a vault credential (POST `/api/credentials`).
2. Deploys the YAML app referencing it (POST `/api/apps/deploy`).
3. Asserts the manifest reports `resolved: true` for the slot.
4. Drives a real DeepSeek chat through the daemon.
5. Asserts the agent called the right URL AND the mock server
   recorded the vault-injected auth header.
6. Cleans up.

### Tier B — Vault round-trip + masking (20 cases)

Confirms the encryption / storage / masking / leak-prevention
pipeline works for every handler. Same 19 handlers as above,
exercised in `scenarios/test_all_handlers_inject.py`. `hmac_signing_secret`
is tested twice (github_webhook + stripe_webhook providers).

| Handler               | Provider used in test |
|-----------------------|-----------------------|
| `api_key`             | deepseek              |
| `bearer_token`        | github_pat            |
| `basic_auth`          | internal_proxy        |
| `oauth2`              | google_oauth          |
| `oauth2_pkce`         | github_oauth_pkce     |
| `device_code`         | tv_app                |
| `multi_field`         | stripe                |
| `connection_string`   | postgres_main         |
| `aws_access_key`      | aws_main              |
| `gcp_service_account` | gcp_workload          |
| `azure_ad`            | azure_app             |
| `ssh_key`             | deploy_key            |
| `client_certificate`  | mtls_client           |
| `hmac_signing_secret` | github_webhook        |
| `hmac_signing_secret` | stripe_webhook        |
| `database_fields`     | postgres_split        |
| `mcp_server`          | mcp_local             |
| `mcp_http`            | mcp_remote            |
| `file_upload`         | kubeconfig            |
| `custom`              | anything_goes         |

Each case:
1. POST `/api/credentials` with provider_type=X and realistic fields.
2. GET `/api/credentials/{id}` to verify storage + masking.
3. Assert `name` matches.
4. Assert `provider_type` matches.
5. Assert masked preview hides plaintext.
6. DELETE the credential.

## Coverage matrix (final)

| Handler               | Tier A (chat + consumer) | Tier B (vault) | Consumer module    |
|-----------------------|:------------------------:|:--------------:|:-------------------|
| `api_key`             | ✅                       | ✅             | LLM brain          |
| `bearer_token`        | ✅ ×2                    | ✅             | LLM brain + http   |
| `oauth2`              | ✅                       | ✅             | LLM brain          |
| `oauth2_pkce`         | ✅                       | ✅             | LLM brain          |
| `device_code`         | ✅                       | ✅             | LLM brain          |
| `basic_auth`          | ✅                       | ✅             | http               |
| `multi_field`         | ✅                       | ✅             | http (Stripe)      |
| `connection_string`   | ✅                       | ✅             | http               |
| `aws_access_key`      | ✅                       | ✅             | http               |
| `gcp_service_account` | ✅                       | ✅             | http               |
| `azure_ad`            | ✅                       | ✅             | http               |
| `ssh_key`             | ✅                       | ✅             | http (header echo) |
| `client_certificate`  | ✅                       | ✅             | http (header echo) |
| `hmac_signing_secret` | ✅                       | ✅ ×2          | http               |
| `database_fields`     | ✅                       | ✅             | http               |
| `mcp_server`          | ✅                       | ✅             | http (header echo) |
| `mcp_http`            | ✅                       | ✅             | http               |
| `file_upload`         | ✅                       | ✅             | http               |
| `custom`              | ✅                       | ✅             | http               |

**41 tests pass total.** All 19 handlers exercised both at the vault
layer (encryption/masking/API round-trip) and through a real LLM agent
that drives a real consumer call (LLM brain or HTTP module).

**Tier A** = real LLM, real conversation, real consumer (real DeepSeek
or real http module hitting a mock external API). The credential is
not just stored but actively delivered to the wire.

**Tier B** = vault encryption / masking / API round-trip is verified.

For handlers that have no natural HTTP-Authorization scheme (`ssh_key`,
`client_certificate`, `mcp_server`), the http module's `_default_auth`
exposes proof headers (`X-Vault-Ssh-Key-Length`, etc.) so the test
can assert the vault delivered the secret value into the module config.

## Test runtime

| Test                         | Real chat? | Approx. duration |
|------------------------------|-----------|------------------|
| test_01_chatbot_api_key      | ✅         | ~25s             |
| test_02_to_05_llm_bindable   | ✅ (×4)    | ~1m 30s          |
| test_06_stripe_payment       | ✅         | ~25s             |
| test_07_github_and_08_basic  | ✅ (×2)    | ~50s             |
| test_09_remaining_handlers   | ✅ (×12)   | ~5m              |
| test_all_handlers_inject     | (vault)    | ~10s             |
| **TOTAL**                    |            | **~8m 11s**      |

Total: **41 tests pass** (20 vault + 21 real-agent), covering all 19
handler types end-to-end through the vault encryption + manifest +
runtime injection pipeline.
