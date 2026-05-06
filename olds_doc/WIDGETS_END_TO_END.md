# Widgets - End-to-End Wiring Guide

> Audience: app authors who want to ship rich UIs without writing
> any frontend code. The Flutter client renders everything; the
> daemon validates, serves, and dispatches.

This guide shows the **complete round-trip** for the four common
patterns so you know exactly where every value goes:

1. Render a list bound to an HTTP data source
2. Submit a form and run a tool with its values
3. Push a widget live from an agent turn
4. Mutate a mounted widget without re-rendering

---

## 1. The mental model

```
┌──────────────────────────────────────────────────────────────────┐
│                         APP YAML                                  │
│ ┌────────────┐  ┌─────────────┐  ┌──────────────┐  ┌──────────┐ │
│ │ widgets:   │  │ modules:    │  │ capabilities:│  │ ./widgets│ │
│ │  inline    │  │  widget: {} │  │  grant       │  │  *.yaml  │ │
│ │  chat_side │  │             │  │   widget.*   │  │          │ │
│ └────────────┘  └─────────────┘  └──────────────┘  └──────────┘ │
└──────────────┬───────────────────────────────────────────────────┘
               │
               │ daemon compile → validate every primitive,
               │ action, accent. Reject if invalid.
               ▼
┌──────────────────────────────────────────────────────────────────┐
│  COMPILED APP (in memory + on disk)                              │
│  CompiledApp.widgets  →  WidgetsConfig                           │
└──────────────┬───────────────────────────────────────────────────┘
               │
               │ Flutter calls GET /widgets at app open
               ▼
┌──────────────────────────────────────────────────────────────────┐
│                    FLUTTER CLIENT                                │
│  • Renders chat_side, workspace_tabs, inline, modals             │
│  • Manages local form / state / loop scope                       │
│  • Substitutes {{form.X}}, {{state.X}}, {{item.X}} client-side   │
│  • Sends actions to /widgets/action with payload + form + state  │
│  • Listens for `widget:*` events on the Socket.IO `/events`      │
│    namespace (joined the session room) for live deltas           │
└──────────────┬───────────────────────────────────────────────────┘
               │
               ▼
┌──────────────────────────────────────────────────────────────────┐
│                       DAEMON RUNTIME                             │
│  • POST /widgets/action  → dispatch (tool/http/chat/...)         │
│  • GET /widgets/data/X   → resolve data sources server-side      │
│  • Socket.IO `widget:*`  → push render/update/close from agent   │
│  • widget.* tool calls   → publish to per-session Socket.IO room │
└──────────────────────────────────────────────────────────────────┘
```

**Key insight:** the daemon never renders. It validates and routes.
The Flutter client renders and substitutes expressions. The agent
**pushes** live changes via the `widget` module's actions.

---

## 2. Pattern A - List bound to an HTTP data source

### YAML

```yaml
modules:
  widget: {}

capabilities:
  grant:
    - module: widget
      actions: [render, update, close, error]

widgets:
  version: 1
  chat_side:
    title: Sources
    icon: library_books
    width: 300
    accent: blue

    # ── data: block - declares named bindings the tree references
    data:
      sources:
        type: http
        url: /rag/sources         # relative to the daemon
        poll: 10s                 # auto-refresh every 10 seconds
        cache: 30s

    # ── tree: references the binding via {{sources}}
    tree:
      type: list
      items: '{{sources}}'        # ← rendered as Array by client
      empty:
        type: empty_state
        icon: inbox
        title: No sources yet
      item:
        type: card
        title: '{{item.title}}'
        subtitle: '{{item.url | truncate(60)}}'
        action:
          action: chat
          template: 'Use source {{item.id}}'
```
### How values flow

1. **Compile time** - the daemon validates the tree, rejects unknown
   primitives, and stores `data.sources` on the compiled app.

2. **Client mount** - Flutter does:
   ```
   GET /api/apps/my-app/widgets
   → returns the full WidgetsConfig as JSON
   ```

3. **Hydrate the binding** - Flutter does:
   ```
   GET /api/apps/my-app/widgets/data/sources
   → daemon resolves data.sources (http source type)
   → executes the GET against /rag/sources
   → returns {data: {value: <json response>, status: 200}}
   ```

4. **Render** - Flutter substitutes `{{sources}}` with the array,
   loops the `item:` template, replaces `{{item.title}}` and
   `{{item.url}}` for each entry.

5. **Polling** - Flutter re-fetches the binding every 10s; the
   `poll:` is a hint the client honors locally. The daemon doesn't
   schedule anything.

