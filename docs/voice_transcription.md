# Voice Transcription — `POST /api/transcribe`

End-to-end contract for the Flutter mic button: record audio → upload →
daemon runs Whisper → text returned to the client. The client falls
back to attaching the raw audio to the next chat message if any step
fails (404, 413, 422, 500), so the feature degrades gracefully.

## Endpoint

```http
POST /api/transcribe
Content-Type: multipart/form-data
Authorization: Bearer <jwt>
```

| Field | Type | Required | Notes |
|---|---|---|---|
| `audio` | file | yes | `.m4a` / `.webm` / `.wav` / `.mp3` / `.ogg`. Max 25 MB by default. |
| `language` | string | no | BCP-47 hint (`fr`, `en-US`, `es`). Auto-detect if omitted. |
| `app_id` | string | no | Current app context. Reserved for future vocab biasing. |

## Responses

### 200 OK
```jsonc
{
  "success": true,
  "data": {
    "text": "Crée une fonction qui lit les tokens d'authentification",
    "language": "fr",       // optional
    "duration_ms": 3200,     // optional
    "confidence": 0.96       // optional, 0–1 scale (local provider only)
  },
  "error": null
}
```

Only `text` is guaranteed — clients must tolerate missing metadata.
The daemon **never** returns `200` with an empty `text`; empty
transcriptions return `422`.

### Error codes (strictly enforced)

| Status | When | Body |
|---|---|---|
| 401 | JWT missing/expired | Standard auth middleware response |
| 404 | `transcribe.enabled: false` or build lacks `faster-whisper` | `{error: "transcription disabled"}` |
| 413 | Audio > `max_audio_bytes` (25 MB default) | `{error: "Audio too large (max 25 MB)"}` |
| 422 | Audio < `min_audio_bytes` (500 B) OR transcript empty | `{error: "Audio too short or empty"}` / `{error: "Transcription returned empty text"}` |
| 500 | Provider timeout / internal failure | `{error: "Transcription failed: <Type>"}` |

## Providers

Configured in `~/.digitorn/config.yaml`:

```yaml
transcribe:
  enabled: true
  provider: local     # or "openai"
  model: base         # faster-whisper size (local only)
  device: auto        # cpu | cuda | auto
  compute_type: int8  # CPU: int8 | CUDA: int8_float16 | float16 | float32
  max_audio_bytes: 26214400
  timeout_seconds: 120.0
```

### `provider: local` (default)

- Uses `faster-whisper` (4× faster than the original `openai-whisper`).
- Install: `pip install digitorn[transcribe]`.
- Model cached to `~/.cache/huggingface/` on first request. First call
  downloads ~150 MB for `base`, ~500 MB for `small`.
- Audio decoding via the `av` (PyAV) package — no system `ffmpeg`
  required for most formats.

### `provider: openai`

- Calls `https://api.openai.com/v1/audio/transcriptions` with model `whisper-1`.
- **API key is read from the Digitorn credentials store** (never from
  `config.yaml` and never from an env var as the primary source — secrets
  do not belong in plaintext config). See [Secrets (OpenAI API key)](#secrets-openai-api-key).
- Cost ≈ $0.006 / minute of audio. Zero local infra.
- `confidence` is not returned (OpenAI doesn't expose per-segment logprobs).

#### <a id="secrets-openai-api-key"></a>Secrets — OpenAI API key

Digitorn has a dedicated encrypted credentials store. Register the key
there, not in `config.yaml`:

```bash
# System-wide (default for a single-tenant deploy)
digitorn credentials set openai api_key sk-... --scope system

# Per-user (each user has their own key, billed separately)
digitorn credentials set openai api_key sk-... --scope user
```

Or via REST:
```bash
curl -X POST http://127.0.0.1:8000/api/credentials \
  -H "Authorization: Bearer $JWT" \
  -H "Content-Type: application/json" \
  -d '{
    "provider_name": "openai",
    "provider_type": "api_key",
    "scope": "system_wide",
    "fields": {"api_key": "sk-..."}
  }'
```

Resolution order (first hit wins):

1. `(user_id, app_id)` — per-app per-user
2. `(user_id, None)` — per-user
3. `(None, app_id)` — per-app shared
4. `(None, None)` — system-wide
5. `OPENAI_API_KEY` env var — **dev/CI fallback only**, not for production.

The daemon never logs or returns the key. The `/health` endpoint
reports `ready: true/false` but never leaks the value.

## Health check

```http
GET /api/transcribe/health
```
```jsonc
{
  "enabled": true,
  "provider": "local",
  "model": "base",
  "ready": true,     // false → see error field
  "error": null
}
```

Hit this at startup to detect misconfigurations (missing deps, unset
API key) before the user hits record.

## Privacy

- The uploaded audio is held in memory + a short-lived temp file, then
  deleted immediately after transcription (even on error).
- Nothing persists to the database. No replay, no log of the
  transcript text.
- Logs contain only: `user_id`, byte size, detected language, duration
  in ms, elapsed ms, and HTTP status code.

## Client fallback behaviour

The Flutter client gracefully degrades if the endpoint returns 404,
413, 422, or 500: it shows a toast and attaches the raw audio file to
the next message instead. So enabling/disabling transcription at any
time is safe — no user-visible breakage.

## Loopback access

`/api/transcribe` is in the loopback-agent allow-list (see
`auth/middleware.py::_LOOPBACK_AGENT_PATH_PREFIXES`). In-process
agents can call it without a JWT — useful for workflows that process
audio attachments (agent receives an audio, calls the daemon to
transcribe, continues with the text).

## Testing

Four behavior tests cover the contract (see `docs/RULES_MATRIX.md`):

| # | Covers |
|---|---|
| **TRX01** | Real speech round-trip (via `edge-tts` synth) returns `success: true` + non-empty `text` + language. |
| **TRX02** | 100-byte payload → 422. |
| **TRX03** | 26 MB payload → 413. |
| **TRX04** | `/health` returns provider + ready flag. |

Run:
```bash
py -3.12 tools/behavior_tests.py --only "TRX01,TRX02,TRX03,TRX04"
```

## Quick curl smoke-test

```bash
# 1. Enable locally (faster-whisper, no external key needed)
pip install digitorn[transcribe]
digitorn restart

# 2a. OR enable OpenAI provider — register the key in credentials (NEVER config.yaml)
digitorn credentials set openai api_key sk-... --scope system
echo 'transcribe: {provider: openai}' >> ~/.digitorn/config.yaml
digitorn restart

# 3. Hit health
curl http://127.0.0.1:8000/api/transcribe/health

# 4. Transcribe an audio
curl -X POST http://127.0.0.1:8000/api/transcribe \
  -F "audio=@sample.m4a" \
  -F "language=fr"
# → {"success":true,"data":{"text":"...","language":"fr",...},"error":null}
```
