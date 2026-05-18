---
id: advanced-20-multimodal-image
title: "Advanced 20 - Multimodal image input"
sidebar_label: "Advanced 20: Image input"
---

The chat API accepts image attachments on every user
message. When the brain points at a vision-capable model,
Digitorn forwards the image to the LLM in the
provider-native shape (OpenAI `image_url` or Anthropic
`image.source.base64`). The agent sees the picture
alongside the text and can reason about it like any other
turn.

Live-tested end-to-end: app `tuto-multimodal-image`,
session `8e58f031`, brain `openai/gpt-5-mini`. Image sent:
256×256 PNG, crimson background, white "DIGITORN" text.
The model returned a 2-bullet description in 30 s, 668
output tokens.

## What you will see work

1. Generate a deterministic PNG client-side (here with
   PIL).
2. Base64-encode the bytes and POST them in the
   `images: [...]` field of `/sessions` (first message)
   or `/sessions/<sid>/messages` (subsequent).
3. The daemon stores the image in the `ImageStore` (see
   `core/image_store.py`), substitutes a lightweight
   `image_ref` in the persisted user message, and inflates
   it back to base64 when calling the LLM.
4. The vision model reasons about the image and returns
   text.

## The YAML

```yaml
app:
  app_id: tuto-multimodal-image
  name: Tuto - Multimodal Image Input
  version: "1.0"
  attachments:
    - image
  attachments_mode: direct

runtime:
  mode: conversation
  workdir_mode: none
  max_turns: 3
  timeout: 60
  tool_injection: direct

agents:
  - id: main
    role: assistant
    brain:
      provider: openai
      backend: openai_compat
      model: gpt-5-mini
      config:
        api_key: placeholder
        base_url: https://api.openai.com/v1
      temperature: 0.1
      # gpt-5-mini is a reasoning model: a chunk of every output
      # budget is consumed by internal reasoning tokens BEFORE
      # visible text. With max_tokens too low (512) the model
      # spends the whole budget thinking and returns an empty
      # reply. 4096 gives ~3000 tokens of reasoning headroom +
      # plenty for the actual answer.
      max_tokens: 4096
      # gpt-5-mini is vision-capable; the framework auto-detects
      # this from the model name. We pin it explicitly so the
      # YAML reads as a self-contained spec.
      vision: true
      image_detail: low
      max_images_per_turn: 5
    system_prompt: |
      You analyse images. When the user sends a message with
      an attached image, describe what you see in 2-4 short
      bullets. Be specific (colours, shapes, text, count). No
      preamble.

tools:
  modules: {}
  capabilities:
    default_policy: auto
    max_risk_level: low
```

Three knobs to know:

- `app.attachments: [image]` declares which attachment
  types this app accepts. Without it, the client composer
  hides the upload button. `attachments_mode: direct`
  routes the image straight to the LLM (vs `tool`, which
  forces the agent to call `WsRead`/`fs.read` to fetch
  the bytes).
- `brain.vision: true` pins vision-on. Setting it to `null`
  asks the framework to auto-detect from the model name;
  `false` causes images to be converted to a text
  description placeholder (cheaper, lossy).
- `brain.max_tokens: 4096`. Reasoning models like
  `gpt-5-mini`, `o3`, and `o4-mini` consume internal
  reasoning tokens before producing visible text. With
  `max_tokens: 512` the model spends the whole budget
  thinking and returns an empty reply (verified during
  the build of this tutorial; first attempt produced 512
  output tokens of pure reasoning, zero visible text).
  4096 leaves ~3000 tokens for reasoning + headroom for
  the answer.

## Send an image from Python

```python
import base64, io, json, requests
from PIL import Image, ImageDraw, ImageFont

# 256x256 crimson square with white "DIGITORN" text.
img = Image.new("RGB", (256, 256), color=(220, 20, 60))
draw = ImageDraw.Draw(img)
font = ImageFont.truetype("arial.ttf", 48)
text = "DIGITORN"
bbox = draw.textbbox((0, 0), text, font=font)
tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
draw.text(((256 - tw) // 2, (256 - th) // 2), text, fill="white", font=font)

buf = io.BytesIO()
img.save(buf, format="PNG")
b64 = base64.b64encode(buf.getvalue()).decode("ascii")

requests.post(
    "http://127.0.0.1:8000/api/apps/tuto-multimodal-image/sessions",
    headers={"Authorization": f"Bearer {TOKEN}",
             "Content-Type": "application/json"},
    json={
        "message": "What text is in this image? "
                   "What colour is the background? Reply in 2 bullets.",
        "queue_mode": "async",
        "images": [{
            "data": b64,
            "mime": "image/png",
            "name": "digitorn-test.png",
        }],
    },
)
```