### Refreshing on demand

```yaml
- type: button
  label: Refresh
  icon: refresh
  action:
    action: refresh
    bindings: [sources]    # tells the client to re-fetch named bindings
```
---

## 3. Pattern B - Submit a form and run a tool

### YAML

```yaml
widgets:
  version: 1
  modals:
    booking:
      title: New booking
      width: 560
      tree:
        type: form
        id: booking_form
        # initial values shown to the user before any input
        initial:
          topic: ""
          duration: 30
        children:
          - type: text_input
            name: topic                # → form.topic
            label: Topic
            placeholder: "1:1 sync"
            required: true
            validation:
              min: 3
              max: 120
          - type: textarea
            name: notes                # → form.notes
            label: Notes
            rows: 4
            max_chars: 500
          - type: select
            name: priority             # → form.priority
            label: Priority
            options:
              - { value: low,  label: Low  }
              - { value: med,  label: Medium }
              - { value: high, label: High }
            default: med
        submit:
          label: Book
          loading_label: "Booking…"
          icon: check
          disabled: '{{!form.valid}}'   # button greyed out until valid
          action:
            action: tool
            tool: create_meeting
            # OPTIONAL - without args, ALL form fields are auto-merged
            args:
              topic:    '{{form.topic}}'
              duration: '{{form.duration}}'
              priority: '{{form.priority}}'
              notes:    '{{form.notes}}'
        on_success:
          action: alert
          kind: success
          text: "Booked!"
        on_error:
          action: alert
          kind: error
          text: "{{error.message}}"
```
### How values flow (the moment the user clicks "Book")

1. **Client validation** - Flutter checks `required`, `regex`, `min`,
   `max`, `type_hint`. If any field fails, the button stays disabled
   (`{{!form.valid}}`).

2. **Substitution** - Flutter replaces `{{form.topic}}` with the
   actual user input from the form state map. The `args:` block
   becomes a concrete dict.

3. **POST** - Flutter sends:
   ```http
   POST /api/apps/my-app/widgets/action
   Authorization: Bearer <jwt>
   Content-Type: application/json

   {
     "widget_id": "w_booking_modal",
     "action_id": "submit",
     "type": "tool",
     "payload": {
       "tool": "create_meeting",
       "args": {
         "topic": "1:1 with Alice",
         "duration": 30,
         "priority": "high",
         "notes": "discuss Q3"
       }
     },
     "form": {
       "topic": "1:1 with Alice",
       "duration": 30,
       "priority": "high",
       "notes": "discuss Q3"
     },
     "state": {},
     "session_id": "sess_xyz"
   }
   ```

4. **Daemon dispatch** - `POST /widgets/action` handler:
   - Reads `payload.tool = "create_meeting"`
   - Reads `payload.args` (already-substituted)
   - **Auto-merges `body.form` into `args` for any field not already
     present** - so apps can omit the `args:` mapping entirely and
     just rely on form field names matching tool params
   - Resolves `create_meeting` via `to_fqn()` → finds the matching
     module action (e.g. `calendar.create_meeting`)
   - Builds the action's `params_model` from args
   - Calls `await module.create_meeting(params)`
   - Returns:
     ```json
     {"data": {"ok": true, "effect": {
        "action": "tool_result",
        "tool": "create_meeting",
        "result": {"success": true, "data": {"meeting_id": "m_42"}, "error": null}
     }}}
     ```

5. **Client effect** - Flutter sees `effect.action == "tool_result"`,
   triggers `on_success` (or `on_error` if `result.success == false`).

### Shortcut - auto-merge, no `args:` needed

If your tool params and your form names match 1-for-1, you can omit
the `args:` block:

```yaml
submit:
  label: Save
  action:
    action: tool
    tool: save_user
  # no args: needed - the daemon auto-merges body.form into args
```
The daemon does `args.setdefault(k, v)` for every `body.form[k]`.

### Reading form values from the agent side

When the user submits, the daemon receives both `payload.args` and
`body.form`. The tool action receives the merged dict as a Pydantic
params model:

```python
@action(
    description="Create a meeting from a widget form",
    params_model=CreateMeetingParams,
)
async def create_meeting(self, params: CreateMeetingParams) -> ActionResult:
    # params.topic, params.duration, params.priority, params.notes
    # are populated from the form values
    ...
    return ActionResult(success=True, data={"meeting_id": ...})
```

That's it - same contract as any other tool action.

---

## 4. Pattern C - Agent pushes a widget live

