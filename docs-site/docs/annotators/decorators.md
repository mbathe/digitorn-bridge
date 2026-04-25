---

id: decorators
title: Annotators Reference
sidebar_position: 1
format: md
---



# Annotators Reference

Annotators are Python decorators applied to module action methods. They serve two purposes:

1. **Metadata declaration** — Attach security, streaming, and configuration information to actions at definition time
2. **Runtime enforcement** — When enabled, enforce permissions, rate limits, audit trails, and intent verification at call time

LLMOS Bridge provides four families of annotators:

- **Security annotators** (6) — Permission enforcement, risk classification, rate limiting, audit, data classification, intent verification
- **Cache annotators** (2) — L2 Redis/fakeredis output cache for read actions and write-side invalidation
- **Streaming annotators** (1) — Progress streaming declaration
- **Configuration annotators** (1) — Module configuration schema declaration

---


## Design Principles

### Metadata-First Architecture

All annotators follow the metadata-first pattern: they set function attributes without wrapping the function. This means:

```python
@requires_permission(Permission.FILESYSTEM_WRITE)
async def _action_write_file(self, params: dict) -> dict:
    ...
```

After decoration, `_action_write_file._required_permissions` is `["filesystem.write"]`. The function itself is unchanged — no wrapper, no overhead, no altered signature.

Runtime enforcement is a separate layer that checks these attributes when the module's `SecurityManager` is injected. This design allows:

- **Gradual adoption** — Modules work identically with or without enforcement enabled
- **Zero overhead** — When enforcement is disabled, decorators add no runtime cost
- **Testability** — Tests can run without security infrastructure
- **Introspection** — Manifests and dashboards can read metadata without executing code

### Stacking Order

Security decorators can be freely stacked. Metadata is preserved through all layers via `_copy_metadata()`:

```python
@requires_permission(Permission.FILESYSTEM_DELETE, reason="Permanently removes files")
@sensitive_action(risk_level=RiskLevel.HIGH, irreversible=True)
@rate_limited(calls_per_minute=30)
@audit_trail("detailed")
@data_classification(DataClassification.INTERNAL)
async def _action_delete_file(self, params: dict) -> dict:
    ...
```

**Order does not matter** for metadata attachment. However, when runtime enforcement is enabled, the enforcement order is:

1. `@requires_permission` — Check permissions first (cheapest)
2. `@rate_limited` — Check rate limits
3. `@intent_verified` — Verify intent (most expensive, may call LLM)
4. `@sensitive_action` — Emit security audit event
5. `@audit_trail` — Log before/after
6. `@data_classification` — Tag output classification

### Streaming Metadata Preservation

The `@streams_progress` streaming decorator's metadata is preserved through security decorator stacking. The `_SECURITY_ATTRS` tuple includes `"_streams_progress"`, ensuring it survives `_copy_metadata()` operations:

```python
@requires_permission(Permission.FILE_DOWNLOAD)
@streams_progress
async def _action_download_file(self, params: dict) -> dict:
    stream = params.pop("_stream", None)
    ...
```

---


## Security Annotators

### @requires_permission

Declares OS-level permissions required to execute the action.

**Signature**:

```python
@requires_permission(*permissions: str, reason: str = "")
```

**Parameters**:

| Parameter      | Type | Description                                 |
| -------------- | ---- | ------------------------------------------- |
| `*permissions` | str  | One or more permission strings              |
| `reason`       | str  | Explanation shown in prompts and audit logs |

**Metadata set**:

- `_required_permissions`: `list[str]` — accumulated permission list
- `_permission_reason`: `str` — reason string

**Runtime behavior** (when enforcement enabled):

1. For each permission, calls `permission_manager.check_or_raise(permission)`
2. If any permission is not granted, raises `PermissionDeniedError`
3. LOW risk permissions may be auto-granted (if `auto_grant_low_risk = true`)

