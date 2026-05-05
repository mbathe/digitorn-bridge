# Digitorn LLM Gateway

OpenAI-compatible HTTP service that fronts every LLM call coming from
a digitorn user authenticated with their digitorn account.

## What it does

- Validates a JWT issued by `digitorn-auth`.
- Resolves a user-facing model alias (`digitorn-pro`, `digitorn-fast`, ...)
  to a real provider/model id.
- Delegates the actual provider call to **LiteLLM as a library** (not as
  the LiteLLM proxy). 100+ providers, streaming, tool calls, cost calc.
- Routes models flagged `provider: custom` to a pluggable
  `CustomRouter` instead of LiteLLM. The gateway is therefore not
  bound to LiteLLM's coverage - in-house finetunes, gRPC backends,
  custom protocols all fit.
- Tracks per-user usage so the digitorn quota can be debited.

## Endpoints

| Method | Path                       | Description |
|--------|----------------------------|-------------|
| POST   | `/v1/chat/completions`     | OpenAI-compatible chat. `stream=true` for SSE. |
| GET    | `/v1/models`               | List of model aliases visible to the caller. |
| GET    | `/healthz`                 | Liveness probe. |

All `/v1/*` routes require `Authorization: Bearer <digitorn-jwt>`.

## Local run

```bash
cd packages/gateway

# 1. Install deps in a venv
python -m venv .venv
.venv/Scripts/activate    # or `source .venv/bin/activate` on POSIX
pip install -e .

# 2. Wire up your provider keys (LiteLLM picks them up by env var)
export ANTHROPIC_API_KEY=sk-ant-...
export OPENAI_API_KEY=sk-...

# 3. Tell the gateway where the auth service lives
export DIGITORN_GATEWAY_AUTH_JWKS_URL=http://localhost:8000/.well-known/jwks.json
export DIGITORN_GATEWAY_AUTH_ISSUER=http://localhost:8000

# 4. Copy the example model catalogue and tweak as you wish
cp config/models.yaml.example config/models.yaml
export DIGITORN_GATEWAY_MODELS_CONFIG_PATH=$PWD/config/models.yaml

# 5. Run
python -m digitorn_gateway
# or
uvicorn digitorn_gateway.main:app --port 8002 --reload
```

Hit it:

```bash
TOKEN="<your digitorn JWT>"
curl -X POST http://127.0.0.1:8002/v1/chat/completions \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "digitorn-pro",
    "messages": [{"role": "user", "content": "Hi"}],
    "stream": false
  }'
```

## Deploy on Fly.io

```bash
cd packages/gateway

# First time: create the app
fly launch --no-deploy --name digitorn-gateway --region cdg

# Set provider keys (one-off)
fly secrets set ANTHROPIC_API_KEY=sk-ant-...
fly secrets set OPENAI_API_KEY=sk-...
fly secrets set DEEPSEEK_API_KEY=sk-...

# Deploy
fly deploy
```

## Configuration

All settings are env vars prefixed `DIGITORN_GATEWAY_`. See `config.py`.

| Variable | Default | Description |
|----------|---------|-------------|
| `HOST` | `0.0.0.0` | Bind address. |
| `PORT` | `8002` | Bind port. |
| `LOG_LEVEL` | `info` | `debug`/`info`/`warning`/`error`. |
| `AUTH_JWKS_URL` | `https://auth.digitorn.ai/.well-known/jwks.json` | JWKS to verify JWTs against. |
| `AUTH_ISSUER` | `https://auth.digitorn.ai` | Required `iss` claim. |
| `AUTH_JWKS_REFRESH_SECONDS` | `900` | How often to re-pull JWKS. |
| `MODELS_CONFIG_PATH` | `config/models.yaml` | Path to the alias catalogue. |
| `QUOTA_BACKEND` | `none` | `none` / `inproc` / `postgres`. |
| `QUOTA_POSTGRES_URL` | `null` | Required when `QUOTA_BACKEND=postgres`. |
| `MAX_REQUEST_BYTES` | `10485760` | 10 MB request size cap. |

## Architecture

```
client (digitorn-bridge daemon, web app, Flutter app, ...)
   │  Authorization: Bearer <digitorn-jwt>
   ▼
┌───────────────────────────┐
│ digitorn-gateway (this)   │
│  ┌─────────────────────┐  │
│  │ FastAPI             │  │
│  │   /v1/chat/...      │  │
│  └──────────┬──────────┘  │
│             │             │
│  auth ─ resolve alias ─ quota check  ─┐
│             │                          │
│             ▼                          │
│   ┌────────────────────┐               │
│   │ llm_call.dispatch  │               │
│   └─┬───────────────┬──┘               │
│     │               │                  │
│     ▼               ▼                  │
│   LiteLLM       CustomRouter           │
│   (100+ prov)   (your code)            │
│     │               │                  │
└─────┼───────────────┼──────────────────┘
      ▼               ▼
   Anthropic      In-house model
   OpenAI         (vLLM, gRPC, ...)
   DeepSeek
   ...
```

## Adding a custom provider

LiteLLM covers most of the market, but when you need something it
doesn't (a new vendor, an in-house model, a non-HTTP protocol), the
custom router is the extension point.

1. Declare the model in `config/models.yaml`:
   ```yaml
   models:
     mycorp-finetune:
       provider: custom
       model: mycorp-llama-7b
       endpoint: https://internal.mycorp.local/llm
       cost_per_1k_input_tokens: 0.0
       cost_per_1k_output_tokens: 0.0
   ```

2. Subclass `CustomRouter` in your deployment image:
   ```python
   from digitorn_gateway.custom_router import CustomRouter, set_router

   class MyRouter(CustomRouter):
       async def handle(self, *, entry, body):
           if entry.model == "mycorp-llama-7b":
               return await call_my_grpc_backend(entry.extra["endpoint"], body)
           return await super().handle(entry=entry, body=body)

   set_router(MyRouter())
   ```

3. The dispatcher in `llm_call.py` will route `model=mycorp-finetune`
   requests to your handler instead of LiteLLM. Streaming via
   `handle_stream` works the same way.

No fork of LiteLLM needed.