The agent decides mid-turn it needs to ask the user a confirmation
or show a chart. It calls the `widget` module:

```python
# From inside the agent loop (any tool call by the agent)
result = await widget.render(
    zone="inline",
    ref="confirm_delete",         # references widgets.inline.confirm_delete
    ctx={"path": "/docs/spec.md"},
)
widget_id = result.data["widget_id"]
```

What happens:

1. The `widget` module mounts the widget in the per-session store
2. Publishes a `widget:render` event on the session's Socket.IO room
   (`/events` namespace, room `session:{sid}`)
3. The Flutter client (joined the session room via `join_session`)
   receives the event
4. Renders `widgets.inline.confirm_delete` substituting `ctx.path`

When the user confirms:
- The `confirm` widget triggers its `confirm_action` (e.g. `tool: delete_thing`)
- The daemon dispatches `delete_thing` with the form/ctx values
- The agent gets a follow-up message via the chat session

### Inline tree (no `ref:`)

You can also push a one-off tree without declaring it in the YAML:

```python
await widget.render(
    zone="inline",
    tree={
        "type": "card",
        "title": "Query results",
        "children": [
            {"type": "stat", "label": "Rows", "value": "{{ctx.count}}"},
            {"type": "chart", "kind": "bar", "data": "{{ctx.rows}}",
             "x": "label", "series": [{"y": "value", "color": "blue"}]},
        ],
    },
    ctx={"count": 128, "rows": [{"label": "A", "value": 12}, {"label": "B", "value": 30}]},
)
```

---

## 5. Pattern D - Mutate a mounted widget

After rendering, the agent can patch the mounted widget without
re-rendering the whole tree:

```python
await widget.update(
    widget_id="w_abc123",
    patch={
        "data.sources": [{"id": 1, "title": "New source"}],
        "state.filter": "active",
    },
)
```

The daemon publishes a `widget:update` event with the patch. The
Flutter client merges the dotted-path keys into the widget's local
state map and re-renders only the affected nodes.

For a clean reset:

```python
await widget.close(widget_id="w_abc123")
```

For an error inside the widget without closing:

```python
await widget.error(
    widget_id="w_abc123",
    binding="sources",
    message="Backend timeout - please retry",
)
```

---

## 6. External `./widgets/*.yaml` files

For any non-trivial app, splitting widgets across files is cleaner:

```
my-app/
├── app.yaml                 ← references via `ref:`
├── package.toml
└── widgets/                 ← each .yaml = one inline widget
    ├── confirm_delete.yaml
    ├── source_card.yaml
    ├── booking_modal.yaml
    └── source_search.yaml
```

Each file is one of:
- A complete `InlineWidget`: `{tree: {...}, data: {...}}`
- A bare tree node: any dict with `type:`

The compiler auto-loads the folder, names each widget after the
file stem, and merges into `widgets.inline`. Collisions with
inline entries declared in `app.yaml` are rejected at compile.

---

## 7. The 4 zones - when to use which

| Zone | Where rendered | Typical use |
|---|---|---|
| **inline** | Chat bubble in the message stream | Confirms, alerts, query results, charts, anything one-shot tied to a turn |
| **chat_side** | Companion side panel always visible next to the chat | Sources / files / context the user references during the conversation |
| **workspace_tabs** | Workspace area, tabbed UI | Dashboards, CRUD tables, settings, tools |
| **modals** | Pop-up dialog | Multi-step wizards, forms, full-screen previews |

**Zone routing rules:**

- An app with a `chat_side:` block automatically gets the side
  panel - visible for all sessions of that app
- `workspace_tabs:` non-empty → a "Widgets" section appears in the
  workspace panel with one tab per entry
- `modals:` are only opened by an explicit `action: open_modal`
- `inline:` are only mounted by `widget.render(zone="inline", ...)`
  from the agent

---

## 8. The 43 primitives - one-line reference

### Layout (9)

| Primitive | Purpose |
|---|---|
| `column` | Vertical stack (children) |
| `row` | Horizontal stack |
| `card` | Bordered container with optional title/icon |
| `section` | Titled, collapsible group |
| `tabs` | Tab bar + content |
| `split` | 2-pane resizable split |
| `grid` | N-column responsive grid |
| `spacer` | Empty space (size / flex) |
| `divider` | Horizontal line |

### Content (4)

| Primitive | Purpose |
|---|---|
| `markdown` | Markdown text |
| `text` | Plain styled text (variant/weight/color) |
| `image` | Image with fit/radius/placeholder |
| `icon` | Material icon |

