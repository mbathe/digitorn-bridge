---

id: computer_control
title: Computer Control Module
sidebar_position: 10
format: md
---




# computer_control

Semantic GUI automation that bridges vision perception with physical GUI actions. Instead of specifying pixel coordinates, describe what you want to interact with in natural language.

| Property | Value |
|----------|-------|
| **Module ID** | `computer_control` |
| **Version** | `1.0.0` |
| **Type** | automation |
| **Platforms** | Linux, macOS, Windows |
| **Dependencies** | Requires `vision` and `gui` modules to be registered |
| **Declared Permissions** | `keyboard`, `perception.capture` |

---


## How It Works

```
Agent: "Click the Save button"
    |
    v
computer_control.click_element(description="Save button")
    |
    +--→ vision.capture_and_parse()          # Screen → VisionElement[]
    |
    +--→ ElementResolver.find(description)    # NL → matching element
    |
    +--→ gui.click_position(x, y)            # Click center of element bounds
    |
    v
Return: element details + click confirmation
```

The module dynamically looks up the `vision` and `gui` modules from the registry at runtime. It does not import them directly — they are resolved through `ModuleRegistry.get()`.

---


## Actions (9)

### read_screen

Capture and parse the current screen state. Returns structured elements with labels, types, and bounding boxes.

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `region` | object | No | `null` | Capture specific region `{x, y, width, height}` |
| `include_scene_graph` | boolean | No | `true` | Include hierarchical scene graph |

**Returns**:
```json
{
  "elements": [
    {"label": "Save", "type": "button", "bounds": [100, 200, 80, 30], "confidence": 0.95},
    {"label": "File name:", "type": "text", "bounds": [50, 150, 100, 20]}
  ],
  "scene_graph": {
    "regions": [
      {"type": "TOOLBAR", "elements": ["Save", "Open", "Close"]},
      {"type": "FORM", "elements": ["File name:", "input field"]}
    ]
  }
}
```

**Security**:
- `@rate_limited(calls_per_minute=30)`

**Performance**: Uses `PerceptionCache` (MD5 hash of screenshot bytes) to avoid redundant vision parsing. Cache TTL: 2 seconds.

### click_element

Click a UI element identified by natural language description.

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `description` | string | Yes | — | Natural language element description |
| `button` | string | No | `"left"` | Mouse button |
| `double_click` | boolean | No | `false` | Double-click |

**Security**: `@requires_permission(Permission.KEYBOARD)`

### type_into_element

Type text into a UI element identified by description.

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `description` | string | Yes | — | Element description |
| `text` | string | Yes | — | Text to type |
| `clear_first` | boolean | No | `true` | Clear field before typing |

**Security**:
- `@requires_permission(Permission.KEYBOARD)`
- `@audit_trail("standard")`

### wait_for_element

Wait for an element to appear on screen.

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `description` | string | Yes | — | Element description |
| `timeout` | integer | No | `30` | Maximum wait in seconds |
| `poll_interval` | float | No | `1.0` | Check interval in seconds |

### find_and_interact

Find an element and perform an action in one step.

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `description` | string | Yes | — | Element description |
| `action` | string | Yes | — | `click`, `type`, `scroll`, `hover` |
| `text` | string | No | `null` | Text for type action |
| `scroll_amount` | integer | No | `null` | Pixels for scroll action |

**Security**: `@sensitive_action(RiskLevel.MEDIUM)`

### get_element_info

Get detailed information about a UI element.

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `description` | string | Yes | — | Element description |

**Returns**: `{"label": "Save", "type": "button", "bounds": [100, 200, 80, 30], "center": [140, 215], "confidence": 0.95}`

### move_to_element

Move the mouse cursor to an element without clicking.

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `description` | string | Yes | — | Element description |

### scroll_to_element

Scroll until an element is visible.

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `description` | string | Yes | — | Element description |
| `max_scrolls` | integer | No | `10` | Maximum scroll attempts |

### execute_gui_sequence

Execute multiple GUI actions in sequence.

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `steps` | array | Yes | — | Array of `{action, description, text, ...}` |

**Security**: `@sensitive_action(RiskLevel.HIGH)` — multiple actions in sequence amplify risk.

---


## Streaming Support

All 9 actions are decorated with `@streams_progress` and emit real-time events via the SSE endpoint (`GET /plans/{plan_id}/stream`). Status transitions vary by action type:

| Action | Status Phases | Progress Pattern |
|--------|---------------|------------------|
| `click_element` | `capturing_screen` → `resolving_element` | 100% on completion |
| `type_into_element` | `capturing_screen` → `resolving_element` → `typing` | 100% on completion |
| `wait_for_element` | `polling` | % based on elapsed/timeout |
| `read_screen` | `capturing_screen` → `parsing` | 100% on completion |
| `find_and_interact` | `capturing_screen` → `resolving_element` → `interacting` | 100% on completion |
| `get_element_info` | `capturing_screen` → `resolving_element` | 100% on completion |
| `execute_gui_sequence` | `executing_step` | % based on step/total |
| `move_to_element` | `capturing_screen` → `resolving_element` | 100% on completion |
| `scroll_to_element` | `scrolling` | % based on scroll attempt/max |

See [Decorators Reference — @streams_progress](../../annotators/decorators.md) for SDK consumption details.

---


## SpeculativePrefetcher

After every click, type, or interaction action, the module triggers a background screen parse. This means the next `read_screen` call is likely to hit the cache, saving ~4 seconds of vision parsing time per iteration.

```
click_element("Save") completes
    |
    +--→ Immediately trigger background: vision.capture_and_parse()
    |    (runs in asyncio.create_task, non-blocking)
    |
    v
Agent's next read_screen() → cache hit → instant response
```

---


## Scene Graph

The `read_screen` action includes a hierarchical scene graph that organizes flat vision elements into semantic regions:

| Region Type | Detection Heuristic |
|-------------|---------------------|
| `TASKBAR` | Bottom 5% of screen |
| `TITLE_BAR` | Top 4% of screen |
| `SIDEBAR` | Left 25% (if enough elements) |
| `TOOLBAR` | Horizontal cluster of buttons near top |
| `FORM` | Cluster of input elements |
| `CONTENT` | Main content area |
| `DIALOG` | Modal overlay (centered, small) |

The scene graph enables the agent to reason about UI structure: "Click the Save button in the toolbar" vs "Click the Save button in the dialog."
