---

id: filesystem
title: Filesystem Module
sidebar_position: 1
format: md
---




# filesystem

File and directory operations with sandboxed path enforcement, atomic writes, and comprehensive security annotations.

| Property | Value |
|----------|-------|
| **Module ID** | `filesystem` |
| **Version** | `1.0.0` |
| **Type** | system |
| **Platforms** | All |
| **Dependencies** | None (stdlib only) |
| **Declared Permissions** | `filesystem.read`, `filesystem.write`, `filesystem.delete` |

---


## Actions

### read_file

Read file content with optional line range and encoding selection.

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `path` | string | Yes | - | Absolute path to the file |
| `encoding` | string | No | `"utf-8"` | File encoding |
| `offset` | integer | No | `null` | Start line (0-indexed) |
| `limit` | integer | No | `null` | Maximum lines to read |

**Returns**: `{"content": "...", "path": "...", "lines": 42, "encoding": "utf-8"}`

**Security**: `@requires_permission(Permission.FILESYSTEM_READ)`

**IML Example**:
```json
{
  "id": "read-config",
  "action": "read_file",
  "module": "filesystem",
  "params": {
    "path": "/home/user/config.yaml",
    "encoding": "utf-8"
  }
}
```

---


### write_file

Write text to file. Creates parent directories if they do not exist. Supports atomic exclusive creation.

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `path` | string | Yes | - | Absolute path to the file |
| `content` | string | Yes | - | Content to write |
| `encoding` | string | No | `"utf-8"` | File encoding |
| `create_dirs` | boolean | No | `true` | Create parent directories if missing |
| `exclusive` | boolean | No | `false` | Fail if file already exists (TOCTOU-safe) |

**Returns**: `{"path": "...", "bytes_written": 1024}`

**Security**:
- `@requires_permission(Permission.FILESYSTEM_WRITE)`
- `@rate_limited(calls_per_minute=60)`
- `@audit_trail("standard")`

---


### append_file

Append text to an existing file.

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `path` | string | Yes | - | Absolute path |
| `content` | string | Yes | - | Content to append |
| `newline` | boolean | No | `true` | Add newline before appending |

**Security**:
- `@requires_permission(Permission.FILESYSTEM_WRITE)`
- `@rate_limited(calls_per_minute=60)`

---


### copy_file

Copy a file to a new location.

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `source` | string | Yes | - | Source file path |
| `destination` | string | Yes | - | Destination file path |
| `overwrite` | boolean | No | `false` | Overwrite if destination exists |

**Security**:
- `@requires_permission(Permission.FILESYSTEM_WRITE)`
- `@rate_limited(calls_per_minute=60)`

---


### move_file

Move or rename a file.

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `source` | string | Yes | - | Source file path |
| `destination` | string | Yes | - | Destination file path |
| `overwrite` | boolean | No | `false` | Overwrite if destination exists |

**Security**:
- `@requires_permission(Permission.FILESYSTEM_WRITE)`
- `@rate_limited(calls_per_minute=60)`

---


### delete_file

Delete a file or directory. Supports recursive deletion.

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `path` | string | Yes | - | Path to delete |
| `recursive` | boolean | No | `false` | Delete directories recursively |

**Security**:
- `@requires_permission(Permission.FILESYSTEM_DELETE)`
- `@sensitive_action(RiskLevel.HIGH, irreversible=True)`
- `@rate_limited(calls_per_minute=60)`
- `@audit_trail("detailed")`

---


### create_directory

Create a directory with optional parent creation.

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `path` | string | Yes | - | Directory path |
| `parents` | boolean | No | `true` | Create parent directories |

**Security**: `@requires_permission(Permission.FILESYSTEM_WRITE)`

---


### list_directory

List files and directories with glob pattern support.

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `path` | string | Yes | - | Directory path |
| `pattern` | string | No | `"*"` | Glob pattern |
| `recursive` | boolean | No | `false` | Recursive listing |
| `max_results` | integer | No | `1000` | Maximum entries returned |