### Data display (7)

| Primitive | Purpose |
|---|---|
| `list` | Vertical list of items (item template) |
| `table` | Sortable / paginated table |
| `chart` | line / bar / area / pie / donut / scatter / gauge |
| `stat` | Single big metric with trend |
| `timeline` | Chronological events |
| `tree` | Hierarchical tree (file browser, etc) |
| `kanban` | 3+ column board with drag/drop |

### Input (14)

| Primitive | Purpose |
|---|---|
| `form` | Container that collects child input values |
| `text_input` | Single-line text field |
| `textarea` | Multi-line text |
| `select` | Single-choice dropdown |
| `multi_select` | Multi-choice dropdown |
| `radio` | Radio group |
| `checkbox` | Single boolean |
| `switch` | Toggle |
| `slider` | Number slider |
| `date` | Date picker |
| `time` | Time picker |
| `datetime` | Date+time picker |
| `file_upload` | File picker that POSTs to a URL |
| `code_editor` | Monaco-like syntax-highlighted editor |

### Action (4)

| Primitive | Purpose |
|---|---|
| `button` | Primary / secondary / ghost / destructive |
| `icon_button` | Compact icon-only button |
| `link` | Plain anchor (external/internal) |
| `confirm` | Card with "are you sure?" + buttons |

### Feedback (5)

| Primitive | Purpose |
|---|---|
| `alert` | info / warning / error / success banner |
| `badge` | Compact label (status pill) |
| `progress` | Bar or circular progress |
| `skeleton` | Loading placeholder |
| `empty_state` | Icon + title + subtitle + CTA when no data |

**Where rendering happens:** Flutter. The daemon validates the tree
against this closed set, refuses anything else, and serves the
JSON-serialised compiled widgets via `GET /widgets`.

---

## 9. The 15 actions - one-line reference

| Action | Routes to |
|---|---|
| `chat` | Inject a user message into the current chat session |
| `tool` | Invoke a module action (form values auto-merged) |
| `http` | App-scoped HTTP call via the daemon |
| `open_url` | Open a URL in the system browser |
| `open_workspace` | Switch / push a workspace tab |
| `open_modal` | Open a declared modal by name |
| `close` | Close the current modal / dismiss |
| `set_state` | Mutate the per-session widget state map |
| `refresh` | Re-fetch named data bindings |
| `copy` | Copy text to clipboard |
| `download` | Trigger a file download |
| `navigate` | Switch to another app / workspace tab |
| `confirm` | Wrap an action with a "are you sure?" prompt |
| `sequence` | Chain multiple actions in order |
| `alert` | Show a toast (info / warning / error / success) |

**Server vs client:** the daemon executes `tool`, `http`, `set_state`,
`sequence` (server effects). All other actions are pure client-side
UI effects - the daemon just acks them.

---

## 10. Testing your widgets

```bash
# Get the full tree
curl -H "Authorization: Bearer $JWT" \
  http://localhost:8000/api/apps/my-app/widgets

# Lint / validate
curl -H "Authorization: Bearer $JWT" \
  http://localhost:8000/api/apps/my-app/widgets/validate

# Resolve a binding
curl -H "Authorization: Bearer $JWT" \
  http://localhost:8000/api/apps/my-app/widgets/data/sources

# Submit an action (form values + tool dispatch)
curl -X POST \
  -H "Authorization: Bearer $JWT" \
  -H "Content-Type: application/json" \
  -d '{
    "widget_id": "w_test",
    "type": "tool",
    "payload": {"tool": "create_meeting"},
    "form": {"topic": "test", "duration": 30}
  }' \
  http://localhost:8000/api/apps/my-app/widgets/action
```

Live `widget:*` events flow through Socket.IO (no SSE
endpoint). The CLI smoke-test path is to use the Socket.IO
client and subscribe to the session room:

```bash
# Use the python client to peek at events on the /events
# namespace, room session:sess_xyz
py -3.12 -m digitorn dev chat my-app -m "trigger a widget"
```

For one-off SDK / Flutter integration, see
[Flutter Socket.IO Integration](flutter_socketio_integration.md).

---

## 11. Compile-time guarantees recap

The daemon **rejects deploys** if:

- A `type:` is not in the 43-primitive set
- An `action:` is not in the 15-action set
- An `accent:` is not in `{blue, purple, green, orange, red, cyan}`
- `version:` is not 1
- Two inputs in the same form share a `name:`
- An external `./widgets/X.yaml` collides with an inline entry
- A form's `submit.action` is malformed
- An action's `kind` is not a string

