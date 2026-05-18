# Image Support - Complete Specification

## Implementation Status: COMPLETE

All core components implemented and tested (62/62 tests pass):

| Component                       | Status |
|---------------------------------|:------:|
| ImageStore (disk storage)       | Done   |
| Multimodal messages             | Done   |
| Messages surface accepts images | Done   |
| Image fetch surface             | Done   |
| Anthropic provider vision       | Done   |
| OpenAI provider vision          | Done   |
| filesystem.read images          | Done   |
| agent_loop image injection      | Done   |
| Socket.IO image events          | Done   |
| Image aging                     | Done   |
| YAML vision config              | Done   |
| Daemon image config             | Done   |

## Overview

Support for images at every level of the framework:
- **User -> Agent**: the user sends images (upload, paste, URL).
- **Tool -> Agent**: a tool produces an image (screenshot, diagram, chart).
- **Agent -> User**: images are rendered in the chat.

## Ecosystem reference

### Claude Code (current capabilities)
- Cmd+V pastes a screenshot into the chat.
- The Read tool does NOT read images from the filesystem.
- The agent cannot capture screenshots on its own.

### Anthropic API (Claude)
```json
{
  "role": "user",
  "content": [
    {"type": "text", "text": "What's in this image?"},
    {"type": "image", "source": {
      "type": "base64", "media_type": "image/png", "data": "iVBOR..."
    }}
  ]
}
```
- Formats: JPEG, PNG, GIF, WebP.
- Max: 8000x8000 px, 100 images per request (200K context).
- **Best practice**: use the Files API for recurring images
  (upload once, reference by `file_id` after).

### OpenAI API (GPT-4o)
```json
{
  "role": "user",
  "content": [
    {"type": "text", "text": "What's in this image?"},
    {"type": "image_url", "image_url": {
      "url": "data:image/png;base64,iVBOR..."
    }}
  ]
}
```
- Formats: PNG, JPEG, WebP, non-animated GIF.
- Max: 50MB payload, 500 images per request.
- `detail: "low"` (512px) or `"high"` (native) trades cost vs quality.

### DeepSeek
- DeepSeek-chat (V3): no vision.
- DeepSeek-VL: separate vision model (7B, 1.3B).
- The standard deepseek-chat API does NOT accept images.

---

## Architecture

### Design principles

1. **Images do NOT live in the messages** - they are stored on
   disk and referenced by an `image_id`. Inflated to base64
   only at LLM-call time (most recent turn only for older
   images).

2. **Unified format** - a `ContentBlock` abstracts differences
   between providers. Anthropic/OpenAI conversion happens in
   the provider, not the agent loop.

3. **Tools can return images** - `ActionResult` supports image
   blocks in `metadata`. The agent loop injects them into the
   messages.

4. **The client receives images over Socket.IO** - no separate
   routes needed; images are inline (base64) in events on the
   `/events` namespace.

---

## 1. Image store (disk storage)

### New component: `ImageStore`

```python
class ImageStore:
    """Store images on disk, return lightweight references."""
    
    def __init__(self, base_dir: Path):
        self._base_dir = base_dir  # ~/.digitorn/images/
    
    async def store(self, data: bytes, mime: str, session_id: str) -> ImageRef:
        """Store an image, return a reference."""
        image_id = uuid4().hex[:12]
        ext = {"image/png": ".png", "image/jpeg": ".jpg", ...}[mime]
        path = self._base_dir / session_id / f"{image_id}{ext}"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return ImageRef(
            image_id=image_id,
            path=str(path),
            mime=mime,
            size=len(data),
            width=...,  # from PIL if available
            height=...,
        )
    
    async def get(self, image_id: str, session_id: str) -> bytes | None:
        """Fetch the bytes of an image."""
        ...
    
    async def get_base64(self, image_id: str, session_id: str) -> str | None:
        """Fetch as base64 (for LLM injection)."""
        ...
    
    def cleanup_session(self, session_id: str):
        """Delete every image for a session."""
        ...

@dataclass
class ImageRef:
    image_id: str
    path: str
    mime: str
    size: int
    width: int = 0
    height: int = 0
```

### Why not base64 inside messages?

