import { useFile, useFileJson } from "@digitorn/preview-sdk";

/**
 * Floating top-right card showing "is this app ready?" at a glance.
 *
 * Three indicators aggregate the full pipeline state:
 *  1. YAML valid — parsed without error + compile.json has no errors
 *  2. Deployed — deploy.json reports success + app_id set
 *  3. Tests passing — tests.json has at least one green result
 *
 * Each indicator is a small chip (dot + label) with a tooltip giving
 * the exact failure when red. When all three are green, the card gets
 * a celebratory ring + subtle pulse.
 */

interface CompileResult {
  status?: string;
  errors?: string[];
  warnings?: string[];
  app_id?: string;
}

interface DeployResult {
  status?: string;
  app_id?: string;
  error?: string;
}

interface TestResult {
  message: string;
  response?: string;
  success: boolean;
  duration_s?: number;
  at?: string;
}

interface TestsLog {
  tests: TestResult[];
  last_run_at?: string;
}

interface Progress {
  current?: number;
  label?: string;
  error?: string;
}

type ChipState = "ok" | "warn" | "error" | "pending";

const CHIP_COLORS: Record<ChipState, { color: string; bg: string; border: string }> = {
  ok: { color: "#10b981", bg: "#052e20", border: "#10b98155" },
  warn: { color: "#f59e0b", bg: "#2a1d06", border: "#f59e0b55" },
  error: { color: "#ef4444", bg: "#2b0b0b", border: "#ef444455" },
  pending: { color: "#64748b", bg: "#0b1120", border: "#334155" },
};

function Chip({
  state,
  label,
  hint,
}: {
  state: ChipState;
  label: string;
  hint?: string;
}) {
  const s = CHIP_COLORS[state];
  const dot =
    state === "ok" ? "●" : state === "error" ? "●" : state === "warn" ? "●" : "○";
  return (
    <div
      title={hint}
      style={{
        display: "flex",
        alignItems: "center",
        gap: 6,
        padding: "4px 8px",
        borderRadius: 6,
        fontSize: 11,
        fontWeight: 600,
        letterSpacing: 0.2,
        background: s.bg,
        color: s.color,
        border: `1px solid ${s.border}`,
      }}
    >
      <span style={{ fontSize: 10, lineHeight: 1 }}>{dot}</span>
      <span>{label}</span>
    </div>
  );
}

export default function ReadyDashboard() {
  const yamlContent = useFile("app.yaml");
  const compile = useFileJson<CompileResult>("_state/compile.json");
  const deploy = useFileJson<DeployResult>("_state/deploy.json");
  const tests = useFileJson<TestsLog>("_state/tests.json");
  const progress = useFileJson<Progress>("_state/progress.json");

  const hasYaml = !!yamlContent && yamlContent.trim().length > 20;
  const compileOk =
    compile?.status === "ok" ||
    compile?.status === "success" ||
    ((compile?.errors?.length ?? 0) === 0 && !!compile);
  const compileErrs = compile?.errors?.length ?? 0;

  const yamlState: ChipState = !hasYaml
    ? "pending"
    : compile && compileErrs === 0 && compileOk
      ? "ok"
      : compile && compileErrs > 0
        ? "error"
        : "warn";

  const deployState: ChipState =
    deploy?.status === "success" || (deploy?.app_id && !deploy.error)
      ? "ok"
      : deploy?.error
        ? "error"
        : "pending";

  const testList = tests?.tests ?? [];
  const passing = testList.filter((t) => t.success).length;
  const testState: ChipState =
    testList.length === 0
      ? "pending"
      : passing === testList.length
        ? "ok"
        : passing > 0
          ? "warn"
          : "error";

  const allGreen =
    yamlState === "ok" && deployState === "ok" && testState === "ok";

  return (
    <div
      style={{
        position: "absolute",
        top: 16,
        right: 16,
        zIndex: 10,
        display: "flex",
        flexDirection: "column",
        gap: 6,
        padding: 10,
        background: "#0b1120f2",
        border: allGreen ? "1px solid #10b98155" : "1px solid #1e293b",
        borderRadius: 10,
        boxShadow: allGreen
          ? "0 0 16px rgba(16, 185, 129, 0.25)"
          : "0 4px 12px rgba(0, 0, 0, 0.3)",
        backdropFilter: "blur(6px)",
        minWidth: 200,
      }}
    >
      <div
        style={{
          fontSize: 10,
          fontWeight: 700,
          letterSpacing: 0.8,
          textTransform: "uppercase",
          color: allGreen ? "#10b981" : "#94a3b8",
          paddingBottom: 4,
          borderBottom: "1px solid #1e293b",
        }}
      >
        {allGreen ? "Ready to ship" : "Building"}
      </div>

      <Chip
        state={yamlState}
        label={compileErrs > 0 ? `YAML (${compileErrs} err)` : "YAML"}
        hint={
          compileErrs > 0
            ? compile?.errors?.join("\n")
            : hasYaml
              ? "Parsed & compiled"
              : "Waiting for app.yaml"
        }
      />
      <Chip
        state={deployState}
        label={deploy?.app_id ? `Deploy` : "Deploy"}
        hint={
          deploy?.error
            ? deploy.error
            : deploy?.app_id
              ? `Deployed as ${deploy.app_id}`
              : "Not deployed yet"
        }
      />
      <Chip
        state={testState}
        label={
          testList.length > 0
            ? `Tests ${passing}/${testList.length}`
            : "Tests"
        }
        hint={
          testList.length === 0
            ? "No auto-tests run yet"
            : testList
                .slice(-5)
                .map(
                  (t) =>
                    `${t.success ? "✓" : "✗"} ${t.message.slice(0, 40)}${
                      t.response ? ` → ${t.response.slice(0, 40)}` : ""
                    }`,
                )
                .join("\n")
        }
      />

      {progress?.label && (
        <div
          style={{
            fontSize: 10,
            color: "#64748b",
            paddingTop: 6,
            borderTop: "1px solid #1e293b",
            fontFamily: "monospace",
          }}
        >
          {progress.error ? "⚠ " : "◆ "}
          {progress.label}
        </div>
      )}
    </div>
  );
}
