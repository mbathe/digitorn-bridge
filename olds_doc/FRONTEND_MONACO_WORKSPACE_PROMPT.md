# Frontend integration - Monaco editor in Flutter for workspace diff

## Your mission

Build a **VS Code-style pending-diff experience** in a Monaco editor
embedded in the Flutter client. The user must see:

1. Live file tree with per-file pending-change badges
   (`insertions_pending` / `deletions_pending`)
2. Every edit the agent makes streams into the editor in real time
3. VS Code-style **gutter decorations**: green for pending insertions,
   red for pending deletions, yellow for modifications
4. **Inline diff view** that can be toggled - side-by-side baseline ↔
   current, with hunk-by-hunk navigation
5. **Approve / Reject actions** - whole file OR per-hunk
6. All of the above **survives daemon restart** (state is server-
   authoritative, you never accumulate diffs client-side)

The server does the heavy lifting: `unified_diff_pending` is always
the cumulative diff baseline→current, pre-computed and in sync with
`insertions_pending` / `deletions_pending`. You render and route user
actions.

---

## Server contract - what you receive

### Per-file payload shape (from live channel + HTTP)

```ts
interface WorkspaceFilePayload {
  // Identity
  path: string;                  // "src/foo.py" - workspace-relative
  content: string;               // CURRENT content
  size: number;
  lines: number;
  updated_at: number;            // unix seconds

  // Language hint for Monaco
  language: string;              // "python" | "typescript" | ...

  // Pending validation state - what the UI renders
  validation: "pending" | "approved";
  insertions_pending: number;    // lines added vs baseline
  deletions_pending: number;     // lines removed vs baseline
  baseline_lines: number;        // line count of the last approved baseline
  unified_diff_pending: string;  // FULL cumulative unified diff baseline→current
                                 //   (capped at 16 000 chars)

  // Per-edit metadata (for chat tool-chip view, not for the diff gutter)
  operation: "write" | "edit" | "delete" | "writeback";
  diff: string;                  // short one-line summary
  unified_diff: string;          // THIS edit's delta (old→new, NOT cumulative)
  status?: "added" | "modified" | "deleted";

  // Cumulative counters (whole session, not reset by approve)
  total_insertions: number;
  total_deletions: number;

  // When user edited manually via PUT writeback
  source?: "user";

  // git info (optional, present when workspace is a git repo)
  git_status?: { branch: string; dirty: boolean; /* ... */ };
}
```

### Endpoints

| Verb | Path | Purpose |
|---|---|---|
| GET | `/api/apps/{app}/sessions/{sid}/workspace` | Workspace summary (file list, render mode, entry file, dirty flag) |
| GET | `/api/apps/{app}/sessions/{sid}/workspace/files/{path}?include_baseline=true` | Full payload + `baseline` string + `unified_diff_pending` at top level |
| GET | `/api/apps/{app}/sessions/{sid}/workspace/files/{path}/history` | Revision list (approved snapshots) |
| GET | `/api/apps/{app}/sessions/{sid}/workspace/code-snapshot` | File tree + metadata only (no content — fetch each file via `files/{path}`) |
| GET | `/api/apps/{app}/sessions/{sid}/workspace/preview-snapshot` | Live preview state (resources, channels, events) |
| GET | `/api/apps/{app}/sessions/{sid}/workspace/changes` | Pending diff vs baseline across all files |
| GET | `/api/apps/{app}/sessions/{sid}/workspace/export` | Portable JSON dump of the full workspace |
| POST | `/api/apps/{app}/sessions/{sid}/workspace/import` | Restore a workspace from an `export` payload |
| POST | `/api/apps/{app}/sessions/{sid}/workspace/fork` | New session pre-populated from another session's export |
| PUT | `/api/apps/{app}/sessions/{sid}/workspace/files/{path}` | User writeback (`{ "content": "...", "auto_approve"?: bool }`) |
| POST | `/api/apps/{app}/sessions/{sid}/workspace/files/approve` | `{ "path": "..." }` - stage whole file |
| POST | `/api/apps/{app}/sessions/{sid}/workspace/files/reject` | `{ "path": "..." }` - revert to baseline |
| POST | `/api/apps/{app}/sessions/{sid}/workspace/files/approve-hunks` | `{ "path": "...", "hunks": [0, 2] }` - stage specific hunks (by index OR 12-char hash) |
| POST | `/api/apps/{app}/sessions/{sid}/workspace/files/reject-hunks` | `{ "path": "...", "hunks": [1] }` - revert specific hunks |
| POST | `/api/apps/{app}/sessions/{sid}/workspace/commit` | `git commit` over approved files |
| POST | `/api/apps/{app}/sessions/{sid}/workspace/git-status` | Refresh `git_status` on tracked files |