A PNG screenshot is 500KB-2MB base64. Over 10 turns with 3 images each:
- Base64 inside messages = 30MB in memory, re-sent on every LLM call.
- Reference + injection on-demand = a few KB in memory.

### Injection strategy

| Turn | Current-turn images | Previous-turn images |
|------|:-:|:-:|
| Current turn | full base64 (high resolution) | - |
| Turn N-1 | low-resolution base64 (resized to 512px) | - |
| Turn N-2+ | Text: "[Image: screenshot of login page, 1920x1080]" | - |

Keeps the context light while still giving the LLM vision over recent images.

---

## 2. Message Format (multimodal)

### ContentBlock

```python
@dataclass
class ContentBlock:
    type: str  # "text", "image", "image_ref"
    
    # For type="text"
    text: str = ""
    
    # For type="image" (inline base64)
    image_data: str = ""  # base64
    media_type: str = ""  # "image/png"
    
    # For type="image_ref" (reference into the image store)
    image_id: str = ""
    alt_text: str = ""  # text description used for context
```

### Multimodal messages

```python
# Before (text only)
{"role": "user", "content": "Fix this bug"}

# After (multimodal)
{"role": "user", "content": [
    {"type": "text", "text": "Fix this bug, here's the screenshot:"},
    {"type": "image_ref", "image_id": "abc123", "alt_text": "Screenshot of error page"}
]}
```

The `content` is either a `str` (backward compatible) or a `list[ContentBlock]`.

---

## 3. Sending images with a message

The daemon's messages surface accepts images alongside the
text body, either as multipart upload (one or more
`images[]` parts plus optional `workspace`) or as JSON with
base64 payloads. The exact route shape is not documented
publicly; clients use the SDK.

JSON shape (handled by the SDK):

```jsonc
{
  "message": "Fix this bug",
  "images": [
    {"data": "iVBOR...", "mime": "image/png", "name": "screenshot.png"}
  ],
  "workspace": "/path/to/project"
}
```

### Limits

| Setting | Value | Configurable |
|-----------|--------|:---:|
| Max images per message | 10 | Yes (`images.max_per_message`) |
| Max size per image | 10MB | Yes (`images.max_size_bytes`) |
| Accepted formats | PNG, JPEG, WebP, GIF | No |
| Max total images per session | 100 | Yes (`images.max_per_session`) |

---

## 4. LLM provider - multimodal conversion

### Anthropic Provider

```python
# Convert content blocks to Anthropic format
def _build_content(blocks: list[ContentBlock]) -> list[dict]:
    result = []
    for block in blocks:
        if block.type == "text":
            result.append({"type": "text", "text": block.text})
        elif block.type == "image":
            result.append({
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": block.media_type,
                    "data": block.image_data,
                }
            })
        elif block.type == "image_ref":
            # Resolve the reference -> base64
            data = image_store.get_base64(block.image_id)
            if data:
                result.append({
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": block.media_type or "image/png",
                        "data": data,
                    }
                })
            else:
                # Expired image -> inject a text description
                result.append({"type": "text", "text": f"[Image: {block.alt_text}]"})
    return result
```

### OpenAI-Compatible Provider (GPT-4o, etc.)

```python
def _build_content(blocks: list[ContentBlock]) -> list[dict]:
    result = []
    for block in blocks:
        if block.type == "text":
            result.append({"type": "text", "text": block.text})
        elif block.type == "image":
            result.append({
                "type": "image_url",
                "image_url": {
                    "url": f"data:{block.media_type};base64,{block.image_data}",
                    "detail": "high",
                }
            })
    return result
```

### Providers sans vision (DeepSeek-chat, Ollama text-only)

```python
def _build_content(blocks: list[ContentBlock]) -> list[dict]:
    # Convertir les images en descriptions textuelles
    texts = []
    for block in blocks:
        if block.type == "text":
            texts.append(block.text)
        elif block.type in ("image", "image_ref"):
            texts.append(f"[Image: {block.alt_text or 'uploaded image'}]")
    return [{"type": "text", "text": "\n".join(texts)}]
```

The provider auto-detects whether the model supports vision.

---

## 5. Tools - images as input and output

### Filesystem : Read image

Le tool `filesystem.read` doit supporter la lecture d'images :