**Built-in permission constants** (from `security/permissions.py`):

| Permission                      | String Value           | Description                          |
| ------------------------------- | ---------------------- | ------------------------------------ |
| `Permission.FILESYSTEM_READ`    | `"filesystem.read"`    | Read files and directories           |
| `Permission.FILESYSTEM_WRITE`   | `"filesystem.write"`   | Write and create files               |
| `Permission.FILESYSTEM_DELETE`  | `"filesystem.delete"`  | Delete files and directories         |
| `Permission.PROCESS_EXECUTE`    | `"process.execute"`    | Execute commands and launch apps     |
| `Permission.PROCESS_KILL`       | `"process.kill"`       | Terminate processes                  |
| `Permission.NETWORK_HTTP`       | `"network.http"`       | HTTP requests                        |
| `Permission.FILE_DOWNLOAD`      | `"file.download"`      | Download files                       |
| `Permission.NETWORK_EMAIL`      | `"network.email"`      | Send and read email                  |
| `Permission.DATABASE_READ`      | `"database.read"`      | Read database                        |
| `Permission.DATABASE_WRITE`     | `"database.write"`     | Write to database                    |
| `Permission.BROWSER`            | `"browser"`            | Browser automation                   |
| `Permission.KEYBOARD`           | `"keyboard"`           | Keyboard and mouse input             |
| `Permission.SCREEN_CAPTURE`     | `"screen.capture"`     | Screenshot capture                   |
| `Permission.PERCEPTION_CAPTURE` | `"perception.capture"` | Vision perception                    |
| `Permission.WINDOW_MANAGER`     | `"window.manager"`     | Window focus and management          |
| `Permission.GPIO_READ`          | `"gpio.read"`          | Read GPIO pins                       |
| `Permission.GPIO_WRITE`         | `"gpio.write"`         | Write GPIO pins                      |
| `Permission.ACTUATOR`           | `"actuator"`           | PWM and actuator control             |
| `Permission.ADMIN`              | `"admin"`              | Administrative operations            |
| `Permission.MODULE_READ`        | `"module.read"`        | Read module information              |
| `Permission.MODULE_MANAGE`      | `"module.manage"`      | Manage module lifecycle              |
| `Permission.MODULE_INSTALL`     | `"module.install"`     | Install/uninstall modules            |

Permission strings are extensible — community modules can define custom permissions like `"my_plugin.resource"`.

**Example**:

```python
@requires_permission(
    Permission.FILESYSTEM_WRITE,
    Permission.FILESYSTEM_DELETE,
    reason="Moves files, which requires write + delete on source"
)
async def _action_move_file(self, params: dict) -> dict:
    ...
```

---


### @sensitive_action

Marks an action as sensitive with risk classification and confirmation requirements.

**Signature**:

```python
@sensitive_action(
    risk_level: RiskLevel = RiskLevel.HIGH,
    requires_confirmation: bool = True,
    irreversible: bool = False,
)
```

**Parameters**:

| Parameter               | Type      | Default | Description                                       |
| ----------------------- | --------- | ------- | ------------------------------------------------- |
| `risk_level`            | RiskLevel | `HIGH`  | Risk classification                               |
| `requires_confirmation` | bool      | `True`  | Whether user confirmation is needed (Phase 2)     |
| `irreversible`          | bool      | `False` | Whether action cannot be undone                   |

**Risk Levels**:

| Level                | Value        | Description                           | Auto-Grant          |
| -------------------- | ------------ | ------------------------------------- | ------------------- |
| `RiskLevel.LOW`      | `"LOW"`      | Read-only, informational              | Yes (if configured) |
| `RiskLevel.MEDIUM`   | `"MEDIUM"`   | Write operations, reversible          | No                  |
| `RiskLevel.HIGH`     | `"HIGH"`     | Destructive or impactful              | No                  |
| `RiskLevel.CRITICAL` | `"CRITICAL"` | System-altering, irreversible         | No                  |

