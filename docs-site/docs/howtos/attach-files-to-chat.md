---
id: attach-files-to-chat
title: Attach files to a chat session
sidebar_label: Attach files to chat
---

The chat composer ships with a paperclip menu that lets the
user drag-drop or pick files for the current message. The
daemon parses each file, indexes it into a per-session
knowledge base, and surfaces the extracted text to the agent
via one of two strategies (`direct` or `tool`). This page
covers the end-to-end pipeline and how to pick a strategy.

## End-user flow

The composer accepts files in three ways:

- **Paperclip button**: opens the native file picker, scoped
  to the categories declared in
  [`app.attachments`](../language/02-app-config.md#appattachments---what-the-composers--menu-accepts).
- **Drag-and-drop**: drop a file or a batch anywhere over the
  message area while the composer is focused.
- **Paste from clipboard**: a screenshot pasted from the
  system clipboard counts as an `image` attachment.

Caps enforced by the composer (and mirrored server-side by
`body.files[:10]`):

| Cap | Value |
|-----|-------|
| Per-file size | 10 MB |
| Cumulative per message | 25 MB |
| File count | 10 files |

Files that fail any check are dropped with an inline toast in
the composer. Nothing reaches the daemon until the user hits
send.

## How the daemon stores and indexes files

On `POST /api/apps/{app_id}/sessions/{sid}/messages` with a
non-empty `files: [...]` array, the daemon:

1. **Persists each upload** to disk under
   `~/.digitorn/files/{session_id}/{file_id}.{ext}`. The
   on-disk filename uses the generated `file_id`, the
   original filename is kept separately in the
   `FileRef.original_name` field

2. **Sniffs the real format** by magic bytes
   (`sniff_format`). Extension is
   only a fallback, so a file uploaded as `report` (no
   suffix) with `Content-Type: application/pdf` still routes
   to the PDF ingestor.
3. **Indexes the file** into a per-session RAG knowledge base
   named `chat-session-{session_id}`
   The ingestor
   is picked by sniffed format
   Available:
   `PDFIngestor`, `DOCXIngestor`, `PPTXIngestor`,
   `ODTIngestor`, `ODSIngestor`, `SpreadsheetIngestor` (XLSX),
   `RTFIngestor`, `CSVIngestor`, `JSONIngestor`,
   `JSONLIngestor`, `MarkdownIngestor`, `HTMLIngestor`,
   `CodeIngestor`, `PlainTextIngestor`. The small-doc
   extracted text is cached on `FileRef.extracted_text` for
   the inject path to skip a second parse.
4. **Updates the FileRef** with the outcome:
   `index_status` (`pending` / `indexed` / `failed` /
   `skipped` / `empty`), `index_chunks`, and `index_error`
   when the ingest crashed.

The next turn picks up the manifest of indexed files and
applies the strategy declared by
[`app.attachments_mode`](../language/02-app-config.md#appattachments_mode---how-the-agent-sees-attached-files).

## The two strategies

### Mode `direct` - prepend full text (default)

Best for chat apps without a workspace. The daemon prepends
a `[Attached files context]` block to the user message
containing the full extracted text of every file. The agent
sees the content immediately, no tool call needed.

```yaml
app:
  app_id: simple-chat
  name: Simple Chat
  attachments: [document]
  attachments_mode: direct

runtime:
  mode: conversation
  workdir_mode: none

agents:
  - id: main
    role: assistant
    brain:
      provider: ollama
      model: qwen25-7b-gpu:latest
      backend: openai_compat
      config:
        base_url: http://localhost:11434/v1
        api_key: ollama
    system_prompt: |
      You answer questions about the documents the user
      attaches. Cite filenames in brackets, e.g. [report.pdf].

tools:
  modules:
    rag: {}                # daemon-internal, indexes uploads
  capabilities:
    default_policy: auto
```

`rag` is loaded but intentionally absent from
`capabilities.grant`: the agent does NOT see RAG tools. The
daemon uses the instance internally to ingest the user's
files and to inject the relevant context before each turn.

If the total extracted text exceeds 80 KB
(`_dispatch._FULL_INJECT_THRESHOLD`), the daemon falls back
to top-20 RAG retrieval against the per-session KB,
truncating each excerpt at 2000 chars. The agent still sees
the same `[Attached files context]` block, just with
excerpts instead of the full document.

### Mode `tool` - mirror into the workspace

For big-corpus apps. Files are written into the workspace
under `attachments/<sanitised-name>` and the agent is told
to call `WsRead` / `WsGlob` / `WsGrep` to inspect them. No
content is prepended to the user message: the prompt only
carries a manifest with per-file size, line count, and chunk
count so the agent can pick sensible `offset` / `limit`
values for paginated reads.

```yaml
app:
  app_id: doc-analyst
  name: Doc Analyst
  attachments: [document]
  attachments_mode: tool

runtime:
  mode: conversation
  workdir_mode: none

agents:
  - id: main
    role: assistant
    brain:
      provider: ollama
      model: qwen25-7b-gpu:latest
      backend: openai_compat
      config:
        base_url: http://localhost:11434/v1
        api_key: ollama
    system_prompt: |
      You analyse attached documents. Always call WsRead
      before answering, never guess. Cite as [filename] or
      [filename · lines A-B] when you read with offsets.

tools:
  modules:
    preview: {}            # hard dep of workspace
    workspace:
      config:
        render_mode: markdown
        agent_root: "attachments"   # lock agent to attachments/
        auto_approve: true
        lint: false
    rag: {}
  capabilities:
    default_policy: auto
    grant:
      - module: workspace
        actions: [read, glob, grep]
```

`agent_root: "attachments"` is the safety lock that prevents
the agent from reading app-private workspace files via `..`
or absolute paths. See
[workspace module reference](../reference/modules/workspace.md#agent_root---scope-lock-for-attachments-mode).

### Combining both: `direct` + workspace loaded

The `digitorn-chat` production setup uses `direct` mode but
ALSO loads the workspace module — the daemon mirrors
attachments under `attachments/` so the agent can re-read
specific sections via `WsRead` when it needs precise quotes,
while still having the full text in the user message for
immediate Q&A.

```yaml
app:
  app_id: chat
  name: Chat
  attachments: [image, document]
  attachments_mode: direct

runtime:
  mode: conversation
  workdir_mode: none

agents:
  - id: main
    role: assistant
    brain:
      provider: ollama
      model: qwen25-7b-gpu:latest
      backend: openai_compat
      config:
        base_url: http://localhost:11434/v1
        api_key: ollama
    system_prompt: |
      You help the user reason over their attached files.
      Quote the content directly when relevant, cite the
      source in brackets, and use WsRead when you need a
      specific section by line range.

tools:
  modules:
    memory:
      config:
        auto_remember: true
        working_memory: true
    preview: {}
    workspace:
      config:
        render_mode: markdown
        agent_root: "attachments"
        auto_approve: true
        lint: false
    rag: {}
  capabilities:
    default_policy: auto
    grant:
      - module: memory
        actions: [set_goal, remember]
      - module: workspace
        actions: [read, glob, grep]
```

`rag` and `preview` are loaded but never granted: they
support the attachments pipeline internally (RAG ingests the
upload, preview owns the workspace channel) and are not
agent-callable. `memory` is granted because the agent should
be able to record facts the user mentions during the chat.

This is exactly the shape `digitorn-chat` ships with.

## The citation format the LLM is taught to emit

Every context block ends with a "Citation rules" line that
tells the model exactly how to cite. The format is:

| Citation | When it appears |
|----------|-----------------|
| `[filename]` | Direct mode (the whole file is in context). |
| `[filename · page N]` | RAG fallback path when a PDF / DOCX / PPTX excerpt has page metadata. |
| `[filename · section X]` | RAG fallback when the excerpt carries a section anchor (Markdown headers, ODT sections). |
| `[filename · lines A-B]` | Tool mode, when the agent reads a slice with `WsRead(offset, limit)`. |

The model is told explicitly never to invent citations and
to fall back to "the documents don't cover this" when an
excerpt does not answer the question. The text of these
instructions is generated by
,
`_format_rag_context_block`, and `_format_tool_mode_block`.

## Troubleshooting

### "The agent doesn't see my file"

Symptoms: the user uploaded a file, the chat went through,
but the agent answers like nothing was attached.

Check, in order:

1. **Did the file index?** Look at the `index_status` on the
   `FileRef`. A future endpoint will expose this via
   `GET /api/apps/{id}/sessions/{sid}` under
   `attachments[]`. Until then, grep the daemon log for the
   session ID:

   ```bash
   tail -F ~/.digitorn/logs/daemon.log \
     | grep -E "rag_ingest|file_store|sid=<sid>"
   ```

   You should see `rag_ingest_ok` or `rag_ingest_failed`
   shortly after the upload. `failed` means the ingestor
   crashed: the message contains the parser exception (a
   broken PDF, an unsupported DOCX feature, etc.).

2. **Is the right module loaded?** `direct` mode needs only
   `rag` in `tools.modules`. `tool` mode also needs
   `workspace` and `preview`. A missing `workspace` silently
   downgrades `tool` to `direct`.

3. **Is the agent allowed to read attachments?** In tool or
   hybrid mode the agent must have `workspace.read` granted
   (and ideally `glob` / `grep`):

   ```yaml
   capabilities:
     grant:
       - module: workspace
         actions: [read, glob, grep]
   ```

   Without the grant, `WsRead` returns a permission error
   and the agent gives up.

4. **Is `agent_root` set correctly?** When `agent_root` is
   set to a value that does NOT match where attachments
   land, the agent sees an empty `attachments/` directory.
   The canonical value is `agent_root: "attachments"`. The
   workspace mirror always writes to that exact path
   regardless of other config.

5. **Did the file exceed the cap?** Per-file 10 MB,
   cumulative 25 MB per message. Files above are rejected
   by the composer before upload.

### "The agent cites a path I don't recognise"

If the citation looks like
`[~/.digitorn/files/<sid>/<id>.pdf]` instead of the original
filename, the RAG retrieval path could not resolve
`meta.original_name` and fell back to `meta.doc_id`. The fix
is in the ingestor: every ingestor in
should set
`metadata["original_name"]` on every chunk. Newer
ingestors do this consistently, older ones may not.

### "The KB is not getting queried"

The retrieval path queries `chat-session-<sid>`, hard-coded
via.  If you have
forked the dispatch path or built a custom ingestor, route
both through this helper so the KB name stays in sync.

## Going further

- The schema fields:
  [`app.attachments` + `app.attachments_mode`](../language/02-app-config.md#appattachments---what-the-composers--menu-accepts)
- The workspace lock:
  [`workspace.agent_root`](../reference/modules/workspace.md#agent_root---scope-lock-for-attachments-mode)
- The RAG module that owns per-session indexing:
  [rag module](../reference/modules/rag.md)
- The preview module that owns the workspace channel:
  [preview module](../reference/modules/preview.md)
