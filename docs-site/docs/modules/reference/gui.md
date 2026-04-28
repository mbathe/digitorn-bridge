---

id: gui
title: GUI Module
sidebar_position: 9
format: md
---




# gui

Physical GUI automation for desktop interaction. Click, type, scroll, drag, capture screenshots, and extract text via OCR. Uses a multi-strategy keyboard input engine for cross-platform compatibility.

| Property | Value |
|----------|-------|
| **Module ID** | `gui` |
| **Version** | `1.0.0` |
| **Type** | automation |
| **Platforms** | Linux, macOS, Windows |
| **Dependencies** | `pyautogui`. Optional: `pytesseract` (OCR), `opencv-python` (image matching) |
| **Declared Permissions** | `keyboard`, `screen.capture` |

---


## Actions (13)

### Mouse Operations

| Action | Description | Key Parameters |
|--------|-------------|----------------|
| `click_position` | Click at screen coordinates | `x`, `y`, `button` (`left`/`right`/`middle`), `clicks`, `interval` |
| `double_click` | Double-click at position | `x`, `y` |
| `right_click` | Right-click at position | `x`, `y` |
| `click_image` | Find image on screen and click | `image_path`, `confidence`, `timeout`, `retry_interval` |
| `scroll` | Scroll by pixels | `x`, `y`, `amount`, `direction` (`up`/`down`/`left`/`right`) |
| `drag_drop` | Drag from point A to point B | `start_x`, `start_y`, `end_x`, `end_y`, `duration` |

**Security for click_position**:
- `@requires_permission(Permission.KEYBOARD)`
- `@rate_limited(calls_per_minute=120)`

### Keyboard Operations

| Action | Description | Key Parameters |
|--------|-------------|----------------|
| `type_text` | Type text string | `text`, `interval` |
| `key_press` | Press key(s) | `keys` (string or array), `interval` |

Key names follow `pyautogui` conventions: `enter`, `tab`, `escape`, `backspace`, `delete`, `space`, `ctrl`, `alt`, `shift`, `command`, `f1`-`f12`, etc.

### Screen Operations

| Action | Description | Key Parameters |
|--------|-------------|----------------|
| `find_on_screen` | Locate image on screen | `image_path`, `confidence`, `grayscale` |
| `get_screen_text` | Extract text via OCR | `region` (optional), `language` |
| `take_screenshot` | Capture screenshot | `path` (optional), `region` |

### Window Operations

| Action | Description | Key Parameters |
|--------|-------------|----------------|
| `get_window_info` | Get active window info | |
| `focus_window` | Focus window by title | `title` |

---


## TextInputEngine

The `type_text` action uses a multi-strategy input engine for cross-platform keyboard input:

```
Strategy selection priority:
1. Clipboard paste (xdotool/xclip/wl-paste)     - fastest, most reliable
2. xdotool type                                    - X11 native
3. wtype                                           - Wayland native
4. ydotool type                                    - Universal fallback
5. pyautogui typewrite                             - Pure Python fallback
```

The engine automatically detects the display server (X11 or Wayland) and selects the best available strategy. This ensures:
- Layout-agnostic input (works with any keyboard layout)
- Unicode support (including non-ASCII characters)
- Reliability across different Linux environments

---


## Implementation Notes

- All blocking I/O via `asyncio.to_thread()`
- Image matching via OpenCV `matchTemplate()` with configurable confidence threshold
- OCR via `pytesseract` (Tesseract binary must be installed)
- Screen capture via `pyautogui.screenshot()` or `mss` for high-performance capture
- Failsafe: `pyautogui.FAILSAFE = True` (move mouse to corner to abort)