**Metadata set**:

- `_sensitive_action`: `True`
- `_risk_level`: `RiskLevel` value
- `_requires_confirmation`: `bool`
- `_irreversible`: `bool`

**Runtime behavior**:

- Emits a security audit event when the action is invoked
- In Phase 2: blocks execution pending user confirmation if `requires_confirmation = True`

**Example**:

```python
@sensitive_action(risk_level=RiskLevel.HIGH, irreversible=True)
async def _action_kill_process(self, params: dict) -> dict:
    """Terminate a process. This action is irreversible."""
    ...
```

---


### @rate_limited

Enforces per-action rate limits.

**Signature**:

```python
@rate_limited(
    calls_per_minute: int | None = None,
    calls_per_hour: int | None = None,
)
```

**Parameters**:

| Parameter          | Type | Default | Description                                     |
| ------------------ | ---- | ------- | ----------------------------------------------- |
| `calls_per_minute` | int  | `None`  | Max calls per minute. `None` = no minute limit. |
| `calls_per_hour`   | int  | `None`  | Max calls per hour. `None` = no hour limit.     |

**Metadata set**:

- `_rate_limit`: `dict` with `calls_per_minute` and/or `calls_per_hour`

**Runtime behavior**:

- Calls `rate_limiter.check_or_raise(module_id, action_name)`
- If rate limit exceeded, raises `RateLimitExceededError`

**Example**:

```python
@rate_limited(calls_per_minute=30)
async def _action_run_command(self, params: dict) -> dict:
    """Execute a system command. Rate limited to 30/min."""
    ...
```

---


### @audit_trail

Adds before/after audit logging to an action.

**Signature**:

```python
@audit_trail(level: str = "standard")
```

**Parameters**:

| Parameter | Type | Default      | Description          |
| --------- | ---- | ------------ | -------------------- |
| `level`   | str  | `"standard"` | Logging detail level |

**Audit Levels**:

| Level        | What is logged                                                                  |
| ------------ | ------------------------------------------------------------------------------- |
| `"minimal"`  | Action invocation only (module, action, timestamp)                              |
| `"standard"` | Invocation + success/failure status                                             |
| `"detailed"` | Invocation + params (truncated) + result (truncated) + status                   |

**Metadata set**:

- `_audit_level`: `str`

**Runtime behavior**:

- Before execution: emits audit event to `llmos.security` topic via EventBus
- After execution: emits success or failure audit event with optional details
- Uses `_safe_summary()` to truncate large params/results (max 200 chars per value, 10 dict keys, 5 list items)

**Example**:

```python
@audit_trail("detailed")
async def _action_execute_query(self, params: dict) -> dict:
    """Execute SQL query. Full audit trail with params and result."""
    ...
```

---


### @data_classification

Declares the data sensitivity level of an action's output.

**Signature**:

```python
@data_classification(classification: DataClassification)
```

**Parameters**:

| Parameter        | Type               | Description                |
| ---------------- | ------------------ | -------------------------- |
| `classification` | DataClassification | Output data classification |

**Data Classifications**:

| Classification                    | Description                                         |
| --------------------------------- | --------------------------------------------------- |
| `DataClassification.PUBLIC`       | No sensitivity restrictions                         |
| `DataClassification.INTERNAL`     | Internal use only, not for external exposure        |
| `DataClassification.CONFIDENTIAL` | Restricted access, requires authorization           |
| `DataClassification.RESTRICTED`   | Highest sensitivity, strict access controls         |

**Metadata set**:

- `_data_classification`: `DataClassification` value

**Runtime behavior** (Phase 1: metadata-only):

- Tags the action output with the classification level
- Enriches the manifest and API responses
- Dashboard can display classification badges

**Example**:

```python
@data_classification(DataClassification.CONFIDENTIAL)
async def _action_read_email(self, params: dict) -> dict:
    """Read email messages. Output classified as CONFIDENTIAL."""
    ...
```