> **No REST DELETE for files.** File deletion is done by the agent
> calling its `workspace.delete` (`WsDelete`) action, which then
> emits a `preview:resource_deleted` event the client picks up.
> Clients drive deletion via the chat (ask the agent to delete) or
> a future explicit endpoint.

### Live updates via Socket.IO

Join the session room; you receive `event` messages with `type`
matching `preview:resource_*`:

```jsonc
// File created / updated / touched
{
  "type": "preview:resource_set",
  "payload": {
    "channel": "files",
    "id": "src/foo.py",        // path
    "payload": { /* WorkspaceFilePayload - full */ }
  }
}

// Partial update (rare - typically validation flip on approve)
{
  "type": "preview:resource_patched",
  "payload": {
    "channel": "files",
    "id": "src/foo.py",
    "patch": {
      "validation": "approved",
      "insertions_pending": 0,
      "deletions_pending": 0,
      "unified_diff_pending": ""
    }
  }
}

// File deleted
{
  "type": "preview:resource_deleted",
  "payload": { "channel": "files", "id": "src/foo.py" }
}

// Bulk replace (rare - full state reset)
{
  "type": "preview:resource_bulk_set",
  "payload": {
    "channel": "files",
    "items": [ { "id": "a.py", "payload": { /* ... */ } }, ... ],
    "replace": true            // true = this is the new complete state
  }
}

// Initial snapshot on reconnect
{
  "type": "preview:snapshot",
  "payload": { /* full resource map by channel */ }
}
```

Keep a client-side state:

```ts
interface FilesState {
  [path: string]: WorkspaceFilePayload;
}
```

Reducer rule: `set` replaces, `patch` merges into existing, `delete`
removes, `bulk_set.replace=true` wipes and replaces, `snapshot`
re-hydrates everything.

---

## Architecture - Flutter ↔ Monaco bridge

```
┌─────────────────────────────────────┐
│        Flutter app (Dart)           │
│                                     │
│  ┌───────────────────────────────┐  │
│  │  SocketIO client              │  │
│  │  HTTP client (dio / http)     │  │
│  │  FilesState (BLoC / Riverpod) │  │
│  └──────────────┬────────────────┘  │
│                 │ postMessage        │
│  ┌──────────────▼────────────────┐  │
│  │  InAppWebView / webview_flutter │
│  │                                 │
│  │  ┌─────────────────────────┐   │
│  │  │  Monaco editor          │   │
│  │  │  + diff decorations     │   │
│  │  │  + side-by-side modal   │   │
│  │  │  + hunk action widgets  │   │
│  │  └─────────────────────────┘   │
│  └─────────────────────────────────┘│
└─────────────────────────────────────┘
```

**Two transport channels:**
- Flutter → Webview: `controller.evaluateJavascript(...)` or
  `callJavascriptFunction(...)` - push state updates + user actions
- Webview → Flutter: `JavascriptHandler` / `JavascriptChannel` - bubble
  Monaco user actions (save, approve-hunk, etc.) back to Dart

**Why not Socket.IO directly in webview?** Simpler to keep all network
auth + token refresh in Dart. The webview is a pure presentation layer.

---

## Monaco integration - the diff gutter

Monaco has first-class support for VS Code-style diff indicators via
**model decorations**. You don't need the `DiffEditor` API for the
gutter - you parse the unified diff into line ranges and paint them on
the normal editor instance.

### Step 1 - parse the unified diff

