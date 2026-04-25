# Index Module — Integration Guide

## Daemon Integration

The index module integrates with the daemon automatically via the
`ModuleLifecycleManager`:

1. **Auto-discovery** — `digitorn-module.toml` is detected at boot.
2. **Watcher injection** — the daemon injects `SourceWatcherService` on every
   module before `start_all()`, so `restore_state()` can restart persistent
   watches immediately.
3. **Lifecycle start** — `ModuleLifecycleManager.start_module("index")`:
   - Loads saved state from `JsonStateStore` (restores index + persistent watches).
   - Calls `on_start()`.
   - Auto-subscribes to all topics declared in `subscribes_events`
     (`digitorn.watcher.*.file_*`, `digitorn.module.*.action_completed`).
4. **Event routing** — `EventBus.subscribe()` wires callbacks using MQTT-style
   wildcard matching. When a `UniversalEvent` arrives, the callback calls
   `IndexModule.on_event(topic, event_dict)`.
5. **Auto-invalidation** — entries are removed when files are written/edited/deleted,
   whether through the API or detected by the watcher.
6. **Auto-reindex** — when a watched file is created or modified, the index
   re-extracts entries automatically.
7. **Graceful shutdown** — `ModuleLifecycleManager.stop_module("index")`:
   - Saves `state_snapshot()` to `JsonStateStore` (index + persistent watch configs).
   - Unsubscribes from all event topics.
   - Calls `on_stop()`.

### Startup Sequence

```text
create_app() / lifespan:
    │
    ├── SourceWatcherService(event_bus)  →  start()
    ├── ServiceBus()
    ├── JsonStateStore(~/.digitorn/state/)
    ├── ModuleLifecycleManager(registry, event_bus, service_bus, state_store)
    │
    ├── for module in registry:
    │       module._watcher_service = watcher_service   ← injection
    │
    └── lifecycle.start_all()
            │
            ├── restore_state(saved)     ← restores index + restarts watches
            ├── on_start()
            └── auto_subscribe_events()  ← wires EventBus → on_event()
```

## Watcher Integration

The index module is the primary consumer of the SourceWatcherService. When a
source is registered with `watch=true`, the full pipeline is:

```text
register_source(watch=true)
    │
    ▼
IndexModule._start_watch()
    │
    ▼
SourceWatcherService.watch(config)
    │
    ├── filesystem  → FilesystemWatcher (inotify/fsevents)
    ├── polling     → PollingWatcher (hash-based)
    └── service_bus → ServiceBusPollingWatcher (module-delegated)
              │
              ▼
        ChangeEvent → EventBus.publish()
              │
              ▼
        digitorn.watcher.{source_id}.file_created
        digitorn.watcher.{source_id}.file_modified
        digitorn.watcher.{source_id}.file_deleted
              │
              ▼
        IndexModule.on_event()
              │
              ├── invalidate_by_path()
              └── _auto_reindex_path()
```

### Backend Selection

The backend is chosen automatically based on `module_id`:

| `module_id`              | Backend                    | How it works                                                     |
|--------------------------|----------------------------|------------------------------------------------------------------|
| `filesystem`             | `FilesystemWatcher`        | OS-native notifications (inotify/FSEvents) via `watchfiles`      |
| `polling`                | `PollingWatcher`           | Periodic SHA-256 hash comparison of files                        |
| `database`, `storage`... | `ServiceBusPollingWatcher` | Delegates `list_items` + `checksum` to owning module via bus     |

### Watch Modes

| Mode         | Lifecycle                               | Persistence                                       |
|--------------|-----------------------------------------|---------------------------------------------------|
| `ephemeral`  | Dies when the application disconnects   | Not saved in state snapshot                        |
| `persistent` | Survives daemon restarts                | Saved in `state_snapshot()`, restored on boot      |

### Making a Custom Module Watchable

Any module can support watch mode by exposing two actions via the service bus:

**`list_items`** — enumerate watchable items:

```python
# Called with:
{"source_id": "crm_db", "root": "postgres://localhost/crm", "patterns": ["public.*"]}

# Must return:
{"items": [{"id": "users", "path": "public.users"}, {"id": "orders", "path": "public.orders"}]}
```

**`checksum`** — compute content hashes:

```python
# Called with:
{"source_id": "crm_db", "items": ["users", "orders"]}

# Must return:
{"checksums": [{"id": "users", "hash": "abc123"}, {"id": "orders", "hash": "def456"}]}
```

The `ServiceBusPollingWatcher` calls these periodically, diffs the snapshots,
and emits `ChangeEvent`s for any differences (new items, changed hashes,
removed items).

### Error Resilience

- If `list_items` or `checksum` fails (e.g. database connection lost), the
  previous snapshot is preserved and no spurious events are emitted.
- When the connection recovers, the next poll cycle resumes normal operation.
- The watcher queue has a capacity of 4096 events. If full, the oldest event
  is dropped to make room (back-pressure).

## Service Bus Usage

The index delegates content reading to owning modules:

```python
# Scan uses filesystem.find + filesystem.read
result = await service_bus.call("filesystem", "find", {"path": root, "pattern": "**/*.py"})
result = await service_bus.call("filesystem", "read", {"path": file_path})
```

## Custom Extractors

Other modules can register extractors at runtime:

```python
# From a PDF module:
await service_bus.call("index", "register_extractor", {
    "name": "pdf",
    "module_id": "pdf",
    "extract_action": "extract_entries",
})
```

## State Persistence

Module state is stored by `JsonStateStore` in `~/.digitorn/state/index.state.json`.
The lifecycle manager calls `state_snapshot()` on stop and `restore_state()` on start.

The snapshot includes:

- All registered sources (with `watch`, `watch_mode`, `app_id`).
- All indexed entries and relations.
- A `persistent_watches` list — configs for watches that must survive restarts.

On restore, persistent watches are restarted automatically via the injected
`SourceWatcherService`. Ephemeral watches are discarded.

## LLM Agent Workflow

```text
1. index.register_source(watch=true)  →  register project with auto-watching
2. index.scan                          →  build the initial index
3. index.context                       →  get smart context for a target
4. filesystem.edit                     →  make precise edits
   ↓                                      (watcher auto-re-indexes)
5. index.context                       →  context is already up-to-date
```