---


### @intent_verified

Marks an action for intent verification via LLM-based analysis.

**Signature**:

```python
@intent_verified(strict: bool = False)
```

**Parameters**:

| Parameter | Type | Default | Description |
| --- | --- | --- | --- |
| `strict` | bool | `False` | `True` = block on verification failure. `False` = log only. |

**Metadata set**:

- `_intent_verified`: `True`
- `_intent_strict`: `bool`

**Runtime behavior** (Phase 2):

- Before execution: calls `IntentVerifier.verify_action()` with the action context
- IntentVerifier sends the action details to an LLM for semantic analysis
- If the LLM flags the action as potentially malicious:
  - `strict = True`: execution is blocked with `SecurityError`
  - `strict = False`: warning is logged, execution proceeds

**Example**:

```python
@intent_verified(strict=True)
async def _action_execute_script(self, params: dict) -> dict:
    """Execute JavaScript in browser. Verified for intent before execution."""
    ...
```

---


## Cache Annotators

Cache annotators enable the **L2 shared cache** (Redis or embedded fakeredis). They are imported from `llmos_bridge.cache`:

```python
from llmos_bridge.cache import cacheable, invalidates_cache
```

The L2 cache sits between the action executor and the actual action implementation. On every call, the executor checks the cache before dispatching and stores the result after a successful execution. Cache misses are transparent — the action simply runs normally.

> **Architecture note** — L2 is layered on top of the L1 in-process session cache (`ActionSessionCache`). L2 is shared across sessions and, when a real Redis is configured, across daemon instances. See [Action Cache](../overview/action-cache.md) for the full architecture.

---


### @cacheable

Marks an action's output as cacheable in the L2 Redis/fakeredis cache.

**Signature**:

```python
@cacheable(
    ttl: int = 300,
    key_params: list[str] | None = None,
    shared: bool = True,
    invalidated_by: list[str] | None = None,
)
```

**Parameters**:

| Parameter | Type | Default | Description |
| --- | --- | --- | --- |
| `ttl` | int | `300` | Time-to-live in seconds. `0` = no expiry. |
| `key_params` | list\[str\] | `None` | Param names included in the cache key. `None` = all params. |
| `shared` | bool | `True` | `True` = use L2 Redis/fakeredis. `False` = L1 session only. |
| `invalidated_by` | list\[str\] | `None` | Informational: action names that invalidate this entry. |

**Cache key format**:

```text
llmos:cache:{module_id}:{action_name}:{sha256_hex16}
```

Path parameters (`path`, `source`, `directory`, `file_path`) are resolved to absolute paths before hashing, so `path="."` and `path="/home/user/project"` produce the same key when they refer to the same directory.

**Metadata set**:

- `_cache_meta["cacheable"]`: `True`
- `_cache_meta["ttl"]`: int
- `_cache_meta["key_params"]`: `list[str] | None`
- `_cache_meta["shared"]`: bool

**Stacking order**: Place `@cacheable` *inside* `@requires_permission` (closer to the function body) so metadata propagates outward through all decorator layers:

```python
@requires_permission(Permission.FILESYSTEM_READ)   # outermost
@cacheable(ttl=60, key_params=["path"])            # inner
async def _action_read_file(self, params: dict) -> dict:
    ...
```

**TTL guidelines**:

| Content type                       | Recommended TTL | Rationale                       |
| ---------------------------------- | --------------- | ------------------------------- |
| Schema info (DB tables, columns)   | 300 s           | Changes only on DDL             |
| Document reads (Word, Excel, PPTX) | 60–120 s        | User edits trigger invalidation |
| HTTP GET responses                 | 30 s            | Web content changes frequently  |
| URL availability checks            | 60 s            | Health status changes slowly    |
| HTML parsing                       | 300 s           | Pure function on given HTML     |
| System info (`get_system_info`)    | 10–30 s         | OS state changes infrequently   |
| Process list                       | 5 s             | Very volatile                   |

