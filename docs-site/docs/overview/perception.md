---

id: perception
title: Perception System
sidebar_position: 5
format: md
---



# Perception System

The perception system gives LLMOS Bridge "eyes" - the ability to see, understand, and react to what's on the screen. It encompasses screenshot capture, OCR text extraction, AI-powered UI element detection (OmniParser), hierarchical scene understanding (SceneGraph), intelligent caching, and speculative prefetching.

---


## Architecture

```
                    ┌─────────────────────────────────────────────┐
                    │              Perception System               │
                    └─────────────────────────────────────────────┘
                                        |
              ┌─────────────────────────┼─────────────────────────┐
              |                         |                         |
    ┌─────────────────┐    ┌──────────────────────┐    ┌─────────────────┐
    │  Screen Capture  │    │    Vision Backends    │    │  Perception      │
    │  (mss/pyautogui) │    │                      │    │  Pipeline        │
    └─────────────────┘    │  ┌────────────────┐  │    │  (capture→OCR→   │
              |             │  │  OmniParser v2 │  │    │   diff→validate) │
              v             │  │  (YOLO+Flor+OCR│  │    └─────────────────┘
    ┌─────────────────┐    │  └────────────────┘  │
    │  OCR Engine      │    │  ┌────────────────┐  │
    │  (pytesseract)   │    │  │  Ultra Backend  │  │
    └─────────────────┘    │  │  (SoM+Grounding)│  │
                            │  └────────────────┘  │
                            └──────────────────────┘
                                        |
              ┌─────────────────────────┼─────────────────────────┐
              |                         |                         |
    ┌─────────────────┐    ┌──────────────────────┐    ┌─────────────────┐
    │ Perception Cache │    │    Scene Graph        │    │  Speculative     │
    │ (MD5+LRU+TTL)   │    │    Builder            │    │  Prefetcher      │
    └─────────────────┘    └──────────────────────┘    └─────────────────┘
```

---


## Screen Capture

### Screenshot

| Field | Type | Description |
|-------|------|-------------|
| `image` | PIL.Image | Captured screenshot |
| `width` | int | Image width |
| `height` | int | Image height |
| `timestamp` | float | Capture timestamp |
| `monitor` | int | Monitor index |

### ScreenCapture

| Method | Description |
|--------|-------------|
| `capture()` | Capture primary monitor |
| `capture_to_file(path)` | Capture and save to file |
| `to_base64(screenshot)` | Convert to base64 string |
| `save(screenshot, path)` | Save to file |

Uses `mss` (fast multi-monitor capture) with `pyautogui` fallback.

---


## OCR Engine

### OCRResult

| Field | Type | Description |
|-------|------|-------------|
| `text` | string | Extracted text |
| `confidence` | float | Average confidence |
| `regions` | list | Text regions with bounds |

### OCREngine

| Method | Description |
|--------|-------------|
| `extract(image)` | Extract text from PIL.Image |

Uses `pytesseract` (Tesseract OCR binary required).

---


## Vision Backends

### BaseVisionModule

Abstract base for vision backends:

| Method | Description |
|--------|-------------|
| `parse_screen(params)` | Parse screenshot into VisionElement list |
| `capture_and_parse(params)` | Capture + parse in one step |
| `find_element(params)` | Find specific element |
| `get_screen_text(params)` | Extract all text |

### VisionElement

| Field | Type | Description |
|-------|------|-------------|
| `label` | string | Descriptive label |
| `type` | string | Element type (button, icon, text, input, etc.) |
| `bounds` | list | Bounding box `[x, y, width, height]` |
| `confidence` | float | Detection confidence (0.0 to 1.0) |
| `text` | string or null | OCR-extracted text content |

### VisionParseResult

| Field | Type | Description |
|-------|------|-------------|
| `elements` | list[VisionElement] | Detected UI elements |
| `metadata` | dict | Backend info, timing, resolution |
| `timestamp` | float | Parse timestamp |

### OmniParser Backend (MODULE_ID: `vision`)

Default vision backend powered by Microsoft OmniParser v2:

```
Screenshot input
    |
    v
YOLO v8 Detection ────── 67K fine-tuned screenshots
    |                      Detects buttons, icons, text fields, checkboxes, etc.
    v
Florence-2 Captioning ── Generates descriptive labels for detected icons
    |
    v
PaddleOCR / EasyOCR ──── Text extraction from screen regions
    |
    v
Non-Maximum Suppression ─ Merge overlapping detections (IOU threshold)
    |
    v
VisionParseResult
```

**Model loading**: Lazy-loaded on first use. Models auto-download from HuggingFace (`microsoft/OmniParser-v2.0`).

**Device selection**: CUDA > MPS > CPU (auto-detected, overridable via `LLMOS_OMNIPARSER_DEVICE`).

**Configuration**:

| Variable | Default | Description |
|----------|---------|-------------|
| `LLMOS_OMNIPARSER_MODEL_DIR` | `~/.cache/omniparser` | Model weights directory |
| `LLMOS_OMNIPARSER_DEVICE` | `auto` | Compute device |
| `LLMOS_OMNIPARSER_BOX_THRESHOLD` | `0.05` | Detection confidence threshold |
| `LLMOS_OMNIPARSER_IOU_THRESHOLD` | `0.1` | NMS IOU threshold |

### Ultra Backend (Advanced SoM)

Advanced Set-of-Marks vision backend with separate detection, grounding, and OCR stages:

```
packages/llmos-bridge/llmos_bridge/modules/perception_vision/ultra/
    ├── module.py          ← UltraVisionModule
    ├── classifier.py      ← Element type classifier
    ├── som.py             ← Set-of-Marks overlay generator
    ├── weight_manager.py  ← Model weight downloading and caching
    └── backends/
        ├── detector.py    ← Object detection backend
        ├── grounder.py    ← Visual grounding backend
        └── ocr.py         ← OCR backend
```