```ts
type DiffKind = "insert" | "delete" | "modify";
interface DiffLineRange {
  startLine: number;             // 1-based, in the CURRENT content
  endLine: number;
  kind: DiffKind;
  hunkIndex: number;             // 0-based, stable hunk ID
  hunkHash: string;              // server-computed 12-char hash (preferred stable id)
}

function parseUnifiedDiff(diff: string): DiffLineRange[] {
  const ranges: DiffLineRange[] = [];
  const lines = diff.split("\n");
  let hunkIndex = -1;
  let newLineNo = 0;
  let currentKind: DiffKind | null = null;
  let rangeStart = 0;

  for (const line of lines) {
    if (line.startsWith("@@")) {
      // header: @@ -oldStart,oldLen +newStart,newLen @@
      const match = line.match(/\+(\d+)(?:,(\d+))?/);
      if (!match) continue;
      hunkIndex++;
      newLineNo = parseInt(match[1], 10);
      currentKind = null;
      continue;
    }
    if (line.startsWith("+++") || line.startsWith("---")) continue;

    if (line.startsWith("+")) {
      if (currentKind !== "insert") {
        if (currentKind) flush();
        currentKind = "insert";
        rangeStart = newLineNo;
      }
      newLineNo++;
    } else if (line.startsWith("-")) {
      if (currentKind !== "delete") {
        if (currentKind) flush();
        currentKind = "delete";
        rangeStart = newLineNo;
      }
      // deleted lines don't advance newLineNo
    } else {
      // context
      if (currentKind) flush();
      currentKind = null;
      newLineNo++;
    }
  }
  if (currentKind) flush();

  function flush() {
    ranges.push({
      startLine: rangeStart,
      endLine: Math.max(rangeStart, newLineNo - 1),
      kind: currentKind!,
      hunkIndex,
      hunkHash: "",   // fill from /history or re-query if needed
    });
  }
  return ranges;
}
```

### Step 2 - apply decorations on the current model

```ts
import * as monaco from "monaco-editor";

function paintDiffGutter(
  editor: monaco.editor.IStandaloneCodeEditor,
  ranges: DiffLineRange[],
) {
  const decos: monaco.editor.IModelDeltaDecoration[] = ranges.map((r) => ({
    range: new monaco.Range(r.startLine, 1, r.endLine, 1),
    options: {
      isWholeLine: true,
      linesDecorationsClassName:
        r.kind === "insert" ? "pending-insert"
        : r.kind === "delete" ? "pending-delete"
        : "pending-modify",
      overviewRuler: {
        color:
          r.kind === "insert" ? "#487e02"
          : r.kind === "delete" ? "#a1260d"
          : "#bf8803",
        position: monaco.editor.OverviewRulerLane.Left,
      },
      minimap: {
        color:
          r.kind === "insert" ? "#487e0290"
          : r.kind === "delete" ? "#a1260d90"
          : "#bf880390",
        position: monaco.editor.MinimapPosition.Inline,
      },
    },
  }));
  // Store the decoration IDs so we can replace them on next update.
  return editor.deltaDecorations(/* previous = */ [], decos);
}
```