**Example**:

```python
from llmos_bridge.cache import cacheable

@cacheable(ttl=60, key_params=["path", "encoding"])
@requires_permission(Permission.FILESYSTEM_READ, reason="Read file contents")
async def _action_read_file(self, params: dict) -> dict:
    ...
```

**`shared=False`** — Use for session-private results (e.g., live browser page content) that must not leak between sessions:

```python
@cacheable(ttl=10, key_params=["session_id", "selector"], shared=False)
@requires_permission(Permission.BROWSER)
async def _action_get_page_content(self, params: dict) -> dict:
    ...
```

---


### @invalidates_cache

Marks a write action as invalidating cached results of other actions in the same module. Invalidation runs **before** the action executes, ensuring any subsequent read reflects the new state.

**Signature**:

```python
@invalidates_cache(*action_names: str)
```

**Parameters**:

| Parameter | Type | Description |
| --- | --- | --- |
| `*action_names` | str | Action names to invalidate (without `_action_` prefix). Use `"*"` to invalidate all cached actions. |

**What it does at runtime**:

1. Builds Redis glob patterns: `llmos:cache:{module_id}:{action}:*`
2. Deletes all matching keys from the L2 cache
3. Proceeds with the action execution

**Metadata set**:

- `_cache_meta["invalidates"]`: `list[str]`

**Stacking order**: Place `@invalidates_cache` *inside* `@requires_permission`, just like `@cacheable`:

```python
@requires_permission(Permission.FILESYSTEM_WRITE)
@invalidates_cache("read_file", "list_directory", "get_file_info")
async def _action_write_file(self, params: dict) -> dict:
    ...
```

**Wildcard invalidation** — Use `"*"` to clear the entire module cache. Appropriate when any write could affect any cached read (e.g., document editors):

```python
@invalidates_cache("*")
@requires_permission(Permission.FILESYSTEM_WRITE, reason="Modifies Excel workbook")
async def _action_write_cell(self, params: dict) -> dict:
    ...
```

**Cross-file note**: Invalidation patterns match on `module_id` and `action_name` only, not on specific file paths. A write to `doc_A.docx` will invalidate cached reads for `doc_B.docx` as well. This is conservative (no stale data) but may cause cold-cache misses across files. For single-document workflows, there is no impact.

---


### Caller-side cache bypass: `_no_cache`

Any caller (LLM plan, test, or application code) can pass `"_no_cache": true` as an extra parameter to any cacheable action to force fresh execution for that specific call:

```python
result = await module.execute("read_file", {"path": "/data/config.yaml", "_no_cache": True})
```

Or in an IML plan:

```json
{
  "id": "verify",
  "module": "filesystem",
  "action": "read_file",
  "params": { "path": "/data/config.yaml", "_no_cache": true }
}
```

**Semantics** — `_no_cache` is a *cache-refresh*, not a *cache-disable*:

- The L2 cache **read** is skipped — the action always executes against the real source.
- The fresh result **is written** back to the L2 cache — subsequent reads benefit from it.
- `_no_cache` is stripped from `params` before the cache key is computed and before the handler is called — action implementations never see it.

**When to use it**:

- After a write, to verify the new on-disk/database state (write actions already invalidate the cache, but `_no_cache` is a safety valve for edge cases like external changes or cross-file invalidation gaps).
- When the user explicitly asks for "current" or "fresh" data on a cached resource.

**When NOT to use it**:

- Routine reads where no write has occurred — the cache is correct and skipping it wastes I/O.
- Multiple reads of the same resource within one plan — use `depends_on` + `{{result.<id>.field}}` templates instead.

---


### Cache-annotated modules (built-in)