```python
async def read(self, params: ReadParams) -> ActionResult:
    path = self._resolve(params.path)
    
    if _is_image(path):
        # Lire comme image, pas comme texte
        data = path.read_bytes()
        base64_data = base64.b64encode(data).decode()
        mime = _mime_for(path.suffix)
        
        return ActionResult(
            success=True,
            data={
                "path": str(path),
                "type": "image",
                "mime": mime,
                "size": len(data),
            },
            metadata={
                "image_data": base64_data,  # Pour le LLM (via agent_loop)
                "media_type": mime,
            }
        )
```

### Browser : Screenshot

```python
async def screenshot(self, params: ScreenshotParams) -> ActionResult:
    # Capture screenshot via Playwright
    data = await page.screenshot(type="png")
    base64_data = base64.b64encode(data).decode()
    
    return ActionResult(
        success=True,
        data={
            "type": "image",
            "mime": "image/png",
            "width": viewport.width,
            "height": viewport.height,
        },
        metadata={
            "image_data": base64_data,
            "media_type": "image/png",
        }
    )
```

### Agent Loop - Injection automatique

In `_append_tool_result`, when the result contains an image:

```python
def _append_tool_result(ctx, messages, call_id, tool_name, result, ok, cb):
    # ... normal text serialisation ...
    
    # If the result has an image, inject it as a content block
    meta = getattr(result, "metadata", {}) or {}
    if "image_data" in meta:
        # Ajouter un message avec l'image pour que le LLM la voie
        messages.append({
            "role": "user",
            "content": [
                {"type": "text", "text": f"[Tool result image from {tool_name}]"},
                {"type": "image", "source": {
                    "type": "base64",
                    "media_type": meta.get("media_type", "image/png"),
                    "data": meta["image_data"],
                }}
            ]
        })
```

---

## 6. Socket.IO Events - Images vers le client

The daemon emits image events on the Socket.IO `/events` namespace, in the room
`session:{session_id}`. Les images arrivent dans les envelopes `tool_call`
with `image_data` (base64) + `image_mime` added to the payload.

### Dans tool_call event

```json
{
  "type": "tool_call",
  "data": {
    "name": "browser__screenshot",
    "result": {
      "type": "image",
      "mime": "image/png",
      "width": 1920,
      "height": 1080
    },
    "image_data": "iVBOR...",
    "image_mime": "image/png"
  }
}
```

### New event: image_message (for images embedded in replies)

```json
{
  "type": "image",
  "data": {
    "image_id": "abc123",
    "mime": "image/png",
    "data": "iVBOR...",
    "width": 800,
    "height": 600,
    "alt": "Diagram of the architecture",
    "source": "tool:presentation.render"
  }
}
```

---

## 7. Persistence - Images dans l'historique

### Session history avec images

`GET /sessions/{sid}/history` returns images as references:

```json
{
  "messages": [
    {
      "role": "user",
      "content": [
        {"type": "text", "text": "Fix this"},
        {"type": "image_ref", "image_id": "abc123", "alt_text": "Error screenshot",
         "mime": "image/png", "width": 1920, "height": 1080}
      ]
    }
  ]
}
```

### Image fetch

The daemon exposes a per-session image-fetch endpoint that
returns the raw bytes (`image/png`). Clients lazy-load
images on demand instead of receiving them inline in
history. The exact route shape is not documented publicly.

---

## 8. Optimisation du contexte

### Problem

A 1920x1080 PNG base64 weighs ~1-2MB ~= 500K estimated tokens.
Si chaque message a une image, le contexte explose en 3 tours.

### Solution : Image Aging

```python
class ImageContextManager:
    """Decides which images get inflated to base64 in the LLM messages."""
    
    def prepare_messages_for_llm(self, messages, current_turn):
        result = []
        for msg in messages:
            if not _has_images(msg):
                result.append(msg)
                continue
            
            blocks = []
            for block in msg["content"]:
                if block["type"] == "text":
                    blocks.append(block)
                elif block["type"] == "image_ref":
                    age = current_turn - block.get("turn", 0)
                    
                    if age == 0:
                        # Current turn -> high resolution
                        blocks.append(_resolve_full(block))
                    elif age <= 2:
                        # 1-2 turns ago -> low resolution (512px)
                        blocks.append(_resolve_low_res(block))
                    else:
                        # 3+ tours → texte seulement
                        blocks.append({
                            "type": "text",
                            "text": f"[Previous image: {block['alt_text']}]"
                        })
            
            result.append({**msg, "content": blocks})
        return result
```

