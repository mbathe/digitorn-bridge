# Image Support - Complete Specification

## Implementation Status: COMPLETE

All core components implemented and tested (62/62 tests pass):

| Component | File | Status |
|-----------|------|:---:|
| ImageStore (disk storage) | `core/image_store.py` | Done |
| Multimodal messages | `core/runtime/multimodal.py` | Done |
| Route /messages with images | `core/api/apps_v2/messages.py` | Done |
| Route GET /images/{id} | `core/api/apps_v2/sessions.py` | Done |
| Anthropic provider vision | `llm_provider/providers/anthropic.py` | Done |
| OpenAI provider vision | `llm_provider/providers/openai_compat.py` | Done |
| filesystem.read images | `modules/filesystem/module.py` | Done |
| agent_loop image injection | `core/runtime/agent_loop.py` | Done |
| Socket.IO image events | `core/app/manager.py` | Done |
| Image aging | `core/runtime/multimodal.py` | Done |
| YAML vision config | `core/app/schema.py` | Done |
| Daemon image config | `core/config.py` | Done |

## Overview

Support for images at every level of the framework :
- **User → Agent** : l'utilisateur envoie des images (upload, paste, URL)
- **Tool → Agent** : un outil produit une image (screenshot, diagram, chart)
- **Agent → User** : les images sont affichées dans le chat

## State of the Art

### Claude Code (limites actuelles)
- Cmd+V pour coller un screenshot dans le chat - fonctionne
- Le Read tool ne peut PAS lire les images depuis le filesystem
- L'agent ne peut pas prendre de screenshots lui-même
- C'est une limitation reconnue par Anthropic (issues #30925, #35866)

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
- Formats : JPEG, PNG, GIF, WebP
- Max : 8000x8000 px, 100 images par requête (200K context)
- **Best practice** : utiliser la Files API pour les images récurrentes
  (upload une fois, référencer par `file_id` ensuite)

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
- Formats : PNG, JPEG, WebP, GIF non-animé
- Max : 50MB payload, 500 images par requête
- `detail: "low"` (512px) ou `"high"` (natif) pour le coût

### DeepSeek
- DeepSeek-chat (V3) : PAS de vision
- DeepSeek-VL : modèle séparé avec vision (7B, 1.3B)
- L'API standard deepseek-chat ne supporte pas les images

---

## Architecture

### Principes de design

1. **Les images ne vivent PAS dans les messages** - elles sont stockées sur disque
   et référencées par un `image_id`. Injectées en base64 uniquement au moment
   de l'appel LLM (dernier tour seulement pour les anciennes images).

2. **Format unifié** - un `ContentBlock` abstrait les différences entre providers.
   La conversion Anthropic/OpenAI se fait dans le provider, pas dans l'agent loop.

3. **Les tools peuvent retourner des images** - le `ActionResult` supporte
   des blocs image dans `metadata`. L'agent loop les injecte dans les messages.

4. **Le client reçoit les images via Socket.IO** - pas besoin de routes séparées,
   les images sont inline (base64) dans les events sur le namespace `/events`.

---

## 1. Image Store (stockage)

### Nouveau composant : `ImageStore`

```
packages/digitorn/core/image_store.py
```

```python
class ImageStore:
    """Stocke les images sur disque, retourne des références légères."""
    
    def __init__(self, base_dir: Path):
        self._base_dir = base_dir  # ~/.digitorn/images/
    
    async def store(self, data: bytes, mime: str, session_id: str) -> ImageRef:
        """Stocke une image, retourne une référence."""
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
        """Récupère les bytes d'une image."""
        ...
    
    async def get_base64(self, image_id: str, session_id: str) -> str | None:
        """Récupère en base64 (pour injection LLM)."""
        ...
    
    def cleanup_session(self, session_id: str):
        """Supprime toutes les images d'une session."""
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

### Pourquoi pas base64 dans les messages ?

Un screenshot PNG = 500KB-2MB en base64. Sur 10 tours avec 3 images chacun :
- Base64 dans messages = 30MB en mémoire × envoyé à chaque appel LLM
- Référence + injection on-demand = quelques KB en mémoire

### Stratégie d'injection

| Tour | Images du tour | Images des tours précédents |
|------|:-:|:-:|
| Tour actuel | base64 complet (haute résolution) | - |
| Tour N-1 | base64 basse résolution (resized 512px) | - |
| Tour N-2+ | Texte : "[Image: screenshot of login page, 1920x1080]" | - |

Ça garde le contexte léger tout en donnant au LLM la vision sur les images récentes.

---

## 2. Message Format (multimodal)

### ContentBlock

```python
@dataclass
class ContentBlock:
    type: str  # "text", "image", "image_ref"
    
    # Pour type="text"
    text: str = ""
    
    # Pour type="image" (inline base64)
    image_data: str = ""  # base64
    media_type: str = ""  # "image/png"
    
    # Pour type="image_ref" (référence à l'image store)
    image_id: str = ""
    alt_text: str = ""  # description textuelle pour le contexte