CSS (inject into the webview's HTML):

```css
.monaco-editor .pending-insert {
  border-left: 3px solid #487e02;
  margin-left: 3px;
}
.monaco-editor .pending-delete {
  border-left: 3px solid #a1260d;
  margin-left: 3px;
}
.monaco-editor .pending-modify {
  border-left: 3px solid #bf8803;
  margin-left: 3px;
}
```

### Step 3 - update when state changes

```ts
// Dart side pushes the file payload via JS bridge:
//   webview.callJavaScriptFunction("onFileUpdate", path, payload)
//
// In webview:
window.onFileUpdate = (path: string, payload: WorkspaceFilePayload) => {
  if (currentPath !== path) return;  // only paint the open file
  const model = editor.getModel()!;
  if (model.getValue() !== payload.content) {
    // Another actor (agent / sibling tab) changed the file - rebase.
    const viewState = editor.saveViewState();
    model.setValue(payload.content);
    if (viewState) editor.restoreViewState(viewState);
  }
  const ranges = parseUnifiedDiff(payload.unified_diff_pending);
  paintDiffGutter(editor, ranges);
  updateHeaderBar(payload);     // insertions/deletions badges
};
```

### Step 4 - side-by-side diff view (modal toggle)

When the user clicks "Show diff":

```ts
// Create a DiffEditor on the fly, baseline on the left, current on right.
async function openSideBySideDiff(path: string) {
  const res = await fetchWorkspaceFile(path, { includeBaseline: true });
  const baseline = res.baseline ?? "";
  const current = res.payload.content;

  const container = document.getElementById("diff-modal-body")!;
  container.innerHTML = "";
  const diffEditor = monaco.editor.createDiffEditor(container, {
    readOnly: false,                    // user can edit the RIGHT side
    renderSideBySide: true,
    automaticLayout: true,
    enableSplitViewResizing: true,
    ignoreTrimWhitespace: false,
  });
  diffEditor.setModel({
    original: monaco.editor.createModel(baseline, res.payload.language),
    modified: monaco.editor.createModel(current, res.payload.language),
  });
  showModal("diff-modal");
}
```

---

## Hunk-level actions - VS Code-style

Each hunk gets an inline widget in the gutter ("approve" / "reject"
icons). When clicked, POST to the hunks endpoint.

### Step 1 - register a CodeLens provider for hunks

```ts
monaco.languages.registerCodeLensProvider("*", {
  provideCodeLenses(model) {
    const ranges = parseUnifiedDiff(currentPendingDiff);
    return {
      lenses: ranges.map((r) => ({
        range: new monaco.Range(r.startLine, 1, r.startLine, 1),
        id: `hunk-${r.hunkIndex}`,
        command: {
          id: "workspace.hunk.menu",
          title: `✓ approve · ✗ reject  (+${r.kind === "insert" ? r.endLine - r.startLine + 1 : 0} −${r.kind === "delete" ? r.endLine - r.startLine + 1 : 0})`,
          arguments: [r.hunkIndex, r.hunkHash, currentPath],
        },
      })),
      dispose: () => {},
    };
  },
  resolveCodeLens: (_, lens) => lens,
});

monaco.editor.registerCommand("workspace.hunk.menu", (_, hunkIndex, hunkHash, path) => {
  // Bubble up to Dart via the JS channel so Flutter shows the action sheet.
  FlutterBridge.postMessage({
    type: "hunk_menu",
    hunkIndex, hunkHash, path,
  });
});
```

### Step 2 - Dart shows a bottom-sheet or context menu

```dart
showModalBottomSheet(
  context: context,
  builder: (_) => Column(children: [
    ListTile(
      leading: Icon(Icons.check, color: Colors.green),
      title: Text('Approve hunk #$hunkIndex'),
      onTap: () => _callWorkspaceApproveHunks(path, [hunkIndex]),
    ),
    ListTile(
      leading: Icon(Icons.close, color: Colors.red),
      title: Text('Reject hunk #$hunkIndex'),
      onTap: () => _callWorkspaceRejectHunks(path, [hunkIndex]),
    ),
  ]),
);
```

**IMPORTANT: always prefer `hunkHash` (12-char) over `hunkIndex` when
posting to the server** - the agent may keep editing the file between
the user seeing the diff and clicking the button. Indices shift; the
hash is stable for the hunk content.

```dart
Future<void> _callWorkspaceApproveHunks(String path, List<String> hunkHashes) async {
  await dio.post(
    '/api/apps/$appId/sessions/$sessionId/workspace/files/approve-hunks',
    data: {'path': path, 'hunks': hunkHashes},
  );
  // The server emits a preview:resource_patched event - your state
  // reducer updates insertions_pending / unified_diff_pending, the
  // webview repaints. Don't mutate client-side state yourself.
}
```

---

## File tree sidebar - badges + status

Render one row per file in `FilesState`. Each row shows:

```
▼ src/
    foo.py        +5 −2  [pending]
    bar.py        ✓ approved
    baz.py        ✓ approved
  test/
    test_foo.py   +12 [new]
    test_bar.py              [!]  lint: 2 errors
```

Data sources:
- `+N −M` from `insertions_pending` / `deletions_pending`
- `pending` / `approved` from `validation`
- `new` when file has no baseline yet (`baseline_lines === 0`)
- `!` when `payload.lint` contains errors (from the lint sub-object)
- `source: "user"` badge if the user manually edited via PUT

Sorting: unapproved files first, alphabetical within.

---

## User actions contract

| Action | Endpoint | Flutter UX |
|---|---|---|
| Open file | GET `/files/{path}?include_baseline=true` | Load into Monaco, baseline cached |
| Manual save (Cmd+S) | PUT `/files/{path}` with `{ content }` | Source = "user", validation = "pending" |
| Approve file | POST `/files/approve` | Button in file tree + editor toolbar |
| Reject file | POST `/files/reject` | Button in file tree + editor toolbar (with confirm modal) |
| Approve hunk | POST `/files/approve-hunks` with `{ hunks: [hash] }` | Gutter CodeLens |
| Reject hunk | POST `/files/reject-hunks` with `{ hunks: [hash] }` | Gutter CodeLens |
| Commit all approved | POST `/commit` with `{ message, paths? }` | Global toolbar button when any file is approved |
| Show history | GET `/files/{path}/history` | Sidebar tab → list revisions, click to diff against HEAD |
| Refresh git status | POST `/git-status` | Pull-to-refresh on the file tree |
| Delete file | DELETE `/files/{path}` | Context menu on file tree row |

### Commit modal

```dart
// Collects all files where validation == "approved"
final approvedPaths = filesState.values
    .where((f) => f.validation == "approved")
    .map((f) => f.path)
    .toList();

showDialog(builder: (_) => AlertDialog(
  title: Text('Commit approved changes'),
  content: Column(children: [
    TextField(
      controller: msgController,
      decoration: InputDecoration(labelText: 'Commit message'),
      maxLines: 3,
    ),
    Text('${approvedPaths.length} files'),
  ]),
  actions: [
    TextButton(onPressed: () => Navigator.pop(context), child: Text('Cancel')),
    ElevatedButton(onPressed: () async {
      await dio.post(
        '/api/apps/$appId/sessions/$sessionId/workspace/commit',
        data: {'message': msgController.text, 'paths': approvedPaths},
      );
      Navigator.pop(context);
    }, child: Text('Commit')),
  ],
));
```

---

## Live-update wiring - the full flow

```dart
// 1. On session open:
final response = await dio.get('/api/apps/$appId/sessions/$sessionId/history');
// Hydrate your FilesState from response.data.preview_snapshot.files
// if present.

// 2. Join Socket.IO:
socket.emit('join_session', {
  'app_id': appId,
  'session_id': sessionId,
  'since_seq': lastSeq,
});

// 3. Route events:
socket.on('event', (envelope) {
  switch (envelope['type']) {
    case 'preview:resource_set':
      _handleResourceSet(envelope['payload']);
      break;
    case 'preview:resource_patched':
      _handleResourcePatched(envelope['payload']);
      break;
    case 'preview:resource_deleted':
      _handleResourceDeleted(envelope['payload']);
      break;
    case 'preview:resource_bulk_set':
      _handleBulkSet(envelope['payload']);
      break;
    case 'preview:snapshot':
      _rehydrate(envelope['payload']);
      break;
  }
});

// 4. When the focused file's payload changes, push to Monaco:
void _handleResourceSet(Map payload) {
  if (payload['channel'] != 'files') return;
  final path = payload['id'] as String;
  final filePayload = payload['payload'] as Map;
  filesState[path] = WorkspaceFilePayload.fromJson(filePayload);
  notifyListeners();           // rebuild file tree badges
  if (openedPath == path) {
    webviewController.callJavaScriptFunction(
      name: 'onFileUpdate',
      args: [path, filePayload],
    );
  }
}
```

---

## Edge cases & gotchas

1. **Diff cap at 16 000 chars**: if `unified_diff_pending.length ===
   16000`, show a "diff truncated - open side-by-side view for full"
   banner.

2. **`validation: "approved"` but `unified_diff_pending` non-empty**:
   shouldn't happen after the recent fix, but if it does (stale
   cache), trust `validation` over the diff text.

