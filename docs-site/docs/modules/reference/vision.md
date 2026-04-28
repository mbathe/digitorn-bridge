---

id: vision
title: Vision Module (OmniParser)
sidebar_position: 17
format: md
---




# vision (OmniParser)

Screen perception powered by Microsoft OmniParser v2. Parses screenshots into structured lists of UI elements with labels, types, bounding boxes, and confidence scores.

| Property | Value |
|----------|-------|
| **Module ID** | `vision` |
| **Version** | `2.0.0` |
| **Type** | perception |
| **Platforms** | Linux, macOS, Windows |
| **Dependencies** | `torch`, `torchvision`, `ultralytics` (YOLO), `transformers` (Florence-2), `paddleocr` or `easyocr` |
| **Declared Permissions** | `perception.capture` |

---


## Architecture

```
Screenshot (PIL.Image or file path)
    |
    v
YOLO v8 Detection ──────── Fine-tuned on 67K screenshots
    |                        Detects: buttons, icons, text fields, checkboxes, etc.
    v
Florence-2 Captioning ──── Generates descriptive labels for detected icons
    |
    v
PaddleOCR / EasyOCR ────── Extracts text content from screen regions
    |
    v
VisionParseResult
    ├── elements: list[VisionElement]
    ├── metadata: dict (resolution, backend, timing)
    └── timestamp: float
```

---


## Actions (4)

### parse_screen

Parse a screenshot into UI elements.

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `screenshot_path` | string | No | `null` | Path to screenshot image |
| `screenshot_b64` | string | No | `null` | Base64-encoded screenshot |
| `capture` | boolean | No | `false` | Capture current screen |

At least one source must be provided. If `capture=true`, takes a live screenshot.

**Returns**:
```json
{
  "elements": [
    {
      "label": "File",
      "type": "menu_item",
      "bounds": [10, 5, 40, 20],
      "confidence": 0.97,
      "text": "File"
    },
    {
      "label": "search icon",
      "type": "icon",
      "bounds": [200, 50, 24, 24],
      "confidence": 0.89,
      "text": null
    }
  ],
  "metadata": {
    "resolution": [1920, 1080],
    "backend": "omniparser",
    "detection_ms": 450,
    "ocr_ms": 200,
    "total_ms": 680,
    "element_count": 42
  }
}
```

### capture_and_parse

Capture a screenshot and parse it in one step. Utilizes caching and speculative prefetch.

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `region` | object | No | `null` | Capture region `{x, y, width, height}` |

**Performance**:
- First call: ~4s (GPU) or ~12s (CPU)
- Cached call: <1ms (MD5 hash match)
- After prefetch: <1ms (background parse completed)

### find_element

Find a specific UI element by label, type, or description.

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `label` | string | No | `null` | Element label to find |
| `element_type` | string | No | `null` | Element type (`button`, `text`, `icon`, etc.) |
| `description` | string | No | `null` | Natural language description |

**Returns**: Matching `VisionElement` or `null` if not found.

### get_screen_text

Extract all visible text from the screen.

**Returns**: `{"text": "File Edit View ...", "regions": [{"text": "File", "bounds": [10, 5, 40, 20]}, ...]}`

---


## Streaming Support

All 4 actions are decorated with `@streams_progress` and emit real-time events via SSE (`GET /plans/{plan_id}/stream`):

| Action | Status Phases | Progress Pattern |
|--------|---------------|------------------|
| `parse_screen` | `loading_screenshot` → `parsing_vision_pipeline` | 100% on completion |
| `capture_and_parse` | `capturing_screen` → `parsing_vision_pipeline` | 100% on completion |
| `find_element` | `loading_screenshot` → `parsing_vision_pipeline` → `searching` | 100% on completion |
| `get_screen_text` | `loading_screenshot` → `parsing_vision_pipeline` | 100% on completion |

See [Decorators Reference - @streams_progress](../../annotators/decorators.md) for SDK consumption details.

---


## VisionElement

Each detected UI element is represented as:

| Field | Type | Description |
|-------|------|-------------|
| `label` | string | Descriptive label (from YOLO + Florence-2) |
| `type` | string | Element type: `button`, `icon`, `text`, `input`, `checkbox`, `dropdown`, `menu_item`, `link`, etc. |
| `bounds` | array | Bounding box `[x, y, width, height]` |
| `confidence` | float | Detection confidence (0.0 to 1.0) |
| `text` | string or null | OCR-extracted text content |

---


## Configuration

```python
class VisionConfig(ModuleConfigBase):
    cache_max_entries: int = 5          # LRU cache size
    cache_ttl_seconds: float = 2.0     # Cache time-to-live
    speculative_prefetch: bool = True   # Background parse after actions
```

**Environment variables**:

| Variable | Default | Description |
|----------|---------|-------------|
| `LLMOS_OMNIPARSER_MODEL_DIR` | `~/.cache/omniparser` | Model weights directory |
| `LLMOS_OMNIPARSER_DEVICE` | `auto` | `auto`, `cpu`, `cuda`, `mps` |
| `LLMOS_OMNIPARSER_BOX_THRESHOLD` | `0.05` | Detection confidence threshold |
| `LLMOS_OMNIPARSER_IOU_THRESHOLD` | `0.1` | Non-maximum suppression IOU |

---


## PerceptionCache

The cache uses MD5 hashing of screenshot bytes as the key. When the screen has not changed, subsequent parses return instantly from cache.

```
parse_screen(screenshot)
    |
    +--→ md5(screenshot_bytes) → cache key
    |
    +--→ Cache hit? → return cached VisionParseResult
    |
    +--→ Cache miss → run full YOLO + Florence-2 + OCR pipeline
         |
         +--→ Store result in LRU cache (max 5 entries, TTL 2s)
```

---


## Model Loading

Models are lazy-loaded on first use and cached in memory:

1. **YOLO v8** - Custom fine-tuned weights for UI element detection
2. **Florence-2** - Microsoft's vision-language model for icon captioning
3. **PaddleOCR** - Default OCR backend (fast, accurate for screen text)

Models are automatically downloaded from HuggingFace (`microsoft/OmniParser-v2.0`) on first use.

---


## Implementation Notes

- Lazy model loading: models load only when first action is called
- Device auto-detection: CUDA > MPS > CPU
- Non-maximum suppression (NMS) merges overlapping detections
- Box annotation: optional debug overlay showing detection boxes on screenshot
- Thread-safe: model inference runs in dedicated threads via `asyncio.to_thread()`