```

### Messages multimodaux

```python
# Avant (texte seul)
{"role": "user", "content": "Fix this bug"}

# Après (multimodal)
{"role": "user", "content": [
    {"type": "text", "text": "Fix this bug, here's the screenshot:"},
    {"type": "image_ref", "image_id": "abc123", "alt_text": "Screenshot of error page"}
]}
```

Le `content` peut être soit un `str` (backward compatible) soit une `list[ContentBlock]`.

---

## 3. Route API - Upload d'images

### Modifier `/messages` pour accepter multipart

```
POST /api/apps/{appId}/sessions/{sessionId}/messages
Content-Type: multipart/form-data

Fields:
  message: "Fix this bug"           (text)
  images[]: file1.png               (file, 0-N images)
  images[]: file2.jpg               (file)
  workspace: "/path/to/project"     (text, optional)
```

Ou en JSON avec base64 (pour les clients qui préfèrent) :

```
POST /api/apps/{appId}/sessions/{sessionId}/messages
Content-Type: application/json

{
  "message": "Fix this bug",
  "images": [
    {"data": "iVBOR...", "mime": "image/png", "name": "screenshot.png"}
  ],
  "workspace": "/path/to/project"
}
```

### Limites

| Paramètre | Valeur | Configurable |
|-----------|--------|:---:|
| Max images par message | 10 | Oui (`images.max_per_message`) |
| Max taille par image | 10MB | Oui (`images.max_size_bytes`) |
| Formats acceptés | PNG, JPEG, WebP, GIF | Non |
| Max total images par session | 100 | Oui (`images.max_per_session`) |

---

## 4. LLM Provider - Conversion multimodale

### Anthropic Provider

```python
# Convertir les content blocks au format Anthropic
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
            # Résoudre la référence → base64
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
                # Image expirée → injecter description textuelle
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

Le provider détecte automatiquement si le modèle supporte la vision.

---

## 5. Tools - Images en entrée et en sortie

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

Dans `_append_tool_result`, quand le résultat contient une image :

```python
def _append_tool_result(ctx, messages, call_id, tool_name, result, ok, cb):
    # ... sérialisation texte normale ...
    
    # Si le résultat contient une image, l'injecter comme content block
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

Le daemon émet les events images sur le namespace Socket.IO `/events`, room
`session:{session_id}`. Les images arrivent dans les envelopes `tool_call`
avec `image_data` (base64) + `image_mime` ajoutés au payload.

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

### Nouveau event : image_message (pour les images dans les réponses)

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

`GET /sessions/{sid}/history` retourne les images comme références :

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

### Route pour récupérer une image

```
GET /api/apps/{appId}/sessions/{sessionId}/images/{imageId}
Response: image/png (bytes)
```

Le client charge les images à la demande (lazy loading) au lieu de tout
recevoir dans le JSON d'historique.

---

## 8. Optimisation du contexte

### Problème

Une image base64 de 1920x1080 PNG ≈ 1-2MB ≈ 500K tokens estimés.
Si chaque message a une image, le contexte explose en 3 tours.

### Solution : Image Aging

```python
class ImageContextManager:
    """Gère quelles images sont injectées en base64 dans les messages LLM."""
    
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
                        # Tour actuel → haute résolution
                        blocks.append(_resolve_full(block))
                    elif age <= 2:
                        # 1-2 tours → basse résolution (512px)
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