3. **Baseline is null**: file is brand-new (never approved). In the
   side-by-side view, show the left pane as empty with "No baseline
   yet". In the gutter, paint every line as insert.

4. **`operation: "delete"`**: the file entry disappears from the
   channel (receives `preview:resource_deleted`). Remove it from the
   tree; close the editor if it was open.

5. **`source: "user"`**: the agent didn't make this edit - the user
   did (via PUT writeback). Render it identically but optionally
   show a "✎ your changes" marker.

6. **Large files (>100k lines)**: Monaco handles them but pagination
   becomes relevant. For the diff view, cap at 1000 visible changed
   lines initially and offer "show all".

7. **Hunk hash drift**: if the agent edits the file while the user is
   viewing the diff, the hashes stay stable for UNCHANGED hunks but
   new hunks appear. Re-fetch the diff on refocus.

8. **Reject when there's no baseline**: the file gets **deleted**
   (server behavior). Show a confirm modal: "This file was never
   approved - rejecting will delete it. Continue?"

9. **Concurrent edits by agent + user**: optimistic UX - if the user
   is mid-keystroke and a `preview:resource_set` arrives with
   different content, DON'T blast the user's work. Show a
   "merge conflict" inline prompt with both versions. Or auto-save
   user's work via PUT writeback before accepting the agent's update.

