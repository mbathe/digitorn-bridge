# `widgets:` - Declarative UI spec v1

> **Status** : locked on 2026-04-14. Daemon-side fully implemented
> end-to-end (server validates, substitutes, dispatches, stores
> state, proxies streams, accepts uploads). Flutter client renders.

This is the canonical reference. It contains:

1. The full Flutter client spec (primitives, actions, data sources,
   expression language)
2. The daemon integration (compile-time validation, REST API, SSE
   protocol, server-side substitution, form re-validation, stream
   bridge, ephemeral workspace, file upload)
3. End-to-end usage patterns inside an app

---

## Table of contents

1. [Overview](#1-overview)
2. [Versioning](#2-versioning)
3. [Directory layout - `./widgets/*.yaml`](#3-directory-layout)
4. [Zones - Z1/Z2/Z3/Z4](#4-zones)
5. [The `widgets:` block](#5-the-widgets-block)
6. [Universal node fields](#6-universal-node-fields)
7. [Primitives (43)](#7-primitives)
8. [Actions (15)](#8-actions)
9. [Expression language](#9-expression-language)
10. [Data sources](#10-data-sources)
11. [State model](#11-state-model)
12. [SSE protocol](#12-sse-protocol)
13. [REST API](#13-rest-api)
14. [Compile-time validation](#14-compile-time-validation)
15. [Server-side runtime](#15-server-side-runtime)
16. [Integration patterns](#16-integration-patterns)
17. [Icons / colors / theme](#17-icons-colors-theme)
18. [Full examples](#18-full-examples)

---

## 1. Overview

Widgets let any Digitorn app expose dynamic UIs rendered by the
Flutter client **without writing a single line of frontend code**.
The daemon:

1. Parses the `widgets:` block from `app.yaml` (+ every
   `./widgets/*.yaml` file) at compile time and validates the tree.
2. Serves the compiled tree via `GET /api/apps/{id}/widgets`.
3. Resolves per-binding data sources via
   `GET /api/apps/{id}/widgets/data/{binding}` (HTTP / tool / static
   / local / stream).
4. Dispatches user actions via `POST /api/apps/{id}/widgets/action`
   (tool / http / chat / set_state / refresh / open_workspace /
   file upload / …).
5. Pushes live `widget:render` / `widget:update` / `widget:close` /
   `widget:error` events on a per-session SSE channel at
   `GET /api/apps/{id}/sessions/{sid}/widget-events`.
6. Substitutes `{{form.X}}` / `{{state.X}}` / `{{ctx.X}}` / `{{item.X}}`
   **server-side** before emitting render/update events, so the
   client gets concrete values.
7. Re-validates form input rules (required, regex, min/max,
   type_hint) before dispatching the action.
8. Bridges `type: stream` data sources to the client via SSE.

The Flutter client:

1. Calls `GET /widgets` once to get the compiled tree.
2. Subscribes to `/widget-events` for live updates.
3. Renders each primitive in one of the 4 zones (inline /
   chat_side / workspace / modal).
4. Manages local form / state / loop scope.
5. Sends user actions to `/widgets/action` with the filled form
   values.

---

## 2. Versioning

Every `widgets:` block declares:

```yaml
widgets:
  version: 1
```
The daemon **rejects** any unknown version at compile time. The
client refuses to render a version it doesn't support and shows a
"Update your client to version X" fallback.

---

## 3. Directory layout

```
my-app/
├── app.yaml              ← declares widgets: block (optional)
├── package.toml
├── prompts/
├── skills/
└── widgets/              ← one widget per file (optional)
    ├── confirm_delete.yaml
    ├── source_card.yaml
    ├── booking_modal.yaml
    └── source_search.yaml
```

Each file in `./widgets/` is auto-loaded at compile time and
merged into `widgets.inline` under its **file stem** (so
`confirm_delete.yaml` becomes `inline.confirm_delete`, referenceable
by the agent via `widget.render(ref="confirm_delete")`).

Each file is one of:

- A complete `InlineWidget` spec:
  ```yaml
  data: { ... }
  tree:
    type: confirm
    ...
  ```
- A bare tree node (any dict with `type:`), auto-wrapped:
  ```yaml
  type: confirm
  text: Delete?
  ```

**Collision rule:** if an external file has the same name as an
inline entry declared in `app.yaml`, compilation fails with a
clear error.

---

## 4. Zones

The Flutter client renders widgets in 4 zones:

| Zone | Where | YAML key | Visibility |
|---|---|---|---|
| **Z1 `inline`** | Chat bubble in the message stream | `widgets.inline.<name>` | Pushed per turn via `widget.render(zone="inline")` |
| **Z2 `chat_side`** | Companion side panel next to the chat | `widgets.chat_side` | Always visible when the block exists |
| **Z3 `workspace`** | Workspace tab container | `widgets.workspace_tabs[]` | Always visible when non-empty |
| **Z4 `modal`** | Pop-up dialog | `widgets.modals.<name>` | Pushed explicitly via `action: open_modal` |

Responsive rules (client-side):

- Below 980 px width, Z2 collapses into a popover accessible via a
  chat header button.
- Z1 widgets are **never** hidden - they're part of the chat
  history.
- Z3 is a sub-tabbed container: one outer "Widgets" tab, then one
  inner tab per entry.
- Z4 is not persistent - modals are dismissed on close.

---

## 5. The `widgets:` block

Root shape:

```yaml
widgets:
  version: 1

  # Z2 - optional
  chat_side:
    title: Sources
    icon: library_books       # material icon name (closed set)
    collapsible: true
    default_open: true
    accent: blue              # blue | purple | green | orange | red | cyan
    density: normal           # compact | normal | roomy
    width: 300                # 260..420
    data: { ... }             # named data sources - see §10
    tree: { ... }             # widget tree - see §7

  # Z3 - optional array
  workspace_tabs:
    - id: dashboard
      title: Dashboard
      icon: dashboard
      accent: blue
      data: { ... }
      tree: { ... }

  # Z4 - optional dict keyed by modal name
  modals:
    booking:
      title: New booking
      width: 640              # 420 | 560 | 640 | 720 | full
      dismissible: true
      tree: { ... }

  # Z1 - optional dict keyed by widget name, referenceable by `ref:`
  inline:
    confirm_delete:
      data: { ... }
      tree: { ... }
```
---

## 6. Universal node fields

Every node, regardless of `type:`, accepts:

```yaml
type: <primitive>        # REQUIRED - must be in the 43-primitive set
id: my_form              # optional - addressable for set_state / updates
when: '{{count > 0}}'    # conditional render - node shown only if truthy
for: '{{items}}'         # loop - node repeated for each entry
as: item                 # loop alias (default: "item")
key: '{{item.id}}'       # loop stability key
data: { ... }            # local data sources scoped to this sub-tree
accent: green            # override accent for this sub-tree
density: compact         # override density for this sub-tree
hidden: false            # static alias for when: false
```
`when:` and `for:` are evaluated **client-side** (Flutter has the
live view of form/state/loop scope). The daemon validates the
structure but does not execute the predicate.

---

## 7. Primitives

43 primitives grouped in 6 categories. Each primitive has a core
set of fields documented here. The daemon validates each field
name against the closed set.

### 7.1 Layout (9)

#### `column`

```yaml
type: column
gap: 12                   # space between children (px)
align: start              # start | center | end | stretch (cross-axis)
main_align: start         # start | center | end | space_between | space_around
padding: 16               # int, [v,h], or [t,r,b,l]
scrollable: false
children: [ ... ]
```
#### `row`

```yaml
type: row
gap: 8
wrap: false               # line wrap
align: center
main_align: start
children: [ ... ]
```
#### `card`

```yaml
type: card
title: "Section"          # optional
subtitle: "..."
icon: info_outline
elevation: 0              # 0 | 1 | 2
padding: 16
action: { ... }           # whole card is clickable
children: [ ... ]
```
#### `section`

Titled, collapsible group.

```yaml
type: section
title: "Advanced options"
icon: tune
collapsible: true
default_open: false
children: [ ... ]
```
#### `tabs`

```yaml
type: tabs
default: overview         # id of default tab (can be expr)
tabs:
  - { id: overview, title: Overview, icon: dashboard, children: [...] }
  - { id: data,     title: Data,                       children: [...] }
```
#### `split`

```yaml
type: split
direction: horizontal     # horizontal | vertical
ratio: 0.4
first:  { ... }
second: { ... }
```
#### `grid`

```yaml
type: grid
columns: 3                # int OR responsive: { sm: 1, md: 2, lg: 3 }
gap: 12
children: [ ... ]
```
#### `spacer` / `divider`

```yaml
type: spacer
size: 16                  # px OR flex: 1

type: divider
```
### 7.2 Content (4)

#### `markdown`

```yaml
type: markdown
text: |
  ## Hello {{user.name}}
  You have **{{count}}** pending tickets.
# OR loaded remotely
source:
  type: http
  url: /docs/readme.md
```
#### `text`

```yaml
type: text
text: "{{item.title}}"
variant: body             # display | headline | title | body | caption | code
weight: bold              # regular | medium | semibold | bold
color: muted              # text | bright | muted | dim | accent | error | success | warning
max_lines: 2
selectable: true
align: start              # start | center | end
```
#### `image`

```yaml
type: image
src: "{{item.thumbnail}}"
alt: "..."
fit: cover                # cover | contain | fill
width: 120
height: 80
radius: 8
placeholder: image_placeholder
```
#### `icon`

```yaml
type: icon
name: check_circle        # material icon name (closed set)
size: 20
color: success
```
### 7.3 Data display (7)

#### `list`

The workhorse for RAG sources, dynamic rows, anything list-shaped.

```yaml
type: list
items: "{{sources}}"      # expr resolving to an array
empty:                    # rendered when empty
  type: empty_state
  icon: inbox
  title: "No sources yet"
loading:                  # optional, shown while data.loading is true
  type: skeleton
  lines: 3
item:                     # template per entry, scope = item.*
  type: card
  icon: "{{item.kind | source_icon}}"
  title: "{{item.title}}"
  subtitle: "{{item.url | truncate(60)}}"
  action:
    action: chat
    template: "Use source {{item.id}}"
group_by: "{{item.type}}" # optional visual grouping
separator: false
max_height: 480
search:                   # optional built-in search bar
  placeholder: "Search…"
  keys: [title, url, tags]
```
#### `table`

```yaml
type: table
rows: "{{tickets}}"
columns:
  - { key: id,    label: "#",    width: 60, align: end }
  - { key: title, label: Title,  flex: 2 }
  - key: status
    label: Status
    render:                # custom cell (sub-tree)
      type: badge
      label: "{{row.status}}"
      color: "{{row.status | status_color}}"
sortable: true
selectable: false         # false | single | multi
pagination: true
page_size: 20
row_action:
  action: chat
  template: "Open ticket {{row.id}}"
empty: { type: empty_state, ... }
```
#### `chart`

```yaml
type: chart
kind: line                # line | bar | area | pie | donut | scatter | gauge
data: "{{metrics}}"
x: timestamp              # X axis key
series:
  - { y: p50, label: "p50 (ms)", color: blue }
  - { y: p95, label: "p95 (ms)", color: orange }
legend: true
height: 240
x_format: "HH:mm"
y_format: number
```
#### `stat`

```yaml
type: stat
label: Active users
value: "{{users | length}}"
delta: "+12%"
trend: up                 # up | down | flat
icon: trending_up
color: success
```
#### `timeline`

```yaml
type: timeline
items: "{{events}}"
item:
  title: "{{item.title}}"
  subtitle: "{{item.at | relative_time}}"
  icon: "{{item.icon}}"
  color: "{{item.kind | kind_color}}"
  body:                   # optional detailed node
    type: markdown
    text: "{{item.description}}"
```
#### `tree`

```yaml
type: tree
roots: "{{files}}"
children_key: children    # path to children in each node
label: "{{node.name}}"
icon: "{{node.kind | tree_icon}}"
default_expanded: 1       # auto-open depth
on_select:
  action: chat
  template: "Open {{node.path}}"
```
#### `kanban`

```yaml
type: kanban
columns:
  - { id: todo,  title: "To do",        items: "{{tickets | filter('status','todo')}}"  }
  - { id: doing, title: "In progress",  items: "{{tickets | filter('status','doing')}}" }
  - { id: done,  title: "Done",         items: "{{tickets | filter('status','done')}}"  }
card:
  title: "{{item.title}}"
  subtitle: "{{item.assignee}}"
on_move:
  action: tool
  tool: update_ticket
  args: { id: "{{item.id}}", status: "{{to}}" }
```
### 7.4 Input (13) - live inside a `form` ancestor

#### `form`

Root container that collects child input values.

```yaml
type: form
id: booking_form
initial: { topic: "", duration: 30 }     # optional defaults
children: [ ... ]
submit:
  label: Book
  loading_label: "Booking…"
  icon: check
  disabled: "{{!form.valid}}"
  action: { action: tool, tool: create_meeting, args: { ... } }
reset: { label: Reset }                  # optional
on_success:
  action: set_state
  set: { booked: true }
on_error:
  action: alert
  kind: error
  text: "{{error.message}}"
```
#### `text_input`

```yaml
type: text_input
name: email               # → form.email
label: Email
placeholder: you@example.com
required: true
type_hint: email          # text | email | url | password | tel | number
prefix_icon: mail
suffix_icon: help
help: "We'll never share it."
validation:
  regex: "^[^@]+@[^@]+$"
  message: Invalid email
  min: 3
  max: 120
```
#### `textarea`

```yaml
type: textarea
name: notes
label: Notes
rows: 4
max_chars: 500
auto_resize: true
```
#### `select`

```yaml
type: select
name: priority
label: Priority
# STATIC
options:
  - { value: low,  label: Low  }
  - { value: med,  label: Medium }
  - { value: high, label: High }
# OR DYNAMIC
options_from: "{{priorities}}"
option_label: "{{item.name}}"
option_value: "{{item.id}}"
required: true
default: med
searchable: true          # combobox when > N options
```
#### `multi_select`

```yaml
type: multi_select
name: tags
options_from: "{{tags}}"
option_label: "{{item.name}}"
option_value: "{{item.id}}"
max: 5
```
#### `radio` / `checkbox` / `switch`

```yaml
type: radio
name: billing
label: Billing cycle
layout: vertical          # vertical | horizontal
options:
  - { value: month, label: Monthly }
  - { value: year,  label: "Yearly (−20%)" }

type: checkbox
name: terms
label: I agree to the terms
required: true

type: switch
name: notifications
label: Email notifications
default: true
```
#### `slider`

```yaml
type: slider
name: temperature
label: Temperature
min: 0
max: 2
step: 0.1
default: 0.7
show_value: true
marks: [0, 0.5, 1, 1.5, 2]
```
#### `date` / `time` / `datetime`

```yaml
type: date
name: start
label: Start date
min: "{{today}}"
max: "{{today | plus_days(90)}}"
format: "YYYY-MM-DD"
```
#### `file_upload`

```yaml
type: file_upload
name: attachments
label: Attach files
accept: [".pdf", ".png", ".jpg"]
multiple: true
max_size_mb: 10
upload_to:
  url: /rag/upload        # relative to daemon
  field: file             # multipart field name
  # OR omit entirely - defaults to the daemon's generic
  # POST /api/apps/{id}/widgets/upload endpoint (see §15).
```
#### `code_editor`

```yaml
type: code_editor
name: query
label: SQL query
language: sql             # sql | js | python | yaml | json | markdown | http
min_lines: 4
max_lines: 20
line_numbers: true
```
### 7.5 Action (4)

#### `button` / `icon_button`

```yaml
type: button
label: Submit
icon: check
variant: primary          # primary | secondary | ghost | destructive | link
size: md                  # sm | md | lg
full_width: false
loading: "{{busy}}"
disabled: "{{!form.valid}}"
action: { ... }

type: icon_button
icon: delete
tooltip: Delete
variant: ghost
action:
  action: confirm
  text: "Delete {{item.name}}?"
  destructive: true
  then:
    action: tool
    tool: delete_item
    args: { id: "{{item.id}}" }
```
#### `link`

```yaml
type: link
label: Open docs
href: "https://example.com/docs"
external: true
icon: open_in_new
```
#### `confirm`

Inline card with a "are you sure?" prompt.

```yaml
type: confirm
text: "Delete {{row.name}}? This cannot be undone."
confirm_label: Delete
cancel_label: Cancel
destructive: true
confirm_action: { ... }
cancel_action: { action: close }
```
### 7.6 Feedback (5)

#### `alert`

```yaml
type: alert
kind: warning             # info | warning | error | success
title: Quota almost full
text: "You've used {{quota.pct | percent}} of your budget."
icon: warning             # optional
dismissible: true
action:                   # inline CTA
  label: Upgrade
  action: open_url
  url: "https://…"
```
#### `badge` / `progress` / `skeleton` / `empty_state`

```yaml
type: badge
label: "{{row.status}}"
color: success            # success | warning | error | info | muted | accent
variant: soft             # solid | soft | outline
icon: check

type: progress
value: 0.42               # 0..1 OR "indeterminate"
label: "Indexing…"
show_value: true
kind: bar                 # bar | circle

type: skeleton
lines: 3
width: 100%

type: empty_state
icon: inbox
title: No sources yet
subtitle: "Drop a file or URL to get started."
action:
  label: Add source
  action: open_modal
  modal: add_source
```
---

## 8. Actions

15 action types, each dispatched via `POST /widgets/action`. Shape:

```yaml
action: <kind>
... action-specific fields ...
```
### 8.1 `chat` - inject a user message

```yaml
action: chat
template: "Use source {{item.id}}"
silent: false             # if true, not shown in history
tools_hint: [create_ticket]
context:                  # extra context passed with the turn
  source_id: "{{item.id}}"
```
### 8.2 `tool` - invoke an agent tool

```yaml
action: tool
tool: create_meeting
args:
  when:  "{{form.date}}"
  topic: "{{form.topic}}"
on_success:
  action: alert
  kind: success
  text: Booked.
on_error:
  action: alert
  kind: error
  text: "{{error.message}}"
```
**Daemon shortcut:** if `args:` is omitted, all `body.form` fields
are auto-merged into the tool call - no templating needed.

### 8.3 `http` - app-scoped HTTP call

```yaml
action: http
method: POST              # GET | POST | PUT | PATCH | DELETE
url: /rag/sources
body:
  url: "{{form.url}}"
  tags: "{{form.tags}}"
query: { x: 1 }
then_refresh: [sources]   # re-fetch bindings after success
on_success: { ... }
on_error:   { ... }
```
### 8.4 `open_url`

```yaml
action: open_url
url: "{{item.url}}"
external: true
```
### 8.5 `open_workspace` - push to Z3

```yaml
action: open_workspace
tab_id: dashboard         # existing tab OR ↓
ephemeral:
  id: "src_{{item.id}}"
  title: "{{item.title}}"
  tree: { ... }           # or ref: source_details
  ctx: { source: "{{item}}" }
```
When `ephemeral:` is provided, the daemon stores the tab in the
session's widget store (see §15) so it appears in the next
snapshot - no client-side bookkeeping needed.

### 8.6 `open_modal` - open Z4

```yaml
action: open_modal
modal: booking            # key in widgets.modals
ctx: { default_date: "{{today}}" }
```
### 8.7 `close`

```yaml
action: close
```
### 8.8 `set_state`

```yaml
action: set_state
set:
  filter: active
  selected_id: "{{item.id}}"
scope: widget             # widget | global (persisted by appId)
```
### 8.9 `refresh`

```yaml
action: refresh
bindings: [sources, tickets]  # or "all"
```
### 8.10 `copy` / `download`

```yaml
action: copy
text: "{{item.id}}"
toast: Copied

action: download
url: "{{file.url}}"
filename: "{{file.name}}"
```
### 8.11 `navigate`

```yaml
action: navigate
app: my_other_app         # optional
workspace_tab: tickets    # optional
```
### 8.12 `confirm`

Wraps a destructive action with a confirmation prompt.

```yaml
action: confirm
text: "Delete this source?"
confirm_label: Delete
destructive: true
then:
  action: tool
  tool: delete
  args: { id: "{{item.id}}" }
```
### 8.13 `sequence`

Run multiple actions in order.

```yaml
action: sequence
steps:
  - { action: tool, tool: save, args: { ... } }
  - { action: refresh, bindings: [items] }
  - { action: close }
stop_on_error: true       # default true
```
### 8.14 `alert`

Show a toast.

```yaml
action: alert
kind: success
text: "Saved"
```
---

## 9. Expression language

The full binding syntax from §6 of the locked spec. The daemon
evaluates these **server-side** when rendering a tree via
`widget.render` or patching one via `widget.update`. The Flutter
client evaluates the same grammar locally for `when:` / `for:` /
live form substitution.

### 9.1 Scopes

| Scope | Contents | Mutable? |
|---|---|---|
| `form.*` | Values of form inputs with `name:` | yes (via input) |
| `form.valid / form.dirty / form.errors.<name>` | Form meta | no |
| `state.*` | Widget-local state | yes (via `set_state`) |
| `data.*` | Resolved data sources | yes (via `refresh`) |
| `data.<k>.loading` / `.error` / `.stale` | Data meta | no |
| `row.*` / `item.*` | Current loop scope | no |
| `index` / `first` / `last` | Loop meta | no |
| `ctx.*` | Context passed by the agent at render time | no |
| `session.*` | user, session_id, app_id, turn_id | no |
| `app.*` | app.id, app.name, app.config.* | no |
| `today` / `now` | Current date/time | no |

### 9.2 Syntax

```
{{var}}                         # lookup
{{a.b.c}}                       # dotted path
{{list[0]}}                     # index
{{form.email}}
{{count > 0}}                   # comparison
{{status == "ok"}}
{{items is empty}}
{{items is not empty}}
{{!loading}}                    # negation
{{a && b}}   {{a || b}}         # logic
{{x | filter1 | filter2}}       # pipeline
{{name | default('-')}}         # filter with arg
{{a ? "yes" : "no"}}            # ternary
```

No loops, no `if/else` inside expressions - those live at the
node level via `when:` / `for:`.

### 9.3 Built-in filters (24, closed set)

| Filter | Example | Result |
|---|---|---|
| `upper` / `lower` / `title` | `{{x \| upper}}` | Case |
| `truncate(n)` | `{{x \| truncate(40)}}` | `"foo…"` |
| `default(v)` | `{{x \| default('-')}}` | Fallback |
| `length` | `{{items \| length}}` | int |
| `date(fmt)` | `{{t \| date('YYYY-MM-DD')}}` | Date |
| `relative_time` | `{{t \| relative_time}}` | `"2h ago"` |
| `money(cur)` | `{{n \| money('EUR')}}` | `"€12.30"` |
| `number(p)` | `{{n \| number(2)}}` | `"3.14"` |
| `percent` | `{{n \| percent}}` | `"42%"` |
| `json` | `{{obj \| json}}` | JSON string |
| `filter(k,v)` | `{{items \| filter('status','ok')}}` | Sublist |
| `map(k)` | `{{items \| map('id')}}` | Projection |
| `pluck(k)` | `{{obj \| pluck('name')}}` | Alias map |
| `join(sep)` | `{{l \| join(', ')}}` | String |
| `first` / `last` | `{{l \| first}}` | Item |
| `sort(k)` | `{{l \| sort('at')}}` | Sorted |
| `reverse` | `{{l \| reverse}}` | Reversed |
| `slice(a,b)` | `{{l \| slice(0,5)}}` | Subarray |
| `replace(a,b)` | `{{s \| replace('_',' ')}}` | Replaced |
| `markdown` | `{{t \| markdown}}` | Inline render |
| `plus_days(n)` / `minus_days(n)` | `{{today \| plus_days(7)}}` | Date math |

Unknown filters raise a compile-time error with a clear message.

---

## 10. Data sources

Under `data:` (at chat_side / workspace_tab / modal / inline
level), each key declares a named binding. The client fetches
via `GET /widgets/data/{binding}` and hydrates live.

### 10.1 HTTP

```yaml
data:
  sources:
    type: http
    method: GET
    url: /rag/sources        # relative to the daemon
    headers: { Accept: application/json }
    query: { limit: 50, filter: "{{state.filter}}" }
    body: { ... }            # for POST
    poll: 5s                 # refetch every 5s (0 = off)
    cache: 30s               # TTL
    debounce: 300ms
    transform: "{{response.data.sources}}"
    when: "{{state.filter != null}}"
```
### 10.2 Tool

```yaml
data:
  summary:
    type: tool
    tool: summarize_docs
    args: { ids: "{{state.selected}}" }
    auto: true               # fetch at mount
```
The daemon resolves the tool via the action registry of every
loaded module - same resolver as widget actions.

### 10.3 Static

```yaml
data:
  priorities:
    type: static
    value:
      - { id: low,  name: Low }
      - { id: med,  name: Medium }
      - { id: high, name: High }
```
### 10.4 Stream (SSE)

```yaml
data:
  live_metrics:
    type: stream
    url: /metrics/live
    reducer: append          # replace | append | merge
    limit: 500
    poll: 5s                 # used if upstream is non-SSE JSON
    when: "{{state.follow}}"
```
The daemon proxies via `GET /widgets/data/{binding}/stream`.
Auto-detects whether the upstream serves `text/event-stream`
(bridge pass-through) or JSON (HTTP poll with interval).

### 10.5 Local (SharedPreferences)

```yaml
data:
  cart:
    type: local
    key: cart.v1
    default: []
```
Client-side only - the daemon returns the declared `default` as
the initial value.

---

## 11. State model

### 11.1 Form state

- Collected automatically by the nearest `form` ancestor.
- Each input with a `name:` becomes `form.<name>`.
- `form.valid` / `form.dirty` / `form.errors.<name>` are auto-filled.
- Validation: `required`, `regex`, `min`, `max`, `type_hint`
  (email, url, number, tel).

### 11.2 Widget state (`state.*`)

- Mutated via `action: set_state` or `widget.set_state` (agent-side).
- `scope: widget` (default) - reset on unmount.
- `scope: global` - persisted in SharedPreferences per `appId`,
  under key `widget.state.<appId>`.

### 11.3 Loop scope

- Inside a node with `for:`, the current item is bound to the
  alias from `as:` (default `item`).
- Meta vars: `index`, `first`, `last`.

### 11.4 Data state

- `data.<k>` - the resolved value.
- `data.<k>.loading` / `data.<k>.error` / `data.<k>.stale` - meta.

### 11.5 Context (`ctx.*`)

- Read-only, passed by the agent at `widget.render` time or by
  the client when it opens a modal / ephemeral workspace.

### 11.6 Per-session isolation - server-side store

The daemon keeps a `WidgetSessionStore` keyed by `session_id`.
Each session has its own:

- `state` map (form, custom keys, results, uploads)
- `mounted` map (widgets currently on screen)
- `events` ring buffer (last 500 for snapshot replay)

Two users opening two sessions in parallel **never cross-talk**.

### 11.7 Widget state → agent prompt (⭐ the big win)

Every turn, the daemon rebuilds the agent's system prompt and
injects a `WIDGET CONTEXT` section containing:

```markdown
# WIDGET CONTEXT

## Form values
- email: alice@example.com
- topic: 1:1 sync

## Session state
- selected_sources: ["doc1.md", "doc2.md", "spec.pdf"]

## Last widget tool result
- rag.query: {"hits": 12, "score": 0.94}

## Currently mounted widgets
- w_q_42 (zone=workspace, ref=source_picker)
```

The agent reads this on every turn, so it can reference form
values, selections, and tool results without any templating.
See §16 for RAG example.

---

## 12. SSE protocol

### 12.1 Client → daemon events

The client POSTs actions via REST (`/widgets/action`). Real
bidirectional flow goes through the SSE stream at:

```
GET /api/apps/{id}/sessions/{sid}/widget-events
```

### 12.2 Daemon → client events

#### `widget:render` - mount or replace

```json
{
  "event": "widget:render",
  "data": {
    "zone": "inline",
    "target": null,
    "widget_id": "w_abc123",
    "ref": "confirm_delete",
    "tree": { "type": "card", ... },
    "ctx": { "path": "/foo" },
    "turn_id": "t_123"
  }
}
```

#### `widget:update` - patch

```json
{
  "event": "widget:update",
  "data": {
    "widget_id": "w_abc123",
    "patch": {
      "data.sources": [ ... ],
      "state.filter": "active"
    }
  }
}
```

#### `widget:close` - unmount

```json
{ "event": "widget:close", "data": { "widget_id": "w_abc123" } }
```

#### `widget:error` - error without unmount

```json
{
  "event": "widget:error",
  "data": {
    "widget_id": "w_abc123",
    "binding": "sources",
    "message": "Backend timeout"
  }
}
```

#### `snapshot` - sent once on connect

Full state replay so the client can hydrate instantly even after
a reload. Contains all currently mounted widgets, the state map,
and the last 500 events.

---

## 13. REST API

All routes under `/api/apps/{app_id}/`, JWT-authenticated
(standard `Authorization: Bearer` header OR `?token=<jwt>` query
param for iframes / `EventSource`).

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/widgets` | Full compiled tree |
| `GET` | `/widgets/validate` | Lint (no deploy) |
| `GET` | `/widgets/data/{binding}` | Resolve a named binding (http / tool / static / local / stream first frame) |
| `GET` | `/widgets/data/{binding}/stream` | SSE bridge for stream bindings |
| `POST` | `/widgets/action` | Dispatch a user action (tool / http / chat / set_state / open_workspace / sequence / …) |
| `POST` | `/widgets/upload` | Multipart file upload (used by `file_upload` primitive) |
| `GET` | `/widgets/upload/{user_id}/{sid}/{file_id}/{filename}` | Serve a previously uploaded file back |
| `GET` | `/sessions/{sid}/widget-events` | SSE stream of render/update/close/error events |

---

## 14. Compile-time validation

The compiler **rejects** a deploy with a precise YAML path if:

1. `type:` is not in the 43-primitive set
2. `action:` is not in the 15-action set
3. `accent:` is not in `{blue, purple, green, orange, red, cyan}`
4. `density:` is not in `{compact, normal, roomy}`
5. `version:` is not 1
6. Two inputs in the same form share a `name:`
7. An external `./widgets/X.yaml` collides with an inline entry
8. A form's `submit.action` is malformed
9. An action's `kind` is not a string
10. A filter referenced in a `{{...}}` pipeline is not in the
    24 closed set
11. `ref:` points to an inline widget that doesn't exist
12. A `for:` without `key:` on a list > 100 entries (warning)
13. `icon:` not in the material icon set (warning)

Errors format:

```
widgets.chat_side.tree.children[1].action.action:
  unknown action "chatt" (did you mean "chat"?)
  at app.yaml:42:8
```

---

## 15. Server-side runtime

### 15.1 Template substitution

When the agent calls `widget.render(tree=...)` or
`widget.update(patch=...)`, the daemon walks every string leaf in
the tree/patch and substitutes `{{...}}` tokens against the live
session state **before** publishing the SSE event. This means:

```python
await widget.render(
    zone="inline",
    tree={"type": "card", "title": "Hello {{form.name}}"},
)
```

If `session.state.form.name == "Alice"`, the client receives a
tree with `title: "Hello Alice"` already baked in. No client-side
coordination of the form state required.

The `ctx:` passed by the agent is exposed as `ctx.*` in the same
evaluator - so `{{ctx.path}}` resolves against the dict the agent
gave.

### 15.2 Form re-validation

Every `POST /widgets/action` with a non-empty `body.form` goes
through `validate_form_values(inputs, form)`. The daemon walks the
compiled tree, finds every form input by name, and re-runs the
same rules the client applied:

- `required`
- `regex` + `message`
- `min` / `max` (string length, numeric range, or list size)
- `type_hint` (email / url / number / tel)
- `multi_select.max` cap
- `checkbox.required` must be truthy

Failure returns `400` with a structured payload:

```json
{
  "detail": {
    "error": "form_validation_failed",
    "fields": {
      "email": "must be a valid email",
      "topic": "topic must be at least 3 characters"
    }
  }
}
```

### 15.3 Stream bridge

`GET /widgets/data/{binding}/stream` probes the upstream URL
declared in the `type: stream` data source. Two modes:

1. **SSE pass-through** - upstream returns `text/event-stream` →
   daemon bridges frames 1-to-1.
2. **HTTP poll** - upstream returns JSON → daemon polls every
   `poll:` seconds (default 5s) and emits one `event: data` frame
   per response.

A first `event: meta` frame carries the `reducer` hint (`replace`
/ `append` / `merge`) and `limit` from the YAML.

### 15.4 Ephemeral workspace

When `action: open_workspace` arrives with an `ephemeral:` block,
the daemon stores it in the session's widget store as a virtual
`mounted` widget with `zone=workspace`. The next `/widgets` or
`/widget-events snapshot` call includes it, so the client renders
the new tab alongside the declared `workspace_tabs[]`.

The ephemeral tree goes through the same `{{...}}` substitution
pipeline as `widget.render`.

### 15.5 File upload pipeline

`POST /widgets/upload` (multipart) accepts:

- `file` - the uploaded file
- `session_id` - optional, defaults to `_default_`
- `binding` - optional, recorded in state for traceability

It stores the file at:

```
~/.local/share/digitorn/uploads/{user_id}/{session_id}/{file_id}/{filename}
```

Returns:

```json
{
  "data": {
    "file_id": "abc123",
    "filename": "spec.pdf",
    "size": 240384,
    "content_type": "application/pdf",
    "url": "/api/apps/my-app/widgets/upload/alice/sess_xyz/abc123/spec.pdf"
  }
}
```

The `file_id` is also promoted into `state.uploads[file_id]` so
the agent / next form submission can reference it without a
round-trip. The served URL is per-user scoped - only the owner
(or admin) can read it back.

Apps that need custom upload handling (validation, virus scan,
indexing) can still declare their own `upload_to.url` on the
`file_upload` primitive.

### 15.6 Form auto-merge into tool args

When a `submit.action` is `tool`-typed and the `args:` map omits
some fields, the daemon automatically merges `body.form` entries
into `payload.args` (without overwriting existing keys). This
makes the simplest form "just work":

```yaml
type: form
children:
  - { type: text_input, name: topic }
  - { type: text_input, name: when }
submit:
  label: Book
  action:
    action: tool
    tool: create_meeting
    # no args: - the daemon passes {topic, when} straight into the tool
```
---

## 16. Integration patterns

### 16.1 Form → tool round-trip

1. User fills the form in `modals.booking`.
2. Client validates locally (`required`, `min`, `max`, …).
3. Client POSTs `/widgets/action` with `type: tool`, the tool
   name, and `form: {...}`.
4. Daemon re-validates the form server-side.
5. Daemon persists `form.*` into `state.form` + `state.last_form`.
6. Daemon merges `form` into tool args, resolves the tool via the
   action registry, calls it.
7. Daemon stores the result in `state.results.<tool>` +
   `state.last_result`.
8. Next agent turn → `WIDGET CONTEXT` section in the system
   prompt contains the form values + the tool result.

### 16.2 Agent pushes a widget live

```python
# Any module action the agent runs
result = await widget.render(
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
    ctx={"count": 128, "rows": [{"label": "A", "value": 12}]},
)
```

1. Daemon substitutes `{{ctx.count}}` → 128, `{{ctx.rows}}` → …
2. Publishes a `widget:render` SSE event.
3. Client receives the event and renders the card.
4. Agent gets back a `widget_id` it can pass to `widget.update` /
   `widget.close` later.

### 16.3 RAG - user picks sources, agent uses them

The canonical "widgets are a bidirectional variable bus" example:

```yaml
modules:
  widget: {}
  rag:
    backend: { type: qdrant, path: ./.qdrant }

capabilities:
  grant:
    - module: widget
      actions: [render, update, close, set_state, get_state]
    - module: rag
      actions: [query, multi_query, list_knowledge_bases]

agents:
  - id: assistant
    role: coordinator
    brain: { provider: deepseek, model: deepseek-chat }
    system_prompt: |
      You are a RAG assistant. The user picks sources via the side
      panel. Whenever they ask a question, query ONLY the sources
      they currently have selected.

      Look at the WIDGET CONTEXT section below. If ``state.selected_sources``
      is empty, ask the user to pick sources first. Otherwise, call
      rag.query with ``sources=state.selected_sources``.

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
              action: set_state
              set:
                # client toggles the id in/out of the array
                "selected_sources.toggle": '{{item.id}}'
        - type: divider
        - type: stat
          label: Selected
          value: '{{state.selected_sources | length}}'
          icon: check
```
Flow :

1. User opens the app → chat_side panel mounts.
2. Daemon resolves `data.sources` via
   `GET /widgets/data/sources` → calls `rag.list_knowledge_bases`
   → returns the list → client renders cards.
3. User clicks 3 source cards → each triggers
   `action: set_state` with `selected_sources.toggle: <id>`.
4. Daemon persists `state.selected_sources = ["s1", "s2", "s3"]`.
5. User types a question in the chat.
6. Agent next turn → system prompt contains:
   ```
   ## Session state
   - selected_sources: ["s1", "s2", "s3"]
   ```
7. Agent calls `rag.query(query=..., sources=["s1","s2","s3"])`.
8. Daemon stores the result in `state.last_result`.
9. Agent reads the hits and replies.

**Zero glue code.** The widget module is the shared bus.

### 16.4 Storing agent output for widgets to display

The reverse direction: the agent stashes a value and mounts a
widget that reads it.

```python
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

Daemon substitutes `{{state.search_results}}` before emitting the
render event - the client receives a list pre-populated with the
agent's output.

---

## 17. Icons / colors / theme

- **Icons**: closed set = Material Icons Round. The daemon
  validates against a generated catalogue.
- **Semantic colors**: `success`, `warning`, `error`, `info`,
  `accent`, `muted`. Never hex literals.
- **Accents**: `blue`, `purple`, `green`, `orange`, `red`, `cyan`.
- **Density**: `compact` (padding −25 %, font −1 px), `normal`
  (default), `roomy` (padding +25 %).
- **Radius (locked)**: cards 10, inputs 7, badges 4, modals 14.
- **Fonts**: Inter (UI), Fira Code (code).

Per-app custom themes are **not** supported. The client applies
the user's theme (dark/light) + the `accent:` declared in the
widget spec. Three months of development, everything stays
visually consistent.

---

## 18. Full examples

### 18.1 RAG sources panel (Z2)

```yaml
widgets:
  version: 1
  chat_side:
    title: Sources
    icon: library_books
    accent: blue
    data:
      sources:
        type: http
        url: /rag/sources
        poll: 10s
    tree:
      type: column
      gap: 10
      padding: 12
      children:
        - type: row
          gap: 8
          children:
            - type: text_input
              name: q
              placeholder: "Search sources…"
              prefix_icon: search
            - type: icon_button
              icon: add
              tooltip: Add source
              action: { action: open_modal, modal: add_source }
        - type: stat
          label: Indexed
          value: "{{sources | length}}"
          icon: storage
        - type: list
          items: "{{sources | filter_search(form.q)}}"
          empty:
            type: empty_state
            icon: inbox
            title: No sources
            subtitle: "Click + to add one."
          item:
            type: card
            icon: "{{item.kind | source_icon}}"
            title: "{{item.title}}"
            subtitle: "{{item.url | truncate(60)}}"
            action:
              action: chat
              template: "Use source {{item.id}} for my next answer"

  modals:
    add_source:
      title: Add RAG source
      width: 560
      tree:
        type: form
        initial: { kind: url }
        children:
          - type: radio
            name: kind
            label: Type
            options:
              - { value: url,  label: URL }
              - { value: file, label: File }
              - { value: text, label: "Raw text" }
          - type: text_input
            name: url
            label: URL
            when: "{{form.kind == 'url'}}"
            required: true
          - type: file_upload
            name: file
            label: File
            when: "{{form.kind == 'file'}}"
            required: true
            accept: [.pdf, .md, .txt]
          - type: textarea
            name: text
            label: Text
            when: "{{form.kind == 'text'}}"
            rows: 6
            required: true
        submit:
          label: Add
          action:
            action: sequence
            steps:
              - action: tool
                tool: add_rag_source
              - { action: refresh, bindings: [sources] }
              - { action: close }
```
### 18.2 Ops dashboard (Z3)

```yaml
widgets:
  version: 1
  workspace_tabs:
    - id: ops
      title: Ops
      icon: monitoring
      accent: blue
      data:
        metrics:   { type: http, url: /metrics/summary, poll: 5s }
        incidents: { type: http, url: /incidents,       poll: 30s }
      tree:
        type: column
        padding: 20
        gap: 20
        children:
          - type: row
            gap: 16
            children:
              - type: stat
                label: Uptime
                value: "{{metrics.uptime_pct | percent}}"
                trend: up
                icon: trending_up
                color: success
              - type: stat
                label: p95 latency
                value: "{{metrics.p95_ms}}ms"
                color: "{{metrics.p95_ms > 200 ? 'warning' : 'success'}}"
              - type: stat
                label: Incidents 24h
                value: "{{incidents | length}}"
                color: "{{incidents | length > 0 ? 'error' : 'success'}}"
          - type: card
            title: "Latency last 60 min"
            children:
              - type: chart
                kind: line
                data: "{{metrics.latency_series}}"
                x: t
                series:
                  - { y: p50, label: p50, color: blue   }
                  - { y: p95, label: p95, color: orange }
                  - { y: p99, label: p99, color: red    }
                height: 260
          - type: card
            title: "Active incidents"
            children:
              - type: table
                rows: "{{incidents}}"
                columns:
                  - { key: id, label: "#", width: 60 }
                  - { key: title, label: Title, flex: 2 }
                  - key: severity
                    label: Sev
                    render:
                      type: badge
                      label: "{{row.severity | upper}}"
                      color: "{{row.severity | sev_color}}"
                      variant: soft
                  - { key: opened_at, label: Opened }
                row_action:
                  action: chat
                  template: "Incident {{row.id}}: summarize and suggest fixes"
```
### 18.3 Onboarding wizard (Z4)

```yaml
widgets:
  version: 1
  modals:
    onboarding:
      title: Quick setup
      width: 640
      tree:
        type: tabs
        default: "{{state.step | default('profile')}}"
        tabs:
          - id: profile
            title: "1. Profile"
            icon: person
            children:
              - type: form
                id: f_profile
                children:
                  - { type: text_input, name: name,  label: Name,  required: true }
                  - { type: text_input, name: email, label: Email, required: true, type_hint: email }
                submit:
                  label: Next
                  action: { action: set_state, set: { step: prefs } }
          - id: prefs
            title: "2. Preferences"
            icon: tune
            children:
              - type: form
                id: f_prefs
                children:
                  - type: select
                    name: lang
                    label: Language
                    options:
                      - { value: en, label: English }
                      - { value: fr, label: Français }
                  - type: switch
                    name: notif
                    label: Email notifications
                    default: true
                submit:
                  label: Finish
                  action:
                    action: sequence
                    steps:
                      - action: tool
                        tool: save_onboarding
                      - { action: close }
```
### 18.4 Inline confirmation pushed by the agent (Z1)

```yaml
# widgets/confirm_delete_file.yaml
tree:
  type: confirm
  text: "Delete `{{ctx.path}}`? This cannot be undone."
  confirm_label: Delete
  destructive: true
  confirm_action:
    action: sequence
    steps:
      - { action: tool, tool: delete_file, args: { path: "{{ctx.path}}" } }
      - { action: chat, template: "Deleted {{ctx.path}}", silent: true }
```
Agent push :

```python
await widget.render(
    zone="inline",
    ref="confirm_delete_file",
    ctx={"path": "/docs/a.md"},
)
```

Daemon substitutes `{{ctx.path}}` → `/docs/a.md` before emitting
the SSE event, client renders the confirmation card in the chat.

---

**Everything that was in the locked Flutter v1 spec is supported.
Everything that moves a value from the UI to the agent (and back)
goes through one surface: the per-session widget store served
behind `/api/apps/{id}/widgets/*` and
`/api/apps/{id}/sessions/{sid}/widget-events`.**
