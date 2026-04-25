import { useFileJson } from "@digitorn/preview-sdk";

/**
 * Horizontal timeline of the builder's 8 canonical states.
 *
 * The agent writes ``_state/progress.json`` every time it transitions
 * between states using ``workspace.write``. Shape:
 *
 *   {"current": 2, "label": "Interview",
 *    "completed": [0, 1], "total": 8, "error": "..."}
 *
 * The client renders the 8 steps and uses `current` + `completed` to
 * decide each step's visual state. No typed preview API involved —
 * just a JSON file in the workspace.
 */

interface Progress {
  current: number;
  label?: string;
  completed?: number[];
  total?: number;
  error?: string;
}

const STEPS = [
  { key: "STATE 0", label: "Discovery" },
  { key: "STATE 1", label: "Pattern match" },
  { key: "STATE 2", label: "Interview" },
  { key: "STATE 3", label: "Generation" },
  { key: "STATE 4", label: "Compile loop" },
  { key: "STATE 5", label: "Persist draft" },
  { key: "STATE 6", label: "Propose deploy" },
  { key: "STATE 7", label: "Propose package" },
];

type StepStatus = "done" | "running" | "pending" | "error";

function stepStatus(
  progress: Progress | undefined,
  idx: number,
): StepStatus {
  if (!progress) return "pending";
  const completed = new Set(progress.completed ?? []);
  if (completed.has(idx)) return "done";
  if (idx === progress.current) {
    return progress.error ? "error" : "running";
  }
  return "pending";
}

const STATUS_STYLES: Record<StepStatus, React.CSSProperties> = {
  done: { color: "#10b981", border: "1px solid #10b981" },
  running: {
    color: "#f59e0b",
    border: "1px solid #f59e0b",
    boxShadow: "0 0 10px rgba(245, 158, 11, 0.45)",
  },
  error: {
    color: "#ef4444",
    border: "1px solid #ef4444",
    boxShadow: "0 0 10px rgba(239, 68, 68, 0.45)",
  },
  pending: { color: "#475569", border: "1px solid #1e293b" },
};

export default function StateTimeline() {
  const progress = useFileJson<Progress>("_state/progress.json");

  return (
    <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
      <div style={{ display: "flex", gap: 8, flex: 1, overflowX: "auto" }}>
        {STEPS.map((step, i) => {
          const status = stepStatus(progress, i);
          const s = STATUS_STYLES[status];
          return (
            <div
              key={step.key}
              style={{
                padding: "4px 10px",
                borderRadius: 999,
                fontSize: 10,
                fontWeight: 600,
                letterSpacing: 0.3,
                whiteSpace: "nowrap",
                background: "#0b1120",
                ...s,
              }}
              title={step.key}
            >
              {step.label}
            </div>
          );
        })}
      </div>
      <div
        style={{
          fontSize: 10,
          color: progress?.error ? "#fca5a5" : "#64748b",
          fontFamily: "monospace",
          minWidth: 140,
          textAlign: "right",
        }}
      >
        {progress
          ? progress.error
            ? `⚠ ${progress.error}`
            : `${progress.label ?? STEPS[progress.current]?.label ?? ""}`
          : "waiting for first write…"}
      </div>
    </div>
  );
}