| Module | Cacheable actions | Invalidated by |
| --- | --- | --- |
| `filesystem` | `read_file` (60 s), `list_directory` (60 s), `get_file_info` (60 s), `search_files` (30 s), `compute_checksum` (120 s) | `write_file`, `append_file`, `copy_file`, `move_file`, `delete_file`, `create_directory` |
| `os_exec` | `list_processes` (5 s), `get_env_var` (30 s), `get_system_info` (10 s) | `set_env_var` |
| `database` | `list_tables` (300 s), `get_table_schema` (300 s) | `execute_query`, `create_table` |
| `api_http` | `http_get` (30 s), `check_url_availability` (60 s), `parse_html` (300 s) | — (stateless) |
| `word` | `read_document`, `get_document_meta`, `list_paragraphs`, `list_tables`, `extract_text`, `count_words` (all 120 s) | All write actions (`@invalidates_cache("*")`) |
| `excel` | `get_workbook_info`, `list_sheets`, `get_sheet_info`, `read_cell`, `read_range` (all 60 s) | All write actions (`@invalidates_cache("*")`) |
| `powerpoint` | `get_presentation_info`, `list_slides`, `read_slide` (all 60 s) | All write actions (`@invalidates_cache("*")`) |

---


### Metadata introspection

```python
from llmos_bridge.cache.decorators import collect_cache_metadata

meta = collect_cache_metadata(module._action_read_file)
# {
#     "cacheable": True,
#     "ttl": 60,
#     "key_params": ["path", "encoding"],
#     "shared": True,
#     "invalidated_by": [],
# }

meta = collect_cache_metadata(module._action_write_file)
# {
#     "invalidates": ["read_file", "list_directory", "get_file_info", "search_files"],
# }
```

---


## Streaming Annotator

### @streams_progress

Marks an action as supporting real-time progress streaming.

**Signature**:

```python
@streams_progress
```

No parameters. This is a simple attribute marker.

**Metadata set**:

- `_streams_progress`: `True`

**How it works**:

1. Module developer applies `@streams_progress` to an `_action_*` method
2. The executor detects the attribute and injects an `ActionStream` into `params["_stream"]`
3. The action pops the stream and uses it to emit progress updates
4. Updates flow through EventBus to SSE clients at `GET /plans/{id}/stream`
5. The SDK receives updates and includes them in the LLM agent's observation

**ActionStream API**:

| Method | Description |
| --- | --- |
| `emit_progress(percent, message="")` | Emit progress update (0-100%) |
| `emit_intermediate(data)` | Emit partial results |
| `emit_status(status)` | Emit status change (e.g., "connecting", "transferring") |

**Complete example**:

```python
from llmos_bridge.orchestration.streaming_decorators import streams_progress
from llmos_bridge.orchestration.stream import _STREAM_KEY, ActionStream

class ApiHttpModule(BaseModule):
    @requires_permission(Permission.FILE_DOWNLOAD)
    @streams_progress
    async def _action_download_file(self, params: dict) -> dict:
        stream: ActionStream | None = params.pop(_STREAM_KEY, None)
        url = params["url"]
        output_path = params["output_path"]

        if stream:
            await stream.emit_status("connecting")

        async with httpx.AsyncClient() as client:
            async with client.stream("GET", url) as response:
                total = int(response.headers.get("content-length", 0))
                downloaded = 0

                with open(output_path, "wb") as f:
                    async for chunk in response.aiter_bytes():
                        f.write(chunk)
                        downloaded += len(chunk)
                        if stream and total > 0:
                            pct = (downloaded / total) * 100
                            await stream.emit_progress(pct, f"{downloaded}/{total} bytes")

        return {"path": output_path, "bytes": downloaded}
```

**Graceful degradation**: If the stream is not injected (e.g., executor does not have an EventBus), `params.pop(_STREAM_KEY, None)` returns `None`. The action works normally, just without progress reporting.

**Streaming-enabled actions across all modules**:

