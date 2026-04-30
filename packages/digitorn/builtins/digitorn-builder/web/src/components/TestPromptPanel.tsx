/**
 * Live-preview test prompt panel.
 *
 * Lets the user dispatch a test message against the current YAML.
 * In dev mode (`session_id === "_dev_"`) there is no daemon available
 * so we degrade gracefully — show a CLI snippet the user can run
 * locally instead, and stream the daemon's response when one IS
 * available.
 */
import { useState } from "react";
import { Play, X, Copy, Check, Loader2, Terminal } from "lucide-react";
import clsx from "clsx";

interface Props {
  open: boolean;
  onClose: () => void;
  appName: string;
  yamlContent: string | null;
  /** Provided by the parent when a real daemon session is active. */
  sessionId?: string | null;
}

interface TurnResult {
  status: "idle" | "running" | "ok" | "error";
  text: string;
  error?: string;
  durationMs?: number;
}

export default function TestPromptPanel({ open, onClose, appName, yamlContent, sessionId }: Props) {
  const [prompt, setPrompt] = useState("Test the agent: introduce yourself and list your capabilities.");
  const [result, setResult] = useState<TurnResult>({ status: "idle", text: "" });
  const [copied, setCopied] = useState(false);

  const isDev = !sessionId || sessionId === "_dev_";

  const cliSnippet = `# 1. Save the YAML to a file
cat > my-app.yaml <<'EOF'
${(yamlContent ?? "").trim()}
EOF

# 2. Deploy + chat in one shot
digitorn dev chat my-app -m ${JSON.stringify(prompt).replace(/^"|"$/g, '"')}`;

  const onTest = async () => {
    if (isDev) return;
    setResult({ status: "running", text: "" });
    const start = Date.now();
    try {
      const r = await fetch(`/api/apps/${appName}/sessions/${sessionId}/messages`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ message: prompt }),
      });
      if (!r.ok) {
        setResult({ status: "error", text: "", error: `HTTP ${r.status}`, durationMs: Date.now() - start });
        return;
      }
      const text = await r.text();
      setResult({ status: "ok", text, durationMs: Date.now() - start });
    } catch (e) {
      setResult({ status: "error", text: "", error: String(e), durationMs: Date.now() - start });
    }
  };

  const onCopy = () => {
    navigator.clipboard.writeText(cliSnippet).then(() => {
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    });
  };

  if (!open) return null;
  return (
    <aside className="absolute right-0 top-0 bottom-0 w-[420px] bg-surface-1 border-l border-border-subtle z-30 flex flex-col shadow-2xl">
      <div className="flex items-center gap-2 px-4 h-11 border-b border-border-subtle">
        <Play className="w-3.5 h-3.5 text-accent" />
        <span className="text-[11px] uppercase tracking-wider font-bold text-accent">
          Test prompt
        </span>
        <div className="flex-1" />
        <button onClick={onClose} className="text-ink-muted hover:text-ink" title="Close">
          <X className="w-4 h-4" />
        </button>
      </div>
      <div className="flex-1 overflow-y-auto p-4 space-y-4">
        <div>
          <label className="text-[10px] uppercase tracking-wider text-ink-dim font-medium mb-1 block">
            Prompt
          </label>
          <textarea
            value={prompt}
            onChange={(e) => setPrompt(e.target.value)}
            rows={3}
            className="w-full px-2 py-1.5 rounded-md bg-surface-2 border border-border-subtle text-xs text-ink"
          />
        </div>

        {isDev ? (
          <div className="rounded-lg border border-border-subtle bg-surface-2/40 p-3">
            <div className="flex items-center gap-2 mb-2">
              <Terminal className="w-3.5 h-3.5 text-ink-muted" />
              <span className="text-[10px] uppercase tracking-wider font-medium text-ink-dim">
                No daemon attached — run locally
              </span>
            </div>
            <div className="text-[11px] text-ink-muted leading-relaxed mb-3">
              The dev preview has no daemon to test against. Save your YAML, then run this snippet
              in your terminal — the dev CLI deploys the app, opens a session, and prints the
              first turn's output.
            </div>
            <pre className="text-[10px] font-mono text-ink-muted bg-surface-0 rounded p-2 overflow-x-auto whitespace-pre-wrap break-all">
              {cliSnippet}
            </pre>
            <button
              onClick={onCopy}
              className="mt-2 inline-flex items-center gap-1.5 h-7 px-2 rounded-md text-[10px] text-accent hover:bg-accent/15"
            >
              {copied ? <Check className="w-3 h-3" /> : <Copy className="w-3 h-3" />}
              {copied ? "Copied" : "Copy snippet"}
            </button>
          </div>
        ) : (
          <button
            onClick={onTest}
            disabled={result.status === "running"}
            className={clsx(
              "w-full inline-flex items-center justify-center gap-2 h-9 rounded-md text-xs font-semibold",
              result.status === "running"
                ? "bg-surface-3 text-ink-dim cursor-not-allowed"
                : "bg-accent text-surface-0 hover:bg-accent/90",
            )}
          >
            {result.status === "running" ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <Play className="w-3.5 h-3.5" />}
            {result.status === "running" ? "Running..." : "Send test prompt"}
          </button>
        )}

        {result.status === "ok" && (
          <div className="rounded-lg border border-status-ok/40 bg-status-ok/5 p-3">
            <div className="flex items-center gap-2 mb-1">
              <span className="w-2 h-2 rounded-full bg-status-ok" />
              <span className="text-[10px] uppercase tracking-wider font-medium text-status-ok">
                Response · {result.durationMs}ms
              </span>
            </div>
            <pre className="text-[11px] text-ink whitespace-pre-wrap font-mono mt-1">{result.text}</pre>
          </div>
        )}
        {result.status === "error" && (
          <div className="rounded-lg border border-status-error/40 bg-status-error/5 p-3 text-[11px] text-status-error">
            {result.error}
          </div>
        )}
      </div>
    </aside>
  );
}
