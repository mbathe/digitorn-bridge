---
id: voice
title: Voice transcription
---

# Voice transcription

The daemon ships a voice-transcription pipeline. The Flutter
mic button records audio, the daemon runs Whisper, the
client gets back the text. The Flutter client falls back to
attaching the raw audio to the next chat message if any
step fails (404, 413, 422, 500), so the feature degrades
gracefully.

The HTTP endpoint that backs this pipeline is not documented
publicly - public clients use the Flutter client (or any
client built on the [Python testing SDK](../client-sdks/python-testing.md))
which abstracts the route.

## Configuration

`~/.digitorn/config.yaml`:

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

- Uses `faster-whisper` (4x faster than the original
  `openai-whisper`).
- Install: `pip install digitorn[transcribe]`.
- Model cached to `~/.cache/huggingface/` on first request.
  First call downloads ~150 MB for `base`, ~500 MB for
  `small`.
- Audio decoding via the `av` (PyAV) package - no system
  `ffmpeg` required for most formats.

### `provider: openai`

- Calls Whisper over the OpenAI API with model `whisper-1`.
- The API key is read from the Digitorn credentials store
  (never from `config.yaml`, never from an env var as the
  primary source - secrets do not belong in plaintext
  config).
- Cost about $0.006 per minute of audio. Zero local infra.
- `confidence` is not returned (OpenAI doesn't expose
  per-segment logprobs).

## Registering the OpenAI key (CLI)

Register the key in the credentials store, never in
`config.yaml`:

```bash
# System-wide (default for a single-tenant deploy)
digitorn credentials set openai api_key sk-... --scope system

# Per-user (each user has their own key, billed separately)
digitorn credentials set openai api_key sk-... --scope user
```

Resolution order at transcription time (first hit wins):

1. `(user_id, app_id)` - per-app per-user
2. `(user_id, None)` - per-user
3. `(None, app_id)` - per-app shared
4. `(None, None)` - system-wide
5. `OPENAI_API_KEY` env var - **dev/CI fallback only**, not
   for production.

The daemon never logs or returns the key. The credentials
health surface reports `ready: true/false` but never leaks
the value.

## Privacy

- The uploaded audio is held in memory + a short-lived temp
  file, then deleted immediately after transcription (even
  on error).
- Nothing persists to the database. No replay, no log of the
  transcript text.
- Logs contain only: `user_id`, byte size, detected
  language, duration in ms, elapsed ms, and HTTP status
  code.

## Client fallback behaviour

The Flutter client gracefully degrades if the transcribe
endpoint returns 404, 413, 422, or 500: it shows a toast and
attaches the raw audio file to the next message instead. So
enabling / disabling transcription at any time is safe - no
user-visible breakage.

## Limits

| Status | When |
|---|---|
| 401 | JWT missing/expired. |
| 404 | `transcribe.enabled: false` or build lacks `faster-whisper`. |
| 413 | Audio over `max_audio_bytes` (25 MB default). |
| 422 | Audio under `min_audio_bytes` (500 B) or transcript empty. |
| 500 | Provider timeout / internal failure. |

The daemon never returns 200 with empty `text`; empty
transcriptions surface as 422.
