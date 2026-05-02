/**
 * NewAppWizard - modal that creates a fresh Digitorn app YAML.
 *
 * Three quick steps in one form:
 *   1. Identity   : app_id (slug) + display name + description
 *   2. Mode       : conversation | one_shot | background
 *   3. Starter    : blank | single-agent chat | multi-agent flow
 *
 * On submit, emits the YAML through ``onCreate`` so the parent
 * (App.tsx) can dump it into the canvas. The wizard never talks to
 * the daemon directly - it only produces a YAML string.
 */
import { useState } from "react";
import { X, Sparkles, Check, AlertCircle } from "lucide-react";
import yaml from "js-yaml";

interface Props {
  open: boolean;
  onClose: () => void;
  onCreate: (yamlText: string) => void;
}

type Mode = "conversation" | "one_shot" | "background";
type Starter = "blank" | "chat" | "multi_agent_flow";

const STARTERS: Array<{ id: Starter; label: string; description: string }> = [
  {
    id: "blank",
    label: "Blank",
    description: "Just the app block + one agent. You'll fill in the rest.",
  },
  {
    id: "chat",
    label: "Single-agent chat",
    description: "One agent answers user messages. Add tools as you go.",
  },
  {
    id: "multi_agent_flow",
    label: "Multi-agent with flow",
    description: "Coordinator + 2 specialists wired through a flow graph.",
  },
];

const SLUG_RE = /^[a-z][a-z0-9-]{1,62}[a-z0-9]$/;

function buildYaml(
  appId: string,
  name: string,
  description: string,
  mode: Mode,
  starter: Starter,
): string {
  const app: Record<string, unknown> = {
    app: { app_id: appId, name, description: description || undefined, version: "0.1.0" },
  };

  // v2 canonical shape: 7 nested top-level blocks. modules go under
  // tools.modules, flow under runtime.flow.
  if (starter === "blank") {
    app.runtime = { mode };
    app.agents = [
      {
        id: "main",
        role: "coordinator",
        brain: { provider: "anthropic", model: "claude-haiku-4-5", credential: "anthropic_main" },
        system_prompt: "You are a helpful agent.",
      },
    ];
  } else if (starter === "chat") {
    app.runtime = { mode, entry_agent: "main" };
    app.tools = { modules: { web: {} } };
    app.agents = [
      {
        id: "main",
        role: "coordinator",
        brain: { provider: "anthropic", model: "claude-haiku-4-5", credential: "anthropic_main" },
        modules: [{ web: ["search", "fetch"] }],
        system_prompt: "Answer concisely. Cite sources when you search the web.",
      },
    ];
  } else {
    app.tools = { modules: { web: {}, agent_spawn: {} } };
    app.agents = [
      {
        id: "lead",
        role: "coordinator",
        brain: { provider: "anthropic", model: "claude-sonnet-4-6", credential: "anthropic_main" },
        modules: [{ agent_spawn: ["Agent"] }],
        system_prompt: "Dispatch the right specialist for the job.",
      },
      {
        id: "researcher",
        role: "specialist",
        brain: { provider: "anthropic", model: "claude-haiku-4-5", credential: "anthropic_main" },
        modules: [{ web: ["search", "fetch"] }],
        system_prompt: "Find facts, return citations.",
      },
      {
        id: "writer",
        role: "specialist",
        brain: { provider: "anthropic", model: "claude-sonnet-4-6", credential: "anthropic_main" },
        system_prompt: "Compose the final answer.",
      },
    ];
    app.runtime = { mode, entry_agent: "lead", max_turns: 30 };
    // v2: ``flow`` is its own top-level block (8th canonical block).
    app.flow = {
      id: "main",
      entry: "triage",
      max_iterations: 25,
      nodes: [
        {
          id: "triage",
          type: "agent",
          agent: "lead",
          routes: [
            { when: "output.kind == 'research'", to: "research_step" },
            { when: "default", to: "write_step" },
          ],
        },
        {
          id: "research_step",
          type: "agent",
          agent: "researcher",
          routes: [{ to: "write_step" }],
        },
        {
          id: "write_step",
          type: "agent",
          agent: "writer",
          routes: [{ to: "end" }],
        },
      ],
    };
  }

  return yaml.dump(app, { lineWidth: 100, noRefs: true });
}