The Ultra backend provides:
- Separate detection and grounding stages for higher accuracy
- Set-of-Marks (SoM) overlay for visual debugging
- Pluggable sub-backends for each stage
- Weight management with automatic downloading

---


## Scene Graph

Transforms a flat list of VisionElements into a hierarchical representation of the UI:

### RegionType

| Type | Detection Heuristic |
|------|---------------------|
| `TASKBAR` | Bottom 5% of screen |
| `TITLE_BAR` | Top 4% of screen |
| `SIDEBAR` | Left 25% (if sufficient elements) |
| `TOOLBAR` | Horizontal cluster of buttons near top |
| `FORM` | Cluster of input elements |
| `CONTENT` | Main content area (default) |
| `DIALOG` | Modal overlay (centered, small bounding box) |
| `MENU` | Vertical list of menu items |
| `STATUS_BAR` | Bottom bar with status text |

### ScreenRegion

| Field | Type | Description |
|-------|------|-------------|
| `type` | RegionType | Region classification |
| `bounds` | tuple | Bounding box |
| `elements` | list | VisionElements in this region |
| `children` | list | Nested sub-regions |
| `focused` | bool | Whether this region has focus |

### SceneGraph

| Field | Type | Description |
|-------|------|-------------|
| `regions` | list[ScreenRegion] | Top-level regions |
| `resolution` | tuple | Screen resolution |
| `timestamp` | float | Generation timestamp |

### SceneGraphBuilder

| Method | Description |
|--------|-------------|
| `build(elements, resolution)` | Build scene graph from flat elements |

**Compact text output**:
```
[WINDOW: Firefox] (focused)
  [TOOLBAR]
    button: "Back" [INTERACTABLE]
    button: "Forward" [INTERACTABLE]
    input: "https://example.com" [INTERACTABLE]
  [CONTENT]
    text: "Welcome to Example"
    link: "Click here" [INTERACTABLE]
  [TASKBAR]
    button: "Activities"
```

Performance: ~5-15ms CPU, purely heuristic (no ML).

---


## Perception Cache

MD5-based LRU cache for parsed vision results. When the screen has not changed, subsequent parses return instantly.

### PerceptionCache

| Method | Description |
|--------|-------------|
| `get(screenshot_bytes)` | Look up cached result by MD5 hash |
| `put(screenshot_bytes, result)` | Store result in cache |
| `invalidate()` | Clear all cache entries |
| `stats()` | Cache hit/miss statistics |

**Algorithm**:
```
1. Compute MD5(screenshot_bytes) → cache_key
2. Check LRU cache (max_entries, TTL)
3. Hit → return cached VisionParseResult
4. Miss → run full pipeline, store result
```

**Configuration** (via VisionConfig):
- `cache_max_entries`: 5 (LRU eviction)
- `cache_ttl_seconds`: 2.0 (time-based expiry)

---


## Speculative Prefetcher

Background screen parsing triggered after every GUI action. The next `read_screen` call hits the cache instead of waiting for a fresh parse.

```
click_element("Save button") completes
    |
    +--→ asyncio.create_task(vision.capture_and_parse())
    |    (non-blocking, runs in background)
    |
    v
1-4 seconds later:
    |
Agent calls read_screen()
    +--→ Cache hit → instant response (<1ms)
```

**Wired into**: `ComputerControlModule._capture_and_parse()`, triggered after click/type/interact actions.

**Configuration**: `speculative_prefetch: true` (default enabled).

**Impact**: Saves ~4s per iteration in typical GUI automation loops.

---


## Perception Pipeline

The `PerceptionPipeline` handles before/after screenshots around action execution, used when `IMLAction.perception` is configured.

### ActionPerceptionResult

| Field | Type | Description |
|-------|------|-------------|
| `before_screenshot` | string | Base64 screenshot before action |
| `after_screenshot` | string | Base64 screenshot after action |
| `before_text` | string | OCR text before action |
| `after_text` | string | OCR text after action |
| `diff` | dict | Detected changes |
| `validation` | dict | Output validation result |

### PerceptionPipeline

| Method | Description |
|--------|-------------|
| `capture_before(action)` | Capture pre-action state |
| `run_after(action, before_result)` | Capture post-action state and diff |
| `run(action)` | Full before + after pipeline |

**Diff detection**: Compares before/after OCR text to identify what changed on screen.

**Output validation**: When `perception.validate_output` is set (JSONPath expression), the pipeline validates the action result against expected screen state.

**Result injection**: Perception data is stored under the reserved `_perception` key:
```
{{result.action_id._perception.after_text}}
{{result.action_id._perception.before_screenshot}}
```

---


## Integration Points

### computer_control Module

The `computer_control` module orchestrates perception + physical GUI actions:
1. Calls `vision.capture_and_parse()` to detect elements
2. Uses `SceneGraphBuilder` for hierarchical understanding
3. Resolves natural language descriptions to pixel coordinates
4. Calls `gui.click_position()` / `gui.type_text()` for physical interaction
5. Triggers speculative prefetch after each action

### Executor Integration

The `PlanExecutor` integrates perception when actions have `PerceptionConfig`:
1. `capture_before=true` → `pipeline.capture_before()`
2. Execute action via module
3. `capture_after=true` → `pipeline.run_after()`
4. Store under `_perception` key in execution results
5. Templates can access: `{{result.X._perception.after_text}}`

### SDK Integration

The `ReactivePlanLoop` includes scene graph in observations:
- Vision elements are included in the agent's observation text
- Scene graph provides spatial context for reasoning
- Progress log shows perception timing