### Tailles estimées

| Stratégie | Taille par image | Tokens estimés |
|-----------|:---:|:---:|
| Haute résolution (1920px) | 1-2 MB | ~300K |
| Basse résolution (512px) | 50-100 KB | ~30K |
| Texte description | 50-100 chars | ~25 |

Avec image aging : 1 image full + 2 low-res + N descriptions = ~360K tokens max.
Sans : N images full = N × 300K = explosion.

---

## 9. Config

Nouveaux paramètres dans `~/.digitorn/config.yaml` :

```yaml
images:
  max_per_message: 10           # Max images par message
  max_size_bytes: 10485760      # 10MB par image
  max_per_session: 100          # Max images par session
  storage_dir: ""               # Vide = ~/.digitorn/images/
  low_res_size: 512             # Taille pour les images anciennes (px)
  aging_full_turns: 1           # Tours avec image haute résolution
  aging_low_turns: 2            # Tours avec image basse résolution
  cleanup_after_days: 7         # Supprimer les images après N jours
```
---

## 10. YAML App Config

```yaml
agents:
  - id: main
    brain:
      provider: anthropic
      model: claude-sonnet-4-5
      vision: true              # Activer le support vision (défaut: auto-detect)
```
Si `vision: false` ou modèle sans vision → les images sont converties en
descriptions textuelles automatiquement.

---

## 11. Compatibilité providers

| Provider | Vision | Format |
|----------|:---:|--------|
| Claude (Anthropic) | Oui | `{"type": "image", "source": {"type": "base64", ...}}` |
| GPT-4o (OpenAI) | Oui | `{"type": "image_url", "image_url": {"url": "data:..."}}` |
| GPT-4o-mini | Oui | Même format |
| DeepSeek-chat (V3) | Non | Converti en texte `[Image: ...]` |
| DeepSeek-VL | Oui | Format OpenAI-compat |
| Ollama (llava) | Oui | `{"images": ["base64..."]}` (format spécial) |
| Ollama (text-only) | Non | Converti en texte |

La détection est automatique via le provider. Chaque provider sait
si son modèle supporte la vision.

---

## 12. Implémentation - Ordre de priorité

### Phase 1 (V1 - démo)
1. Route `/messages` accepte des images (multipart + JSON base64)
2. ImageStore basique (stockage disque)
3. Anthropic provider : injection base64 dans les messages
4. Socket.IO event avec image_data pour le client
5. Client web : upload/paste + affichage inline

### Phase 2 (V1.1)
6. OpenAI-compat provider : conversion format
7. Filesystem.read supporte les images
8. Image aging (context optimization)
9. Route GET /images/{id} pour lazy loading

### Phase 3 (V2)
10. Browser.screenshot → image dans le contexte
11. Presentation module → slides as images
12. Image generation tools (DALL-E, Stable Diffusion via MCP)
13. Files API Anthropic (upload une fois, référence par file_id)

---

## 13. Ce que Digitorn fera MIEUX que Claude Code

| Feature | Claude Code | Digitorn |
|---------|:---:|:---:|
| User paste image | Oui (Cmd+V) | Oui (paste + upload + URL) |
| Read image from disk | Non (bug) | Oui (filesystem.read) |
| Agent screenshot | Non | Oui (browser.screenshot) |
| Image in tool results | Non | Oui (metadata.image_data) |
| Multi-image par message | Limité | 10 images max |
| Image aging (context) | Non | Oui (full → low-res → text) |
| Provider fallback sans vision | Non | Oui (texte automatique) |
| Image persistence | Non | Oui (ImageStore + /images/{id}) |

Sources :
- [Claude Vision API](https://platform.claude.com/docs/en/build-with-claude/vision)
- [OpenAI Images and Vision](https://developers.openai.com/api/docs/guides/images-vision)
- [Claude Code image issue #35866](https://github.com/anthropics/claude-code/issues/35866)
- [Using images in Claude Code](https://amanhimself.dev/blog/using-images-in-claude-code/)