Errors carry the YAML path, e.g.:

```
widgets.modals.booking.tree.children[1].action.action:
  unknown action 'chatt' (allowed: ['alert', 'chat', 'close', ...])
```

---

## 12. What the daemon does NOT do

To set realistic expectations:

- **Doesn't render** - the Flutter client owns rendering.
- **Doesn't evaluate `{{...}}` expressions** - substitution happens
  client-side. The daemon receives concrete values via `body.form`
  and `payload.args` after the client has done its work.
- **Doesn't poll data sources** - `poll: 10s` is a hint the client
  uses to schedule its own refetches.
- **Doesn't validate tool args** beyond the action's params model.
  Type errors surface from the tool itself.
- **Doesn't cache HTTP data sources** in v1.

---

## 13. Widget state as first-class variables ⭐

This is the part that makes widgets really powerful: **every value
the user touches becomes a variable visible to the agent on the
next turn**. Forms, set_state actions, tool results - all of them
land in the per-session widget state and are injected into the
agent's system prompt under a ``WIDGET CONTEXT`` section.

### What lands in the state, and where

| Source | Lands in | Visible to agent via |
|---|---|---|
| Form submission (`body.form` from POST `/widgets/action`) | `state.form.<field>` + `state.last_form` | `WIDGET CONTEXT` section |
| `action: set_state { set: { x: 1 } }` | `state.x` | `WIDGET CONTEXT` section |
| Tool result from a widget-triggered `action: tool` | `state.results.<tool>` + `state.last_result` | `WIDGET CONTEXT` section |
| Agent calls `widget.set_state(set={...})` from a tool | `state.<key>` | next-turn prompt |

The agent does NOT need to call anything - the section is rebuilt
every turn from the live state. So you can write a system_prompt
that says "use whatever the user has selected as filters" and the
agent will see the actual selection inline.

### Example WIDGET CONTEXT section in the prompt

After the user fills a form (`email`, `topic`) and selects 3 items
in a list (`selected_sources`), the agent's system prompt contains:

```markdown
# WIDGET CONTEXT

## Form values
- **email**: alice@example.com
- **topic**: 1:1 with Alice

## Session state
- **selected_sources**: ["doc1.md", "doc2.md", "spec.pdf"]

## Last widget tool result
- **rag.query**: {"hits": 12, "score": 0.94}

## Currently mounted widgets
- **w_q_42** (zone=workspace, ref=source_picker)
```

The agent can now reference these values verbatim in its replies
or pass them as args to tool calls.

### How to read the state from a tool

When the agent calls a tool, the tool can read the widget state
either:

**A. Implicitly via the system prompt** - easiest, no code change.
The agent reads the WIDGET CONTEXT section and includes the right
values in its tool call args.

**B. Explicitly via `widget.get_state(key)`** - for tools that need
the value as a string regardless of what the agent wrote:

```python
class RagQueryParams(BaseModel):
    query: str
    sources: list[str] | None = None

@action(params_model=RagQueryParams)
async def rag_query(self, params: RagQueryParams) -> ActionResult:
    sources = params.sources
    if not sources:
        # Fall back to the user's current widget selection
        from digitorn.modules.widget.module import GetStateParams
        widget = self._service_bus.get("widget")
        if widget:
            r = await widget.get_state(GetStateParams(key="selected_sources"))
            if r.data.get("found"):
                sources = r.data["value"]
    # ... do the RAG query with the resolved sources
```

### Example - RAG app where the user picks sources via widgets

```yaml
# rag-app/app.yaml
modules:
  widget: {}
  rag:
    backend: { type: qdrant, path: ./.qdrant }

capabilities:
  grant:
    - module: widget
      actions: [render, update, close, set_state, get_state]
    - module: rag
      actions: [query, multi_query]

agents:
  - id: assistant
    role: coordinator
    brain: { provider: deepseek, model: deepseek-chat }
    system_prompt: |
      You are a RAG assistant. The user picks sources via the side
      panel. Whenever they ask a question, query ONLY the sources
      they currently have selected.

      Look at the WIDGET CONTEXT section below to see the current
      ``selected_sources`` array. If empty, ask the user to pick
      sources first via the panel.

widgets:
  version: 1

  chat_side:
    title: Sources
    icon: library_books
    width: 320
    accent: blue
    data:
      sources:
        type: tool
        tool: rag.list_knowledge_bases
    tree:
      type: column
      gap: 12
      padding: 16
      children:
        - type: text
          text: "Pick the sources to use for your next question"
          variant: caption
          color: muted
        - type: list
          items: '{{sources}}'
          item:
            type: card
            title: '{{item.name}}'
            subtitle: '{{item.doc_count}} docs'
            action:
              # Toggle this source in the user's selection
              action: set_state
              set:
                # The Flutter client appends/removes from the array
                "selected_sources.toggle": '{{item.id}}'
        - type: divider
        - type: stat
          label: Selected
          value: '{{state.selected_sources | length}}'
          icon: check
```
**The flow** :