**Returns**: `{"entries": [{"name": "...", "type": "file", "size": 1024, ...}], "total": 42}`

**Security**: `@requires_permission(Permission.FILESYSTEM_READ)`

---


### search_files

Search files by name and content patterns.

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `path` | string | Yes | - | Base directory |
| `name_pattern` | string | No | `null` | Filename glob pattern |
| `content_pattern` | string | No | `null` | Content regex pattern |
| `recursive` | boolean | No | `true` | Search recursively |
| `case_sensitive` | boolean | No | `true` | Case-sensitive matching |
| `max_results` | integer | No | `100` | Maximum results |

**Security**: `@requires_permission(Permission.FILESYSTEM_READ)`

---


### get_file_info

Read file metadata: size, timestamps, permissions, symlink status.

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `path` | string | Yes | - | File or directory path |

**Returns**: `{"path": "...", "size": 1024, "created": "...", "modified": "...", "permissions": "0644", "is_symlink": false}`

**Security**: `@requires_permission(Permission.FILESYSTEM_READ)`

---


### create_archive

Create zip, tar, tar.gz, or tar.bz2 archives.

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `output_path` | string | Yes | - | Archive output path |
| `source_paths` | array | Yes | - | Files/directories to archive |
| `format` | string | No | `"zip"` | `zip`, `tar`, `tar.gz`, `tar.bz2` |

**Security**: `@requires_permission(Permission.FILESYSTEM_WRITE)`

---


### extract_archive

Extract archives to a destination directory.

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `archive_path` | string | Yes | - | Path to archive |
| `destination` | string | Yes | - | Extraction directory |

**Security**: `@requires_permission(Permission.FILESYSTEM_WRITE)`

---


### compute_checksum

Compute MD5, SHA1, or SHA256 checksum of a file.

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `path` | string | Yes | - | File path |
| `algorithm` | string | No | `"sha256"` | `md5`, `sha1`, `sha256` |

**Returns**: `{"path": "...", "algorithm": "sha256", "checksum": "abc123..."}`

**Security**: `@requires_permission(Permission.FILESYSTEM_READ)`

---


## Streaming Support

5 long-running actions are decorated with `@streams_progress` and emit real-time events via SSE (`GET /plans/{plan_id}/stream`):

| Action | Status Phases | Progress Pattern |
|--------|---------------|------------------|
| `search_files` | `searching` | % based on directories scanned |
| `create_archive` | `archiving` | % based on files added/total |
| `extract_archive` | `extracting` | % based on entries extracted/total |
| `compute_checksum` | `computing` | % based on bytes read/file size |
| `watch_path` | `watching` | % based on elapsed/timeout |

Fast actions (`read_file`, `write_file`, `copy_file`, etc.) are not streaming-enabled as they complete near-instantly.

See [Decorators Reference - @streams_progress](../../annotators/decorators.md) for SDK consumption details.

---


## Security Details

### Sandbox Path Enforcement

When `security.sandbox_paths` is configured, all file operations are restricted to those directories. The `PermissionGuard` uses `os.path.realpath()` to resolve symlinks, preventing symlink-based escapes.

```yaml
# config.yaml
security:
  sandbox_paths:
    - /home/user/projects
    - /tmp/llmos
```

Any path outside these directories raises `PermissionDeniedError`.

### Atomic Exclusive Creation

The `exclusive` flag in `write_file` uses `os.open()` with `O_CREAT | O_EXCL` flags, providing TOCTOU-safe file creation. If the file already exists, the operation fails atomically.

### Symlink Resolution

All write operations resolve the target path to its real path before writing. This prevents attacks where a symlink points to a sensitive location outside the sandbox.

---


## Implementation Notes

- All I/O is async via `asyncio.to_thread()` wrapping synchronous stdlib calls
- No external dependencies - uses only Python standard library (`pathlib`, `shutil`, `os`, `zipfile`, `tarfile`, `hashlib`)
- File content is returned as string (not bytes) - binary files should be handled via path references
