import { useFileJson } from "@digitorn/preview-sdk";

interface CompileResult {
  status: string;
  errors?: string[];
  warnings?: string[];
  app_id?: string;
}

export default function CompileStatus() {
  const compile = useFileJson<CompileResult>("_state/compile.json");

  if (!compile) {
    return (
      <span style={{ ...badge, color: "#64748b", borderColor: "#334155" }}>
        no compile
      </span>
    );
  }

  const isOk = compile.status === "ok" || compile.status === "success";
  const isErr = compile.status === "error" || compile.status === "failed";
  const color = isOk ? "#10b981" : isErr ? "#ef4444" : "#f59e0b";
  const errCount = compile.errors?.length ?? 0;
  const warnCount = compile.warnings?.length ?? 0;

  return (
    <span
      style={{ ...badge, color, borderColor: `${color}66` }}
      title={
        isErr && compile.errors
          ? compile.errors.join("\n")
          : warnCount > 0 && compile.warnings
            ? compile.warnings.join("\n")
            : compile.status
      }
    >
      {isOk && "compiled"}
      {isErr && `${errCount} error${errCount !== 1 ? "s" : ""}`}
      {!isOk && !isErr && compile.status}
      {warnCount > 0 && !isErr && ` (${warnCount} warn)`}
    </span>
  );
}

const badge: React.CSSProperties = {
  fontSize: 10,
  fontWeight: 600,
  fontFamily: "monospace",
  padding: "2px 8px",
  borderRadius: 4,
  border: "1px solid",
  letterSpacing: 0.3,
};