export default function NewAppWizard({ open, onClose, onCreate }: Props) {
  const [appId, setAppId] = useState("my-app");
  const [name, setName] = useState("My App");
  const [description, setDescription] = useState("");
  const [mode, setMode] = useState<Mode>("conversation");
  const [starter, setStarter] = useState<Starter>("chat");

  if (!open) return null;

  const slugError = SLUG_RE.test(appId)
    ? null
    : "lowercase letters / digits / dashes, 3-64 chars (no spaces).";
  const canSubmit = !slugError && name.trim().length > 0;

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/70 backdrop-blur-sm"
      onClick={onClose}
    >
      <div
        className="bg-surface-1 border border-border-subtle rounded-xl shadow-2xl w-[640px] max-w-[92vw] max-h-[88vh] overflow-y-auto"
        onClick={(e) => e.stopPropagation()}
      >
        {/* Header */}
        <div className="flex items-center justify-between p-4 border-b border-border-subtle">
          <div className="flex items-center gap-2">
            <Sparkles className="w-4 h-4 text-accent" />
            <span className="text-sm font-semibold text-ink">New Digitorn app</span>
          </div>
          <button
            onClick={onClose}
            className="p-1 rounded hover:bg-surface-2 text-ink-muted hover:text-ink"
            aria-label="Close"
          >
            <X className="w-4 h-4" />
          </button>
        </div>

        {/* Body */}
        <div className="p-5 space-y-5">
          {/* Identity */}
          <div className="space-y-3">
            <div className="text-[10px] uppercase tracking-wider text-ink-dim">Identity</div>
            <div>
              <label className="block text-[11px] text-ink-muted mb-1">App id</label>
              <input
                type="text"
                value={appId}
                onChange={(e) => setAppId(e.target.value.toLowerCase())}
                className="w-full px-3 py-2 bg-surface-2 border border-border-subtle rounded text-[13px] font-mono text-ink focus:border-accent focus:outline-none"
                placeholder="my-app"
              />
              {slugError && (
                <div className="flex items-center gap-1 mt-1 text-[11px] text-status-error">
                  <AlertCircle className="w-3 h-3" /> {slugError}
                </div>
              )}
            </div>
            <div>
              <label className="block text-[11px] text-ink-muted mb-1">Display name</label>
              <input
                type="text"
                value={name}
                onChange={(e) => setName(e.target.value)}
                className="w-full px-3 py-2 bg-surface-2 border border-border-subtle rounded text-[13px] text-ink focus:border-accent focus:outline-none"
                placeholder="My App"
              />
            </div>
            <div>
              <label className="block text-[11px] text-ink-muted mb-1">Description (optional)</label>
              <textarea
                value={description}
                onChange={(e) => setDescription(e.target.value)}
                rows={2}
                className="w-full px-3 py-2 bg-surface-2 border border-border-subtle rounded text-[12px] text-ink focus:border-accent focus:outline-none resize-none"
                placeholder="What this app does"
              />
            </div>
          </div>

          {/* Mode */}
          <div className="space-y-2">
            <div className="text-[10px] uppercase tracking-wider text-ink-dim">Execution mode</div>
            <div className="grid grid-cols-3 gap-2">
              {(["conversation", "one_shot", "background"] as Mode[]).map((m) => (
                <button
                  key={m}
                  onClick={() => setMode(m)}
                  className={
                    "p-2.5 rounded border text-[12px] font-medium transition-all " +
                    (mode === m
                      ? "bg-accent/10 border-accent text-accent"
                      : "bg-surface-2 border-border-subtle text-ink-muted hover:border-ink-dim")
                  }
                >
                  {m}
                </button>
              ))}
            </div>
            <div className="text-[11px] text-ink-dim leading-relaxed">
              <strong className="text-ink-muted">conversation</strong>: multi-turn chat (default).{" "}
              <strong className="text-ink-muted">one_shot</strong>: single input → single output.{" "}
              <strong className="text-ink-muted">background</strong>: trigger-driven (cron / webhook).
            </div>
          </div>

          {/* Starter */}
          <div className="space-y-2">
            <div className="text-[10px] uppercase tracking-wider text-ink-dim">Starter template</div>
            <div className="space-y-2">
              {STARTERS.map((s) => (
                <button
                  key={s.id}
                  onClick={() => setStarter(s.id)}
                  className={
                    "w-full p-3 rounded border text-left transition-all flex items-start gap-2.5 " +
                    (starter === s.id
                      ? "bg-accent/10 border-accent"
                      : "bg-surface-2 border-border-subtle hover:border-ink-dim")
                  }
                >
                  <div
                    className={
                      "flex-shrink-0 w-4 h-4 rounded-full border flex items-center justify-center mt-0.5 " +
                      (starter === s.id ? "bg-accent border-accent" : "border-ink-dim")
                    }
                  >
                    {starter === s.id && <Check className="w-2.5 h-2.5 text-surface-1" />}
                  </div>
                  <div className="flex-1">
                    <div className={"text-[12px] font-semibold " + (starter === s.id ? "text-accent" : "text-ink")}>
                      {s.label}
                    </div>
                    <div className="text-[11px] text-ink-muted mt-0.5">{s.description}</div>
                  </div>
                </button>
              ))}
            </div>
          </div>
        </div>

        {/* Footer */}
        <div className="flex items-center justify-end gap-2 p-4 border-t border-border-subtle bg-surface-2/50">
          <button
            onClick={onClose}
            className="px-3 py-1.5 text-[12px] text-ink-muted hover:text-ink rounded hover:bg-surface-2"
          >
            Cancel
          </button>
          <button
            disabled={!canSubmit}
            onClick={() => {
              const text = buildYaml(appId.trim(), name.trim(), description.trim(), mode, starter);
              onCreate(text);
              onClose();
            }}
            className="px-4 py-1.5 text-[12px] font-semibold rounded bg-accent text-surface-1 hover:bg-accent/90 disabled:opacity-40 disabled:cursor-not-allowed"
          >
            Create app
          </button>
        </div>
      </div>
    </div>
  );
}
