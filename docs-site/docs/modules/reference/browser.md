---

id: browser
title: Browser Module
sidebar_position: 8
format: md
---




# browser

Web browser automation powered by Playwright. Supports Chromium, Firefox, and WebKit with headless and GUI modes.

| Property | Value |
|----------|-------|
| **Module ID** | `browser` |
| **Version** | `1.0.0` |
| **Type** | automation |
| **Platforms** | Linux, macOS, Windows |
| **Dependencies** | `playwright` (optional) |
| **Declared Permissions** | `browser` |

---


## Actions

### open_browser

Launch a browser instance.

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `browser_type` | string | No | `"chromium"` | `chromium`, `firefox`, `webkit` |
| `headless` | boolean | No | `true` | Run without GUI |
| `viewport_width` | integer | No | `1280` | Browser viewport width |
| `viewport_height` | integer | No | `720` | Browser viewport height |
| `proxy` | string | No | `null` | Proxy URL |
| `locale` | string | No | `null` | Browser locale (e.g., `"en-US"`) |
| `timezone` | string | No | `null` | Timezone (e.g., `"America/New_York"`) |

**Security**:
- `@requires_permission(Permission.BROWSER)`
- `@audit_trail("standard")`

### close_browser

Close the browser session and release resources.

### navigate_to

Navigate to a URL.

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `url` | string | Yes | - | Target URL |
| `wait_until` | string | No | `"load"` | `load`, `domcontentloaded`, `networkidle`, `commit` |
| `timeout` | integer | No | `30000` | Navigation timeout in milliseconds |

**Security**: `@requires_permission(Permission.BROWSER)`

### click_element

Click a page element.

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `selector` | string | Yes | - | CSS selector or XPath |
| `button` | string | No | `"left"` | `left`, `right`, `middle` |
| `click_count` | integer | No | `1` | Number of clicks |
| `delay` | integer | No | `0` | Delay between clicks (ms) |

**Security**: `@requires_permission(Permission.BROWSER)`

### fill_input

Fill a text input or textarea.

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `selector` | string | Yes | - | Input element selector |
| `value` | string | Yes | - | Text to fill |

### submit_form

Click submit button and wait for navigation.

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `selector` | string | No | `null` | Submit button selector (auto-detected if null) |
| `wait_until` | string | No | `"load"` | Navigation wait condition |

### select_option

Select option(s) in a `<select>` element.

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `selector` | string | Yes | - | Select element selector |
| `values` | array | Yes | - | Values to select |

### get_element_text

Get the text content of an element.

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `selector` | string | Yes | - | Element selector |

**Returns**: `{"text": "...", "selector": "..."}`

### get_page_content

Extract page content as HTML, text, or within a selector.

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `format` | string | No | `"text"` | `html`, `text`, `markdown` |
| `selector` | string | No | `null` | Limit to element matching selector |

### take_screenshot

Capture a screenshot of the page.

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `path` | string | No | `null` | Save to file path |
| `full_page` | boolean | No | `false` | Capture full scrollable page |

**Returns**: `{"path": "...", "screenshot_b64": "..."}`

### execute_script

Execute JavaScript in the page context.

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `script` | string | Yes | - | JavaScript code |
| `args` | array | No | `[]` | Arguments passed to script |

**Security**:
- `@requires_permission(Permission.BROWSER)`
- `@sensitive_action(RiskLevel.HIGH)`
- `@audit_trail("detailed")`

### wait_for_element

Wait for an element to appear, disappear, or change visibility.

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `selector` | string | Yes | - | Element selector |
| `state` | string | No | `"visible"` | `attached`, `detached`, `visible`, `hidden` |
| `timeout` | integer | No | `30000` | Wait timeout (ms) |

### download_file

Download a file via the browser.

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `url` | string | No | `null` | Direct URL (or trigger via click) |
| `selector` | string | No | `null` | Click this element to trigger download |
| `output_path` | string | Yes | - | Local save path |

---


## Streaming Support

4 actions are decorated with `@streams_progress` and emit real-time events via SSE (`GET /plans/{plan_id}/stream`):

| Action | Status Phases | Progress Pattern |
|--------|---------------|------------------|
| `navigate_to` | `navigating` | 100% on page load |
| `submit_form` | `submitting` → `waiting_for_navigation` | 100% on completion |
| `download_file` | `downloading` | 100% on completion |
| `wait_for_element` | `waiting` | % based on elapsed/timeout |

Fast actions (`click_element`, `fill_input`, `get_page_content`, etc.) are not streaming-enabled as they complete near-instantly.

See [Decorators Reference - @streams_progress](../../annotators/decorators.md) for SDK consumption details.

---


## Implementation Notes

- Per-session `asyncio.Lock` for concurrency safety
- Browser instance is reused across actions within a session
- Lazy cleanup: browser is closed when module stops or session times out
- `playwright` is an optional dependency - the module gracefully reports unavailability if not installed
