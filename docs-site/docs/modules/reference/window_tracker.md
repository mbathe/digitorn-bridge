---

id: window_tracker
title: Window Tracker Module
sidebar_position: 11
format: md
---




# window_tracker

Window focus monitoring and context recovery. Tracks the active window, detects context switches, and recovers focus to a target window. Essential for multi-step GUI automation.

| Property | Value |
|----------|-------|
| **Module ID** | `window_tracker` |
| **Version** | `1.0.0` |
| **Type** | system |
| **Platforms** | Linux |
| **Dependencies** | `xdotool`, `wmctrl` (system packages) |
| **Declared Permissions** | `window.manager` |

---


## Why Window Tracking?

During multi-step GUI automation, focus can be lost when:
- A notification steals focus
- A dialog opens in another window
- The user interacts with the desktop
- An application opens a new window

Without tracking, subsequent GUI actions (click, type) go to the wrong window. The window tracker module detects these context switches and recovers focus automatically.

---


## Actions (8)

### get_active_window

Get information about the currently focused window.

**Returns**:
```json
{
  "window_id": "0x04800003",
  "title": "Document.docx - LibreOffice Writer",
  "pid": 12345,
  "class_name": "libreoffice",
  "position": {"x": 0, "y": 0},
  "size": {"width": 1920, "height": 1080},
  "workspace": 0
}
```

### list_windows

List all visible windows.

**Returns**: `{"windows": [WindowInfo, ...]}`

### start_tracking

Begin tracking a target window by title pattern.

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `title_pattern` | string | Yes | - | Regex or substring to match window title |

Starts a background tracking state that monitors whether the target window remains focused.

### stop_tracking

Stop the current tracking session.

### get_tracking_status

Check if the target window is still focused.

**Returns**:
```json
{
  "tracking": true,
  "target_focused": true,
  "target_title": "Document.docx - LibreOffice Writer",
  "current_title": "Document.docx - LibreOffice Writer",
  "context_switches": 3,
  "last_switch_time": "2024-01-15T10:30:00Z"
}
```

### recover_focus

Re-focus the tracked target window.

**Security**: `@requires_permission(Permission.WINDOW_MANAGER)`

### focus_window

Focus a specific window by its window ID.

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `window_id` | string | Yes | - | X11 window ID |

**Security**:
- `@requires_permission(Permission.WINDOW_MANAGER)`
- `@audit_trail("standard")`

### detect_context_switch

Check if the focused window has changed since the last check.

**Returns**: `{"switched": true, "from_title": "...", "to_title": "..."}`

---


## WindowInfo

| Field | Type | Description |
|-------|------|-------------|
| `window_id` | string | X11 window ID (hex) |
| `title` | string | Window title |
| `pid` | integer | Process ID |
| `class_name` | string | Window class |
| `position` | object | `{x, y}` screen position |
| `size` | object | `{width, height}` dimensions |
| `workspace` | integer | Virtual desktop number |

---


## Implementation Notes

- Uses `xdotool` for window ID, title, PID, geometry
- Uses `wmctrl -l` for window listing
- XWayland fallback via `swaymsg` for Wayland compositors
- TrackingState maintained in-memory (not persisted across restarts)
- Context switch counter tracks how often focus was lost during a tracking session
