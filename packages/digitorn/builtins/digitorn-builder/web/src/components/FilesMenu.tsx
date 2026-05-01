/**
 * Files menu — toolbar dropdown that surfaces the build pipeline
 * status (YAML / Deploy / Tests) without occupying canvas space.
 *
 * Replaces the floating ReadyDashboard card. The toolbar button shows
 * a single status dot (red/orange/green) so the user can see at a
 * glance if something needs attention; clicking opens the popover
 * with detailed chips and tooltips.
 */
import { useEffect, useRef, useState } from "react";
import { useFile, useFileJson } from "@digitorn/preview-sdk";
import { FileBox, ChevronDown } from "lucide-react";
import clsx from "clsx";

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
}
interface TestsLog { tests: TestResult[]; last_run_at?: string }
interface Progress { current?: number; label?: string; error?: string }

type ChipState = "ok" | "warn" | "error" | "pending";

const CHIP_TONE: Record<ChipState, string> = {
  ok:      "bg-status-ok/15 text-status-ok border-status-ok/40",
  warn:    "bg-status-warn/15 text-status-warn border-status-warn/40",
  error:   "bg-status-error/15 text-status-error border-status-error/40",
  pending: "bg-surface-2 text-ink-dim border-border-subtle",
};
const DOT_TONE: Record<ChipState, string> = {
  ok: "bg-status-ok",
  warn: "bg-status-warn",
  error: "bg-status-error",
  pending: "bg-ink-dim/40",
};

function aggregate(states: ChipState[]): ChipState {
  if (states.includes("error")) return "error";
  if (states.includes("warn")) return "warn";
  if (states.every((s) => s === "ok")) return "ok";
  return "pending";
}

export default function FilesMenu() {
  const yamlContent = useFile("app.yaml");
  const compile = useFileJson<CompileResult>("_state/compile.json");
  const deploy = useFileJson<DeployResult>("_state/deploy.json");
  const tests = useFileJson<TestsLog>("_state/tests.json");
  const progress = useFileJson<Progress>("_state/progress.json");

  const hasYaml = !!yamlContent && yamlContent.trim().length > 20;
  const compileOk =
    compile?.status === "ok" || compile?.status === "success" ||
    ((compile?.errors?.length ?? 0) === 0 && !!compile);
  const compileErrs = compile?.errors?.length ?? 0;
  const yamlState: ChipState =
    !hasYaml ? "pending"
    : compile && compileErrs === 0 && compileOk ? "ok"
    : compile && compileErrs > 0 ? "error"
    : "warn";
  const deployState: ChipState =
    deploy?.status === "success" || (deploy?.app_id && !deploy.error) ? "ok"
    : deploy?.error ? "error"
    : "pending";
  const testList = tests?.tests ?? [];
  const passing = testList.filter((t) => t.success).length;
  const testState: ChipState =
    testList.length === 0 ? "pending"
    : passing === testList.length ? "ok"
    : passing > 0 ? "warn"
    : "error";

  const overall = aggregate([yamlState, deployState, testState]);

  const [open, setOpen] = useState(false);
  const wrapRef = useRef<HTMLDivElement>(null);

  // Close on outside click
  useEffect(() => {
    if (!open) return;
    const onClick = (e: MouseEvent) => {
      if (wrapRef.current && !wrapRef.current.contains(e.target as Node)) {
        setOpen(false);
      }
    };
    window.addEventListener("mousedown", onClick);
    return () => window.removeEventListener("mousedown", onClick);
  }, [open]);

  return (
    <div ref={wrapRef} className="relative">
      <button
        onClick={() => setOpen((o) => !o)}
        className={clsx(
          "inline-flex items-center gap-1.5 h-8 px-2.5 rounded-lg text-xs",
          "text-ink-muted hover:text-ink hover:bg-surface-2",
          open && "bg-surface-2 text-ink",
        )}
        title="Build pipeline status (YAML / Deploy / Tests)"
      >
        <FileBox className="w-3.5 h-3.5" />
        <span>Files</span>
        <span className={clsx("w-1.5 h-1.5 rounded-full", DOT_TONE[overall])} />
        <ChevronDown className={clsx("w-3 h-3 transition-transform", open && "rotate-180")} />
      </button>

      {open && (
        <div
          className="absolute right-0 top-9 z-30 w-[260px] rounded-xl bg-surface-1 border border-border shadow-2xl overflow-hidden"
        >
          <div className="px-3 py-2 border-b border-border-subtle">
            <div className={clsx(
              "text-[10px] uppercase tracking-wider font-bold",
              overall === "ok" ? "text-status-ok"
              : overall === "error" ? "text-status-error"
              : overall === "warn" ? "text-status-warn"
              : "text-ink-dim",
            )}>
              {overall === "ok" ? "Ready to ship" : overall === "error" ? "Errors blocking" : "Building"}
            </div>
          </div>
          <div className="p-2 space-y-1">
            <DetailChip
              state={yamlState}
              label={compileErrs > 0 ? `YAML (${compileErrs} err)` : "YAML"}
              detail={
                compileErrs > 0 ? compile?.errors?.slice(0, 3).join(" · ")
                : hasYaml ? "Parsed & compiled."
                : "Waiting for app.yaml"
              }
            />
            <DetailChip
              state={deployState}
              label="Deploy"
              detail={
                deploy?.error ? deploy.error
                : deploy?.app_id ? `Deployed as ${deploy.app_id}`
                : "Not deployed yet"
              }
            />
            <DetailChip
              state={testState}
              label={testList.length > 0 ? `Tests ${passing}/${testList.length}` : "Tests"}
              detail={
                testList.length === 0 ? "No auto-tests run yet."
                : testList.slice(-3).map((t) => `${t.success ? "✓" : "✗"} ${t.message.slice(0, 36)}`).join(" · ")
              }
            />
          </div>
          {progress?.label && (
            <div className="px-3 py-2 border-t border-border-subtle text-[10px] font-mono text-ink-muted">
              {progress.error ? "⚠ " : "◆ "}{progress.label}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

function DetailChip({ state, label, detail }: { state: ChipState; label: string; detail?: string }) {
  return (
    <div className={clsx("rounded-md border px-2.5 py-1.5", CHIP_TONE[state])}>
      <div className="flex items-center gap-1.5">
        <span className={clsx("w-1.5 h-1.5 rounded-full", DOT_TONE[state])} />
        <span className="text-[11px] font-semibold">{label}</span>
      </div>
      {detail && (
        <div className="text-[10px] text-ink-muted mt-1 line-clamp-2 leading-snug">
          {detail}
        </div>
      )}
    </div>
  );
}