### Estimated sizes

| Strategy | Size per image | Estimated tokens |
|-----------|:---:|:---:|
| High resolution (1920px) | 1-2 MB | ~300K |
| Low resolution (512px) | 50-100 KB | ~30K |
| Texte description | 50-100 chars | ~25 |

Avec image aging : 1 image full + 2 low-res + N descriptions = ~360K tokens max.
Without aging: N full images = N x 300K, context explosion.

---

## 9. Config

New settings in `~/.digitorn/config.yaml`:

```yaml
images:
  max_per_message: 10           # Max images par message
  max_size_bytes: 10485760      # 10MB par image
  max_per_session: 100          # Max images par session
  storage_dir: ""               # Vide = ~/.digitorn/images/
  low_res_size: 512             # Taille pour les images anciennes (px)
  aging_full_turns: 1           # Turns kept at high resolution
  aging_low_turns: 2            # Turns kept at low resolution
  cleanup_after_days: 7         # Delete images after N days
```
---

## 10. YAML App Config

```yaml
agents:
  - id: main
    brain:
      provider: anthropic
      model: claude-sonnet-4-5
      vision: true              # Enable vision support (default: auto-detect)
```
If `vision: false` or the model lacks vision -> images are converted to
descriptions textuelles automatiquement.

---

## 11. Provider compatibility

| Provider | Vision | Format |
|----------|:---:|--------|
| Claude (Anthropic) | Oui | `{"type": "image", "source": {"type": "base64", ...}}` |
| GPT-4o (OpenAI) | Oui | `{"type": "image_url", "image_url": {"url": "data:..."}}` |
| GPT-4o-mini | Yes | Same format |
| DeepSeek-chat (V3) | Non | Converti en texte `[Image: ...]` |
| DeepSeek-VL | Oui | Format OpenAI-compat |
| Ollama (llava) | Yes | `{"images": ["base64..."]}` (special format) |
| Ollama (text-only) | Non | Converti en texte |

Detection is automatic via the provider. Each provider knows
whether its model supports vision.

---

## 12. Implementation - priority order

### Phase 1 (V1 - demo)
1. Route `/messages` accepte des images (multipart + JSON base64)
2. ImageStore basique (stockage disque)
3. Anthropic provider : injection base64 dans les messages
4. Socket.IO event avec image_data pour le client
5. Client web : upload/paste + affichage inline

### Phase 2 (V1.1)
6. OpenAI-compat provider : conversion format
7. Filesystem.read supporte les images
8. Image aging (context optimization)
9. Route GET `/images/<id>` pour lazy loading

### Phase 3 (V2)
10. Browser.screenshot → image dans le contexte
11. Presentation module → slides as images
12. Image generation tools (DALL-E, Stable Diffusion via MCP)
13. Anthropic Files API (upload once, reference by file_id)

---

## 13. Ce que Digitorn fera MIEUX que Claude Code

| Feature | Claude Code | Digitorn |
|---------|:---:|:---:|
| User paste image | Oui (Cmd+V) | Oui (paste + upload + URL) |
| Read image from disk | Non (bug) | Oui (filesystem.read) |
| Agent screenshot | Non | Oui (browser.screenshot) |
| Image in tool results | Non | Oui (metadata.image_data) |
| Multi-image per message | Limited | 10 images max |
| Image aging (context) | Non | Oui (full → low-res → text) |
| Provider fallback sans vision | Non | Oui (texte automatique) |
| Image persistence | Non | Oui (ImageStore + `/images/<id>`) |

Sources :
- [Claude Vision API](https://platform.claude.com/docs/en/build-with-claude/vision)
- [OpenAI Images and Vision](https://developers.openai.com/api/docs/guides/images-vision)
- [Claude Code image issue #35866](https://github.com/anthropics/claude-code/issues/35866)
- [Using images in Claude Code](https://amanhimself.dev/blog/using-images-in-claude-code/)
