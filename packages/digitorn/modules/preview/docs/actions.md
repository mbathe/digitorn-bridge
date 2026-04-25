# preview — actions reference

All preview actions are **per-session**: they operate on the canvas
state of whichever session id the agent loop has activated on the
module (`set_active_session`). In normal use the daemon sets this for
you at the start of every turn.

## State map actions

### `preview.set_state(key, value)`

Set a single scalar in the session's live state map. Published to all
connected SSE subscribers as a `state_changed` delta.

```yaml
preview.set_state:
  key: "current_state"
  value: "STATE 2 — INTERVIEW"
```

### `preview.patch_state(patch)`

Merge a dict into the state map. Use this when you need to update
several related fields atomically.

```yaml
preview.patch_state:
  patch:
    current_state: "STATE 4 — COMPILE LOOP"
    compile_attempts: 2
    last_errors: ["agents[0].brain.provider: invalid value 'xyz'"]
```

### `preview.get_state()`

Return the full snapshot for the current session (state map + nodes +
edges + recent events + seq).

### `preview.clear()`

Wipe state, nodes, edges, and the event buffer. Publishes a single
`cleared` delta so subscribers reset their UI.

## Canvas actions

### `preview.push_node(id, type, label, position, data, status)`

Add or replace a canvas node. Shape is compatible with ReactFlow:

```yaml
preview.push_node:
  id: "state-2"
  type: "state"
  label: "Interview"
  position: {x: 240, y: 120}
  data: {phase: "questioning"}
  status: "idle"    # idle | running | done | error
```

### `preview.update_node(id, updates)`

Partial update — fields that exist on the node get updated, unknown
fields land in `node.data`. Returns an error if the node doesn't exist.

### `preview.highlight_node(id, status)`

Shortcut to change a node's status. Used when an agent transitions a
state: `running` when entering, `done` when moving on.

### `preview.remove_node(id)`

Drop a node. **Any edges touching it are cascade-dropped** — clients
see one `node_removed` plus one `edge_removed` per dropped edge.

## Edge actions

### `preview.push_edge(id, source, target, label, data)`

Add or replace an edge between two node ids.

### `preview.remove_edge(id)`

Drop an edge.

## Free-form events

### `preview.emit(event_type, data)`

Push an event that does not mutate state or the canvas graph. The
browser receives it in the rolling event buffer (`usePreviewEvents`).
Use this for logs, progress beacons, and toasts.

```yaml
preview.emit:
  event_type: "compile_attempt"
  data: {attempt: 1, duration_ms: 420, success: true}
```

## Sequencing guarantees

Every mutation has a monotonically increasing `seq` per session. The
React SDK uses it to skip duplicates after a reconnect and to keep the
event buffer ordered. Deltas older than the last observed seq are
dropped client-side.
