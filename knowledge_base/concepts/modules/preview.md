---
id: module-concept-preview
title: "preview module - overview"
type: module-concept
module: preview
isolation: shared
keywords: [preview, preview-module]
version: 1.0.0
---

# `preview` module

- **Isolation**: `shared` (one instance shared across apps)
- **Version**: `1.0.0`
- **Actions**: 0 visible, 17 internal

## Description (from class docstring)

Preview Module - universal live canvas for Digitorn apps.

Agents push state and events to a per-session preview stream that the
app's ``web/`` UI reads via Socket.IO (namespace ``/events``, room
``session:{session_id}``). This gives any app (digitorn-builder,
future workflow editors, multi-agent orchestrators, …) an n8n-style
live canvas **without writing a single line of frontend code** when
the app uses the default React SDK.

The module is stateless across the process in the sense that all
state lives inside a per-session ``PreviewSessionState``. Events are
published via the Socket.IO bus injected by the bootstrap.

Actions (all broadcast live to any connected browser for the session):

    preview.set_state(key, value)    update a scalar value in the state map
    preview.patch_state(patch)       merge a dict into the state map
    preview.get_state()              read the current state map
    preview.clear()                  reset everything

    preview.push_node(node)          add or replace a canvas node
    preview.update_node(id, updates) partial update of an existing node
    preview.highlight_node(id, status)  set status: idle|running|done|error
    preview.remove_node(id)

    preview.push_edge(edge)
    preview.remove_edge(id)

    preview.emit(event_type, data)   free-form event pushed to the stream

Every mutation also appends a ``PreviewEvent`` with an incrementing
``seq`` so clients can reconcile after a reconnect.

> Class-level summary: Per-session live preview for Digitorn apps.

    All actions resolve the current session via
    :meth:`BaseModule._get_session_id` (same mechanism as memory).
    Every mutation publishes a ``PreviewEvent`` with an incrementing
    sequence number, stored in the session's event ring buffer for
    snapshot replay on (re)connect.

## Configuration

Set under `modules.preview.config` in `app.yaml`. All fields derive from the module's Pydantic config model.

| Field | Type | Required | Default | Description |
|-------|------|:--------:|---------|-------------|
| `workspace` | str |  | `''` | Auto-injected by the daemon. |

## Actions

| Action | Short name | Internal | Risk | One-liner |
|--------|-----------|:--------:|------|-----------|
| `set_state` | `PreviewSetState` | ✓ | low | Set a single key in the session's live preview state map. |
| `patch_state` | `PreviewPatchState` | ✓ | low | Merge a dict of fields into the session's live preview state. |
| `get_state` | `PreviewGetState` | ✓ | low | Read the current preview state + canvas snapshot for the session. |
| `clear` | `PreviewClear` | ✓ | low | Clear all preview state, nodes, edges, and events for the session. |
| `emit` | `PreviewEmit` | ✓ | low | Push a free-form event to the live preview stream. |
| `set_resource` | `PreviewSetResource` | ✓ | low | Upsert a resource into a named channel. Generic primitive that any app shell can plug into. |
| `patch_resource` | `PreviewPatchResource` | ✓ | low | Merge fields into an existing resource (creates it if absent). |
| `delete_resource` | `PreviewDeleteResource` | ✓ | low | Delete a resource from a channel. |
| `list_resources` | `PreviewListResources` | ✓ | low | List every resource id+payload in a channel. |
| `clear_channel` | `PreviewClearChannel` | ✓ | low | Clear every resource in a channel. |
| `bulk_set_resources` | `PreviewBulkSetResources` | ✓ | low | Upsert many resources in one shot (snapshot/import). |
| `push_node` | `PreviewPushNode` | ✓ | low | Add or replace a canvas node (wrapper over set_resource('nodes', ...)). |
| `update_node` | `PreviewUpdateNode` | ✓ | low | Partially update an existing canvas node. |
| `highlight_node` | `PreviewHighlightNode` | ✓ | low | Highlight a node by setting its status (idle\|running\|done\|error). |
| `remove_node` | `PreviewRemoveNode` | ✓ | low | Remove a canvas node by id (and any edges touching it). |
| `push_edge` | `PreviewPushEdge` | ✓ | low | Add or replace a canvas edge between two nodes. |
| `remove_edge` | `PreviewRemoveEdge` | ✓ | low | Remove a canvas edge by id. |

## Per-action cards

For the full parameter spec of each action, see the auto-generated cards in `knowledge_base/modules/preview-*.md`.