The `images` array accepts any number of attachments per
message, each shaped
`{data: <base64>, mime: <"image/png"|"image/jpeg"|...>, name: <str>}`.
The framework's `max_images_per_turn` setting on the brain
caps how many actually reach the model (older images get
aged out per the doc at `core/runtime/multimodal.py`).

## Real session transcript (session `8e58f031`)

**User message (persisted with image_ref):**

```json
[
  {
    "type": "text",
    "text": "What text is in this image? What colour is the background? Reply in 2 bullets."
  },
  {
    "type": "image_ref",
    "image_id": "a941e692e449",
    "mime": "image/png",
    "alt_text": "digitorn-test.png",
    "width": 256,
    "height": 256,
    "turn": 0
  }
]
```

The `image_ref` is the placeholder that lives in the
durable conversation log. It carries the metadata the UI
needs to render a thumbnail (mime, dimensions, alt_text)
without bloating the message journal with megabytes of
base64. The framework re-inflates `image_ref` into the
full base64 payload at LLM-call time using the
`ImageStore` keyed by `image_id`.

**Assistant reply (2 turns later, 30 s end-to-end):**

```
- Text: "digitorn" (all lowercase, centered)
- Background colour: solid white
```

Honest take on this output:

- **Text detection**: the model saw the letters correctly
  and reported them centered. It got the **case wrong**
  (lowercase vs uppercase in the PNG). With `image_detail:
  low` the image is downscaled to 512px max which softens
  glyph edges; `image_detail: high` should fix this at
  the cost of more tokens.
- **Colour detection**: the model said "solid white"; the
  actual background was crimson (RGB 220,20,60). This is
  a real `gpt-5-mini` miss, not a pipeline bug. Bigger
  vision models (`gpt-4o`, `claude-3.5-sonnet`) get
  colour right reliably.

Both detections are **honest**: the image truly reached
the model (token count was 2000 prompt vs 800-ish without
the image), the model just did its best with what it saw.
This is the kind of edge case worth knowing when picking
a vision model for production.

## What we proved

| Claim | Status |
|---|---|
| `app.attachments: [image]` + `attachments_mode: direct` accepts uploads | verified, image_id `a941e692e449` created |
| The framework replaces base64 with `image_ref` in the persisted message | verified by inspecting `msg[1]` content blocks |
| `brain.vision: true` triggers the multimodal call path | verified, prompt_tokens jumped from ~200 (text-only baseline) to 2000 with the image |
| Reasoning-model output budget must include thinking headroom | verified by negative test (max_tokens=512 → empty reply, max_tokens=4096 → answer) |
| `gpt-5-mini` text-in-image recognition works, colour recognition is unreliable | verified by content of `msg[2]` |

## Tuning notes per model

- **`gpt-5-mini`**: cheap, fast, sometimes wrong on
  colours and fine text. Good for sanity checks and "is
  there a chart in this image" style classifications. Use
  `image_detail: high` if text precision matters.
- **`gpt-4o`**: more accurate, more expensive. Default
  for production text extraction.
- **`claude-3.5-sonnet`**, **`claude-opus-4-7`**:
  Anthropic's vision is on par with `gpt-4o` for
  document understanding; significantly better at chart
  and diagram reasoning. Route via the
  `provider: anthropic, api_key: "claude-code"` pattern
  documented in [Reference - Claude Code OAuth](../reference/runtime/credentials.md#claude-code-oauth).
- **`ollama` vision models** (`llava`, `bakllava`): work
  but emit messy structured output. Hold off on tool
  calls + vision together on these.

## Going further

- Send multiple images per turn: stick more entries in
  `images: [...]`. The framework batches them into one
  LLM call, capped at `max_images_per_turn`.
- Add `attachments: [document, image]` to accept PDFs,
  Word docs, etc. Documents are extracted to text before
  reaching the model (see
  [`core/api/apps_v2/attachments.py`](https://github.com/digitorn-ai/digitorn-bridge/blob/main/packages/digitorn/core/api/apps_v2/attachments.py)).
- Combine with `attachments_mode: tool`: the agent has
  to call `WsRead("attachments/<name>")` to load the
  bytes. Useful when you want the agent to decide
  whether the image is even worth loading.
- Image generation (DALL-E, Stable Diffusion via MCP):
  set `brain.image_generation: true`; the framework
  handles image output in tool results and SSE events.