| Module             | Action                  | Streaming Pattern                                           |
| ------------------ | ----------------------- | ----------------------------------------------------------- |
| `computer_control` | `click_element`         | status: capturing_screen, resolving_element                 |
| `computer_control` | `type_into_element`     | status: capturing_screen, resolving_element                 |
| `computer_control` | `wait_for_element`      | progress: poll count + elapsed time                         |
| `computer_control` | `read_screen`           | status: capturing_and_parsing, progress: element count      |
| `computer_control` | `find_and_interact`     | status: capturing_screen, resolving_element                 |
| `computer_control` | `get_element_info`      | status: capturing_screen                                    |
| `computer_control` | `execute_gui_sequence`  | progress: step N/total per step                             |
| `computer_control` | `move_to_element`       | status: capturing_screen, resolving_element                 |
| `computer_control` | `scroll_to_element`     | progress: scroll N/max_scrolls                              |
| `vision`           | `parse_screen`          | status: loading_screenshot, parsing_vision_pipeline         |
| `vision`           | `capture_and_parse`     | status: capturing_screen, parsing_vision_pipeline           |
| `vision`           | `find_element`          | status: loading_screenshot, parsing_vision_pipeline         |
| `vision`           | `get_screen_text`       | status: loading_screenshot, extracting_text                 |
| `filesystem`       | `search_files`          | status: searching, progress: match count                    |
| `filesystem`       | `create_archive`        | status: creating_archive, progress: complete                |
| `filesystem`       | `extract_archive`       | status: extracting, progress: complete                      |
| `filesystem`       | `compute_checksum`      | status: computing_checksum, progress: complete              |
| `filesystem`       | `watch_path`            | progress: elapsed/timeout percentage                        |
| `os_exec`          | `run_command`           | status: starting_process, running, progress: exit code      |
| `api_http`         | `download_file`         | status: connecting, downloading; progress: bytes/total      |
| `api_http`         | `upload_file`           | status: reading_file, uploading, progress: bytes            |
| `api_http`         | `send_email`            | status: connecting_smtp, progress: sent                     |
| `api_http`         | `read_email`            | status: connecting_imap, progress: message count            |
| `api_http`         | `webhook_trigger`       | progress: attempt N/max_attempts                            |
| `browser`          | `navigate_to`           | status: navigating, progress: loaded URL                    |
| `browser`          | `submit_form`           | status: submitting, progress: navigated URL                 |
| `browser`          | `download_file`         | status: downloading, progress: complete                     |
| `browser`          | `wait_for_element`      | status: waiting_for_element, progress: found/not found      |

**Manifest integration**: Set `streams_progress=True` in the corresponding `ActionSpec`:

```python
ActionSpec(
    name="download_file",
    description="Download file with streaming progress",
    streams_progress=True,  # Declared in manifest
)
```

---


## Configuration Annotator

### @configurable / ModuleConfigBase

The configuration annotation system allows modules to declare typed configuration schemas.

**ModuleConfigBase**:

```python
from llmos_bridge.modules.config import ModuleConfigBase, ConfigField

class VisionConfig(ModuleConfigBase):
    cache_max_entries: int = ConfigField(5, description="Max cached results")
    cache_ttl_seconds: float = ConfigField(2.0, description="Cache TTL")
    speculative_prefetch: bool = ConfigField(True, description="Enable prefetch")
    device: str = ConfigField("auto", description="Compute device", enum=["auto", "cpu", "cuda", "mps"])
```

**ConfigField parameters**:

| Parameter     | Type   | Description                         |
| ------------- | ------ | ----------------------------------- |
| `default`     | any    | Default value                       |
| `description` | str    | Field description for documentation |
| `enum`        | list   | Allowed values (optional)           |
| `min_value`   | number | Minimum value for numeric fields    |
| `max_value`   | number | Maximum value for numeric fields    |

**Integration with BaseModule**:

