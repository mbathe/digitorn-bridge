import { useFileJson } from "@digitorn/preview-sdk";

/**
 * Phase 6 auto-test log. The builder writes ``_state/tests.json``
 * after each ``Chat(app_id, message, watch=true)`` run. Shape:
 *
 *   {
 *     "tests": [
 *       {"message": "bonjour", "response": "BONJOUR",
 *        "success": true, "duration_s": 2.5, "at": "2026-04-19T09:55:00Z"}
 *     ],
 *     "last_run_at": "2026-04-19T09:55:00Z"
 *   }
 *
 * If the file is absent we render a muted placeholder — the panel
 * only becomes useful once Phase 6 has fired at least once.
 */

interface TestResult {
  message: string;
  response?: string;
  success: boolean;
  duration_s?: number;
  at?: string;
  error?: string;
  tools_used?: string[];
}

interface TestsLog {
  tests: TestResult[];
  last_run_at?: string;
}

export default function AutoTestPanel() {
  const log = useFileJson<TestsLog>("_state/tests.json");
  const tests = log?.tests ?? [];

  if (tests.length === 0) {
    return (
      <div style={styles.empty}>
        <span style={styles.emptyLabel}>Auto-tests</span>
        <span style={styles.emptyHint}>
          Will appear when the builder runs <code>Chat(app_id, message, watch=true)</code> to verify
          the deployed app.
        </span>
      </div>
    );
  }

  const passing = tests.filter((t) => t.success).length;
  const total = tests.length;
  const allGreen = passing === total;

  return (
    <div style={styles.root}>
      <div style={styles.header}>
        <span
          style={{
            ...styles.status,
            color: allGreen ? "#10b981" : "#f59e0b",
            borderColor: allGreen ? "#10b98155" : "#f59e0b55",
          }}
        >
          {allGreen ? "✓" : "!"} Auto-test {passing}/{total}
        </span>
        {log?.last_run_at && (
          <span style={styles.ts}>{formatTs(log.last_run_at)}</span>
        )}
      </div>
      <div style={styles.rows}>
        {tests.slice(-6).map((t, i) => (
          <div key={i} style={styles.row}>
            <span
              style={{
                ...styles.dot,
                color: t.success ? "#10b981" : "#ef4444",
              }}
            >
              {t.success ? "✓" : "✗"}
            </span>
            <span style={styles.msg} title={t.message}>
              {t.message.slice(0, 60)}
            </span>
            <span style={styles.arrow}>→</span>
            <span
              style={styles.resp}
              title={t.response ?? t.error ?? ""}
            >
              {(t.response ?? t.error ?? "").slice(0, 80)}
            </span>
            {t.duration_s != null && (
              <span style={styles.dur}>{t.duration_s.toFixed(1)}s</span>
            )}
          </div>
        ))}
      </div>
    </div>
  );
}

function formatTs(iso: string): string {
  try {
    const d = new Date(iso);
    return d.toLocaleTimeString();
  } catch {
    return iso;
  }
}

const styles: Record<string, React.CSSProperties> = {
  root: {
    display: "flex",
    flexDirection: "column",
    gap: 4,
  },
  empty: {
    display: "flex",
    alignItems: "center",
    gap: 10,
    padding: "4px 2px",
  },
  emptyLabel: {
    fontSize: 11,
    fontWeight: 600,
    color: "#64748b",
    letterSpacing: 0.4,
    textTransform: "uppercase",
  },
  emptyHint: {
    fontSize: 10,
    color: "#475569",
  },
  header: {
    display: "flex",
    alignItems: "center",
    gap: 8,
    paddingBottom: 4,
  },
  status: {
    fontSize: 10,
    fontWeight: 700,
    padding: "2px 8px",
    borderRadius: 4,
    border: "1px solid",
    letterSpacing: 0.3,
  },
  ts: {
    fontSize: 10,
    color: "#64748b",
    fontFamily: "monospace",
  },
  rows: {
    display: "flex",
    flexDirection: "column",
    gap: 2,
  },
  row: {
    display: "grid",
    gridTemplateColumns: "16px 1fr 16px 1fr 48px",
    alignItems: "center",
    gap: 8,
    padding: "3px 6px",
    background: "#020617",
    border: "1px solid #1e293b",
    borderRadius: 4,
    fontSize: 11,
    fontFamily: "monospace",
  },
  dot: {
    fontWeight: 700,
    textAlign: "center",
  },
  msg: {
    color: "#cbd5e1",
    whiteSpace: "nowrap",
    overflow: "hidden",
    textOverflow: "ellipsis",
  },
  arrow: { color: "#475569", textAlign: "center" },
  resp: {
    color: "#a7f3d0",
    whiteSpace: "nowrap",
    overflow: "hidden",
    textOverflow: "ellipsis",
  },
  dur: {
    color: "#64748b",
    textAlign: "right",
    fontSize: 10,
  },
};
