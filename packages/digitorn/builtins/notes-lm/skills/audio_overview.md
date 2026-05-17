# Skill: Audio Overview (2-host podcast)

THE killer feature. Generate a podcast-style audio between two hosts discussing the corpus, then synthesise via OpenAI TTS.

Triggered by "audio overview", "podcast", "narrate my sources", "make it audio".

## Prerequisites

The user must have provided an OpenAI API key via secrets:

- `{{secret.OPENAI_API_KEY}}` — used to POST `https://api.openai.com/v1/audio/speech`

If the key is missing, refuse with this exact 2-line message:

```text
Audio overview needs an OpenAI API key.
Add OPENAI_API_KEY in your app secrets (Settings → Secrets) and retry. The other 5 skills work without it.
```

## Steps

### 1. Read the corpus

`WsGlob("attachments/**")` → `WsRead` each (sample if >10 files).

### 2. Generate the script as a workspace file

Compose a JSON dialogue in `audio_overview/script.json`:

```json
[
  {"speaker": "host_a", "text": "Welcome back. Today we're digging into <subject>."},
  {"speaker": "host_b", "text": "And the first thing that struck me is..."},
  {"speaker": "host_a", "text": "..."}
]
```

Script rules:

- **2 hosts** alternating, names `host_a` and `host_b`.
- **~40-70 turns** for a 5-7 min podcast (100-150 words per turn = ~6000-8000 words total).
- Personalities:
  - `host_a` = curious, asks questions, summarises
  - `host_b` = analytic, brings the data, occasionally contrarian
- **No citations spoken** — file provenance lives only in the JSON.
- **Verbal language**: contractions, "right?", "yeah", spoken phrasing. No bullets. No "in conclusion".
- **Real tension**: if sources disagree, dramatise it.
- Each `text` must fit in OpenAI TTS limit (~4000 chars).

Write via `WsWrite(path="audio_overview/script.json", content=<json>)`.

### 3. Synthesise each turn via OpenAI TTS

Iterate the script. For each turn `i`:

```text
http.post(
  url="https://api.openai.com/v1/audio/speech",
  headers={
    "Authorization": "Bearer {{secret.OPENAI_API_KEY}}",
    "Content-Type": "application/json"
  },
  json={
    "model": "gpt-4o-mini-tts",
    "voice": "alloy" if speaker == "host_a" else "echo",
    "input": text,
    "response_format": "mp3"
  }
)
```

The `http` module returns binary content. Save via `WsWrite(path="audio_overview/turn_<NNN>.mp3", content=<base64-from-response>)`.

If the http response shape doesn't allow direct binary write, fall back to listing the script and tell the user to call OpenAI TTS themselves with the script file.

### 4. List the segments

Write `audio_overview.md`:

```markdown
# Audio Overview · <date>

Total turns: <N> · Estimated duration: ~<minutes> min

## Listen

1. [Turn 1 · host_a](audio_overview/turn_001.mp3)
2. [Turn 2 · host_b](audio_overview/turn_002.mp3)
...

## Script

See [audio_overview/script.json](audio_overview/script.json) for the dialogue text.
```

### 5. Reply

`Audio overview ready: <N> turns, ~<duration> min. Open audio_overview.md to listen.`

## Fallback (no TTS key OR http failure)

- Write the script JSON + a readable `audio_overview.md` with the dialogue inlined as quoted lines.
- Tell the user: "Script ready. Plug an OpenAI key for audio synthesis."

## Cost note

5-7 min at gpt-4o-mini-tts pricing ≈ $0.02-0.05. Mention this once on the first generation.

## Caveats

- TTS API caps input at ~4000 chars per call. Split long turns automatically.
- Sequential synthesis (~1-2s per turn) → 50 turns = 1-2 min wall time. Tell the user upfront.
- If a turn fails, retry once with `voice: "nova"` as fallback then skip.
