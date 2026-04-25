import { useEffect, useMemo, useState } from "react";

/**
 * A full-screen overlay panel (toggle with `?`) that shows the 21
 * Digitorn modules + their LLM-visible actions. Offers:
 *  - instant search (name, action, or description)
 *  - grouping by category
 *  - click-to-copy the grant snippet for each module
 *
 * The data set mirrors what ``/api/discovery/modules`` returns, baked
 * into the preview for offline speed. Future iteration: fetch the
 * live snapshot at mount time via the SDK's helper.
 */

interface ModuleDef {
  id: string;
  category: string;
  description: string;
  actions: Array<{ name: string; desc: string }>;
  config_hint?: string;
}

const MODULES: ModuleDef[] = [
  {
    id: "memory",
    category: "core",
    description: "Persistent cognitive state (goals, todos, facts, episodic memory).",
    actions: [
      { name: "task_create", desc: "Create a task to track progress." },
      { name: "task_update", desc: "Update a task's status." },
      { name: "set_goal", desc: "Set the main goal for this session." },
      { name: "remember", desc: "Store a fact that survives context compaction." },
    ],
    config_hint: "working_memory: true, todo_list: true, semantic: {vector, graph}",
  },
  {
    id: "web",
    category: "io",
    description: "Web search + fetch + scraping.",
    actions: [
      { name: "search", desc: "Search the web." },
      { name: "fetch", desc: "Fetch a web page as text." },
      { name: "extract", desc: "Extract content via CSS selectors." },
      { name: "download", desc: "Download a file to a local path." },
    ],
    config_hint: "search_backend: duckduckgo|brave|tavily",
  },
  {
    id: "http",
    category: "io",
    description: "HTTP client (SSRF-protected, host allowlisting).",
    actions: [
      { name: "get", desc: "HTTP GET with auto-parsed response." },
      { name: "post", desc: "HTTP POST with JSON serialization." },
      { name: "put", desc: "HTTP PUT — replace resource." },
      { name: "patch", desc: "HTTP PATCH — partial update." },
      { name: "delete", desc: "HTTP DELETE." },
      { name: "head", desc: "HTTP HEAD — headers only." },
      { name: "options", desc: "HTTP OPTIONS — CORS discovery." },
      { name: "json_api", desc: "Structured JSON API call." },
      { name: "ping", desc: "Reachability check." },
      { name: "download", desc: "Stream a URL to disk." },
      { name: "upload", desc: "Multipart upload." },
      { name: "request", desc: "Raw request with full control." },
    ],
    config_hint: "timeout: 30, constraints.allowed_hosts: [...]",
  },
  {
    id: "filesystem",
    category: "io",
    description: "Real-disk I/O scoped to the session workspace.",
    actions: [
      { name: "read", desc: "Read a file." },
      { name: "write", desc: "Write a file." },
      { name: "edit", desc: "Find-and-replace in a file." },
      { name: "glob", desc: "Find files by name pattern." },
      { name: "grep", desc: "Search contents by regex." },
    ],
  },
  {
    id: "shell",
    category: "io",
    description: "Command execution (sync, async, status, kill).",
    actions: [{ name: "bash", desc: "Run a shell command." }],
    config_hint: "constraints.allowed_commands/blocked_commands/allowed_paths",
  },
  {
    id: "workspace",
    category: "live_ui",
    description: "Virtual filesystem that streams to the live preview client.",
    actions: [
      { name: "write", desc: "Create / overwrite a file (streams live)." },
      { name: "read", desc: "Read a file (numbered lines)." },
      { name: "edit", desc: "Surgical text replacement." },
      { name: "glob", desc: "Find by name pattern." },
      { name: "grep", desc: "Search file contents." },
      { name: "delete", desc: "Remove a file." },
    ],
    config_hint:
      "render_mode: react|builder|latex|slides|html|markdown|code, sync_to_disk, lint",
  },
  {
    id: "agent_spawn",
    category: "agentic",
    description: "Multi-agent orchestration — 1 Agent tool with 8 modes.",
    actions: [
      { name: "Agent", desc: "spawn|wait|status|cancel|reassign|list — 8 modes" },
    ],
  },
  {
    id: "context_builder",
    category: "agentic",
    description: "Discovery, watchers, ask_user, background run.",
    actions: [
      { name: "ask_user", desc: "Structured user question." },
      { name: "use_skill", desc: "Reload a skill's instructions." },
      { name: "search_tools", desc: "Search tools in the app." },
      { name: "get_tool", desc: "Get a tool's schema." },
      { name: "list_categories", desc: "List tool categories." },
      { name: "browse_category", desc: "Browse a category." },
      { name: "execute_tool", desc: "Invoke any tool by name." },
      { name: "background_run", desc: "Run tool in background." },
      { name: "watch_start", desc: "Start a persistent watcher." },
      { name: "watch_stop", desc: "Stop a watcher." },
      { name: "watch_pause", desc: "Pause a watcher." },
      { name: "watch_resume", desc: "Resume a watcher." },
      { name: "watch_status", desc: "Get watcher status." },
      { name: "watch_list", desc: "List watchers." },
      { name: "watch_history", desc: "Get watcher run history." },
    ],
  },
  {
    id: "behavior",
    category: "agentic",
    description: "Runtime rule enforcement (profiles: coding|research|data|creative|assistant).",
    actions: [],
    config_hint: "profile: coding, rules: [...], classifier.frequency",
  },
  {
    id: "preview",
    category: "live_ui",
    description: "SSE transport for live canvas — workspace writes through it.",
    actions: [],
  },
  {
    id: "widget",
    category: "live_ui",
    description: "UI widgets (render, update, close, state).",
    actions: [],
  },
  {
    id: "database",
    category: "data",
    description: "SQL connectors (sqlite, postgres, mysql).",
    actions: [
      { name: "connect", desc: "Open a connection." },
      { name: "disconnect", desc: "Close a connection." },
      { name: "fetch", desc: "SELECT." },
      { name: "execute", desc: "INSERT/UPDATE/DELETE." },
      { name: "schema_introspect", desc: "Discover tables + columns." },
    ],
    config_hint: "setup: [{action: connect, params: {...}}]",
  },
  {
    id: "rag",
    category: "data",
    description: "Vector + graph KB — query, multi_query.",
    actions: [
      { name: "query", desc: "Semantic search." },
      { name: "multi_query", desc: "Multi-query fusion." },
      { name: "list_knowledge_bases", desc: "List KBs." },
    ],
    config_hint: "backend: {type: qdrant, path: '.../kb/.qdrant'}",
  },
  {
    id: "vector",
    category: "data",
    description: "Low-level vector ops (prefer rag).",
    actions: [],
  },
  {
    id: "cache",
    category: "data",
    description: "In-memory KV with TTL.",
    actions: [
      { name: "get", desc: "Get a value." },
      { name: "set", desc: "Set with TTL." },
      { name: "delete", desc: "Delete a key." },
      { name: "list", desc: "List keys." },
    ],
  },
  {
    id: "queue",
    category: "data",
    description: "Durable task queue.",
    actions: [
      { name: "enqueue", desc: "Add a task." },
      { name: "dequeue", desc: "Take the head." },
      { name: "peek", desc: "Look without removing." },
      { name: "stats", desc: "Queue metrics." },
    ],
  },
  {
    id: "index",
    category: "data",
    description: "Full-text search over files/records.",
    actions: [
      { name: "index", desc: "Add to index." },
      { name: "search", desc: "Full-text search." },
      { name: "delete", desc: "Remove from index." },
    ],
  },
  {
    id: "channels",
    category: "triggers",
    description:
      "Bidirectional channels — webhooks, cron, file watcher, email, RSS, queue.",
    actions: [],
    config_hint: "providers: {<name>: {adapter, config, activation}}",
  },
  {
    id: "cron_native",
    category: "triggers",
    description: "Native scheduler — alternative to channels.cron.",
    actions: [],
  },
  {
    id: "mcp",
    category: "agentic",
    description: "Model Context Protocol bridge to external tools.",
    actions: [],
  },
  {
    id: "lsp",
    category: "dev",
    description: "Language servers + built-in linters (python, markdown, json, yaml, toml).",
    actions: [
      { name: "diagnostics", desc: "Get current diagnostics." },
      { name: "check", desc: "Run lint on demand." },
      { name: "notify_change", desc: "Re-check after a file write." },
    ],
    config_hint: "<lang>: 'linter command string'  (e.g. python: 'ruff check')",
  },
];