```python
class VisionModule(BaseModule):
    CONFIG_MODEL = VisionConfig

    # Access config at runtime
    async def _action_parse_screen(self, params: dict) -> dict:
        cache_ttl = self.config.cache_ttl_seconds
        ...
```

**Schema generation**: `_collect_config_schema()` produces JSON Schema for the API.

---


## Metadata Introspection

### collect_security_metadata()

Extract all security decorator metadata from a function:

```python
from llmos_bridge.security.decorators import collect_security_metadata

meta = collect_security_metadata(module._action_delete_file)
# {
#     "required_permissions": ["filesystem.delete"],
#     "permission_reason": "Permanently removes files",
#     "risk_level": "HIGH",
#     "irreversible": True,
#     "audit_level": "detailed",
# }
```

### collect_streaming_metadata()

Extract streaming metadata from a function:

```python
from llmos_bridge.orchestration.streaming_decorators import collect_streaming_metadata

meta = collect_streaming_metadata(module._action_download_file)
# {"streams_progress": True}
```

### BaseModule._collect_security_metadata()

Introspect all actions in a module:

```python
meta = module._collect_security_metadata()
# {
#     "read_file": {"required_permissions": ["filesystem.read"]},
#     "write_file": {"required_permissions": ["filesystem.write"], "audit_level": "standard"},
#     "delete_file": {"required_permissions": ["filesystem.delete"], "risk_level": "HIGH", ...},
# }
```

---


## Impact on System Components

### Dashboard

Security metadata enriches the dashboard with:

- Permission badges per action
- Risk level indicators
- Audit level markers
- Data classification tags
- Streaming capability indicators

### Orchestration

The executor uses metadata to:

- Inject `ActionStream` for `@streams_progress` actions
- Check permissions before dispatch
- Enforce rate limits
- Emit audit events
- Run intent verification for `@intent_verified` actions

### Manifest / API

Metadata is serialized into the `ModuleManifest` and exposed through:

- `GET /modules/{id}` — module details with security metadata
- `GET /modules/{id}/actions/{action}/schema` — action schema with annotations
- `GET /context` — system prompt includes permission requirements

### LLM Agent

The agent receives enriched context including:

- Which actions require specific permissions
- Risk levels for sensitive actions
- Which actions stream progress
- Data classification of outputs

This allows the agent to make informed decisions about which actions to include in plans and whether to set `requires_approval` for sensitive operations.

---


## Best Practices

### Apply Decorators Consistently

Every `_action_*` method should have at minimum:

- `@requires_permission` — even for read-only actions
- `@audit_trail("standard")` — for write operations

### Match Risk Level to Impact

| Impact                        | Risk Level | Example                          |
| ----------------------------- | ---------- | -------------------------------- |
| Read-only, no side effects    | LOW        | `read_file`, `list_processes`    |
| Writes, but reversible        | MEDIUM     | `write_file`, `send_email`       |
| Destructive, hard to undo     | HIGH       | `delete_file`, `kill_process`    |
| System-altering, irreversible | CRITICAL   | `format_disk`, `drop_database`   |

### Use Specific Permissions

Prefer `Permission.FILESYSTEM_DELETE` over `Permission.FILESYSTEM_WRITE` for deletion actions. Granular permissions enable fine-grained control.

### Document Reasons

Always provide a `reason` parameter to `@requires_permission`:

```python
@requires_permission(
    Permission.PROCESS_KILL,
    reason="Terminates running process by PID"
)
```

The reason is displayed in:

- Audit logs
- Permission request prompts
- Dashboard tooltips

### Test Both Paths

Test actions with security enabled and disabled:

```python
# Without enforcement (unit test)
result = await module.execute("delete_file", {"path": "/tmp/test"})

# With enforcement (integration test)
module.set_security(security_manager)
with pytest.raises(PermissionDeniedError):
    await module.execute("delete_file", {"path": "/tmp/test"})
```