10. **Socket.IO disconnect mid-session**: on reconnect, re-GET
    `/history` to rehydrate the full file channel (because events
    between disconnect and reconnect may be gone from the ring
    buffer). Dedup by `event_id` as usual.

---

## Minimum testing checklist

Before shipping:

- [ ] Open a file with pending edits → green/red gutter marks appear
- [ ] Make an agent do 3 successive edits on one file → gutter marks
      grow, badge counter grows with each event (+1 +2 +3)
- [ ] Click approve on the file → all gutter marks disappear, badge
      goes to 0
- [ ] Click approve on a single hunk (CodeLens) → ONLY that hunk's
      marks disappear; others stay
- [ ] Reject a file → content reverts to baseline, gutter clears
- [ ] Reject a single hunk → that hunk reverts, others stay pending
- [ ] Side-by-side diff modal opens with correct baseline
- [ ] Emoji / CJK characters render correctly in diff
- [ ] Disconnect WiFi + reconnect → state rehydrates from
      `/history`, no ghost pending marks
- [ ] Kill daemon + restart → pending state survives, file tree
      badges re-populate
- [ ] Agent edits a file you're not viewing → file tree badge
      updates even though editor is closed
- [ ] User edits in editor (PUT writeback) → `source: "user"`
      badge appears, other clients see the change
- [ ] File has a syntax error → `payload.lint` populates →
      file tree shows `!` and the editor shows red squiggles
      (Monaco's native marker support + `payload.lint[].severity`)
- [ ] 10 files in the tree, each with pending edits → each has
      correct independent badges, approving one doesn't affect others
- [ ] Diff longer than 16k chars → "truncated, open full view"
      banner appears, side-by-side modal shows full diff
- [ ] Attempt to reject a never-approved file → confirm dialog
      warns about deletion

---

## What NOT to do

1. **Don't compute the diff client-side.** The server's
   `unified_diff_pending` is THE source of truth. If you diff
   client-side you'll drift.
2. **Don't accumulate pending state client-side.** Every edit event
   carries the full cumulative state. Just replace.
3. **Don't paint `unified_diff` (per-edit) in the gutter.** That's
   the chat tool-chip's concern. The gutter reads
   `unified_diff_pending` only.
4. **Don't send `insertions` / `deletions` to the user - those are
   totals for THIS write.** Show `insertions_pending` /
   `deletions_pending` (since-baseline) in the UI.
5. **Don't approve by index if the file is still being edited
   live.** Use `hunkHash` for deferred user decisions.
6. **Don't keep a baseline cache older than one session.** Refetch
   it when you reopen a file.
7. **Don't assume Monaco's `DiffEditor` can apply approvals** - it
   only displays. All mutations go through the workspace API.

---

## TL;DR

1. Flutter holds the `FilesState`, wired from Socket.IO + HTTP.
2. For each file, `unified_diff_pending` (cumulative, 16k-capped) is
   parsed into line-ranges and painted as Monaco gutter decorations.
3. CodeLens per hunk offers approve/reject; the Dart side owns the
   action sheet + HTTP POST.
4. Side-by-side view uses Monaco's `DiffEditor` with `baseline` on
   the left and `content` on the right.
5. All state is server-authoritative - you never mutate your local
   `FilesState` directly; you just react to the events the server
   emits after every write / approve / reject.