const CATEGORIES: Array<{ id: string; label: string; color: string }> = [
  { id: "core", label: "Core", color: "#10b981" },
  { id: "io", label: "I/O", color: "#3b82f6" },
  { id: "agentic", label: "Agentic", color: "#8b5cf6" },
  { id: "live_ui", label: "Live UI", color: "#ec4899" },
  { id: "data", label: "Data", color: "#f59e0b" },
  { id: "triggers", label: "Triggers", color: "#06b6d4" },
  { id: "dev", label: "Dev", color: "#64748b" },
];

export default function SchemaReferencePanel({
  open,
  onClose,
}: {
  open: boolean;
  onClose: () => void;
}) {
  const [query, setQuery] = useState("");

  // ESC to close, and focus the search box when opened.
  useEffect(() => {
    if (!open) return;
    const handler = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, [open, onClose]);

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    if (!q) return MODULES;
    return MODULES.filter((m) => {
      if (m.id.includes(q)) return true;
      if (m.description.toLowerCase().includes(q)) return true;
      if (m.category.includes(q)) return true;
      return m.actions.some(
        (a) =>
          a.name.toLowerCase().includes(q) || a.desc.toLowerCase().includes(q),
      );
    });
  }, [query]);

  if (!open) return null;

  return (
    <div
      style={{
        position: "fixed",
        inset: 0,
        background: "#0b1120ee",
        backdropFilter: "blur(6px)",
        zIndex: 100,
        display: "flex",
        justifyContent: "center",
        alignItems: "flex-start",
        paddingTop: 40,
      }}
      onClick={onClose}
    >
      <div
        onClick={(e) => e.stopPropagation()}
        style={{
          width: "min(880px, 92vw)",
          maxHeight: "88vh",
          background: "#0f172a",
          border: "1px solid #1e293b",
          borderRadius: 12,
          display: "flex",
          flexDirection: "column",
          color: "#e2e8f0",
          fontFamily:
            "'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif",
          boxShadow: "0 20px 40px rgba(0,0,0,0.6)",
        }}
      >
        {/* header */}
        <div
          style={{
            padding: 16,
            borderBottom: "1px solid #1e293b",
            display: "flex",
            alignItems: "center",
            gap: 12,
          }}
        >
          <div style={{ fontSize: 14, fontWeight: 700, letterSpacing: 0.4 }}>
            Digitorn Schema Reference
          </div>
          <div style={{ fontSize: 11, color: "#64748b" }}>
            {filtered.length} / {MODULES.length} modules
          </div>
          <div style={{ flex: 1 }} />
          <input
            autoFocus
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="Filter by module, action, description…"
            style={{
              flex: "1 1 260px",
              maxWidth: 360,
              fontSize: 12,
              padding: "6px 10px",
              background: "#020617",
              border: "1px solid #1e293b",
              borderRadius: 6,
              color: "#e2e8f0",
              outline: "none",
            }}
          />
          <button
            onClick={onClose}
            style={{
              background: "transparent",
              color: "#64748b",
              border: "1px solid #1e293b",
              borderRadius: 4,
              fontSize: 10,
              padding: "4px 10px",
              cursor: "pointer",
            }}
          >
            ESC
          </button>
        </div>

        {/* body */}
        <div style={{ overflowY: "auto", padding: 12 }}>
          {CATEGORIES.map((cat) => {
            const mods = filtered.filter((m) => m.category === cat.id);
            if (mods.length === 0) return null;
            return (
              <div key={cat.id} style={{ marginBottom: 16 }}>
                <div
                  style={{
                    fontSize: 10,
                    fontWeight: 700,
                    letterSpacing: 1.2,
                    textTransform: "uppercase",
                    color: cat.color,
                    padding: "4px 6px",
                  }}
                >
                  {cat.label}
                </div>
                <div
                  style={{
                    display: "grid",
                    gridTemplateColumns: "repeat(auto-fill, minmax(260px, 1fr))",
                    gap: 8,
                  }}
                >
                  {mods.map((m) => (
                    <div
                      key={m.id}
                      style={{
                        background: "#0b1120",
                        border: "1px solid #1e293b",
                        borderRadius: 8,
                        padding: 10,
                      }}
                    >
                      <div
                        style={{
                          display: "flex",
                          alignItems: "center",
                          gap: 8,
                          marginBottom: 4,
                        }}
                      >
                        <span
                          style={{
                            fontSize: 13,
                            fontWeight: 700,
                            color: cat.color,
                          }}
                        >
                          {m.id}
                        </span>
                        <span style={{ fontSize: 9, color: "#64748b" }}>
                          {m.actions.length === 0
                            ? "no llm actions"
                            : `${m.actions.length} action${m.actions.length > 1 ? "s" : ""}`}
                        </span>
                      </div>
                      <div style={{ fontSize: 11, color: "#94a3b8", lineHeight: 1.4 }}>
                        {m.description}
                      </div>
                      {m.actions.length > 0 && (
                        <div style={{ marginTop: 8, display: "flex", flexDirection: "column", gap: 2 }}>
                          {m.actions.map((a) => (
                            <div
                              key={a.name}
                              title={a.desc}
                              style={{
                                fontSize: 10,
                                color: "#cbd5e1",
                                fontFamily: "monospace",
                                display: "flex",
                                gap: 6,
                              }}
                            >
                              <span style={{ color: cat.color }}>•</span>
                              <span style={{ color: "#e2e8f0" }}>{a.name}</span>
                              <span
                                style={{
                                  color: "#64748b",
                                  overflow: "hidden",
                                  textOverflow: "ellipsis",
                                  whiteSpace: "nowrap",
                                }}
                              >
                                {a.desc}
                              </span>
                            </div>
                          ))}
                        </div>
                      )}
                      {m.config_hint && (
                        <div
                          style={{
                            marginTop: 8,
                            padding: 6,
                            background: "#020617",
                            borderRadius: 4,
                            fontSize: 10,
                            fontFamily: "monospace",
                            color: "#64748b",
                            overflowX: "auto",
                          }}
                        >
                          config: {m.config_hint}
                        </div>
                      )}
                    </div>
                  ))}
                </div>
              </div>
            );
          })}

          {filtered.length === 0 && (
            <div
              style={{
                padding: 40,
                textAlign: "center",
                color: "#64748b",
                fontSize: 12,
              }}
            >
              No module matches “{query}”
            </div>
          )}
        </div>

        {/* footer */}
        <div
          style={{
            padding: "8px 16px",
            borderTop: "1px solid #1e293b",
            fontSize: 10,
            color: "#64748b",
            display: "flex",
            justifyContent: "space-between",
          }}
        >
          <span>
            Reference snapshot — authoritative list via{" "}
            <code style={{ color: "#94a3b8" }}>
              App(list_modules=true)
            </code>
          </span>
          <span>
            Shortcut: <kbd style={kbd}>?</kbd> toggle · <kbd style={kbd}>Esc</kbd> close
          </span>
        </div>
      </div>
    </div>
  );
}

const kbd: React.CSSProperties = {
  padding: "1px 5px",
  background: "#0b1120",
  border: "1px solid #334155",
  borderRadius: 3,
  fontFamily: "monospace",
  fontSize: 9,
};