1. User opens the app → `chat_side` panel loads → `data.sources` resolves
   via `GET /widgets/data/sources` → daemon calls `rag.list_knowledge_bases`
   → returns the list → client renders cards
2. User clicks 3 sources → each click triggers `action: set_state` with
   `selected_sources.toggle: <id>` → daemon adds these IDs to
   `state.selected_sources`
3. Agent loop next turn → `widget.get_prompt_sections()` injects:
   ```
   ## Session state
   - selected_sources: ["src1", "src2", "src3"]
   ```
4. User types "what is the spec for X?" in chat
5. Agent reads the system prompt → sees `selected_sources` → calls
   `rag.query(query="spec for X", sources=["src1","src2","src3"])`
6. RAG returns hits → tool result is auto-stored in `state.last_result`
7. Agent reasons over the hits and replies

**Zero glue code on either side**. The agent reads what's in the
session state, the user sees the result. The `widget` module is the
shared bus.

### Storing arbitrary agent data for widgets to display

The reverse direction works too - the agent can stash a value in
state via `widget.set_state` and then mount a widget that reads it:

```python
# Inside an agent turn
await widget.set_state(set={
    "search_results": [{"title": "...", "url": "..."}, ...],
})
await widget.render(
    zone="inline",
    tree={
        "type": "list",
        "items": "{{state.search_results}}",
        "item": {
            "type": "card",
            "title": "{{item.title}}",
            "subtitle": "{{item.url | truncate(60)}}",
        },
    },
)
```

The Flutter client receives the `widget:render` event AND has
access to the session's state via the snapshot, so `{{state.search_results}}`
resolves correctly when the list renders.

### Recap - the 4 places state can flow to/from

```
┌────────────────────────────────────────────────────────────┐
│                  PER-SESSION WIDGET STATE                  │
│        (one map per session_id, in WidgetModule)           │
│                                                             │
│   form.<field>      ←  POST /widgets/action body.form      │
│   results.<tool>    ←  tool execution from widget action   │
│   last_result       ←  same                                │
│   <any key>         ←  widget.set_state(set={...})         │
│                        OR action: set_state                │
│                                                             │
│  Reads:                                                     │
│   ┌─→ Agent system prompt (WIDGET CONTEXT section)         │
│   ├─→ Tools via widget.get_state(key="...")                │
│   ├─→ Widget templates via {{state.X}} (client-side)       │
│   └─→ /widgets/data/{binding} resolution context           │
└────────────────────────────────────────────────────────────┘
```

**Per-session isolation** is automatic - each session_id has its
own state map and its own prompt section. Two users selecting
different sources in parallel never see each other's selections.

---

## 14. Quickest possible end-to-end example

```yaml
# ── app.yaml ─────────────────────────────────────────────────
modules:
  widget: {}
  llm_provider: {}

capabilities:
  grant:
    - module: widget
      actions: [render, update, close, error]

widgets:
  version: 1

  workspace_tabs:
    - id: greeter
      title: Greeter
      tree:
        type: form
        children:
          - { type: text_input, name: name, label: Your name, required: true }
          - { type: text_input, name: city, label: Your city, required: true }
        submit:
          label: Greet me
          action:
            action: tool
            tool: greet
```
```python
# ── inside any module ─────────────────────────────────────────
class GreetParams(BaseModel):
    name: str
    city: str

@action(description="Greet a user", params_model=GreetParams)
async def greet(self, params: GreetParams) -> ActionResult:
    return ActionResult(
        success=True,
        data={"message": f"Hello {params.name} from {params.city}!"},
    )
```

User opens the app → workspace tab "Greeter" appears → fills the
form → clicks "Greet me" → daemon receives `body.form = {name: "Alice",
city: "Paris"}` → auto-merges into args → calls `greet(GreetParams(name="Alice", city="Paris"))`
→ returns the message → client shows it.

**Zero frontend code. Zero per-app schema duplication. Zero glue.**
