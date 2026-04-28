import {
  Clock, Filter, Zap, ArrowRight, Webhook, AlertOctagon, Pause,
} from "lucide-react";
import clsx from "clsx";
import type { HookFlow } from "../lib/describe-hook";

interface Props {
  flow: HookFlow;
  /** When true, the card title is muted (used inside the agent's
   *  "Hooks affecting this node" section to differentiate from the
   *  hook's own page). */
  compact?: boolean;
}

/**
 * Vertical "trigger pipeline" card for a single hook.
 *
 * Layout:
 *   ┌──────────────────────────────────────────┐
 *   │ ◆ hook_id                  [enabled]     │
 *   │                                          │
 *   │ ⏰ WHEN     →  Before each tool call      │
 *   │                                          │
 *   │ 🎯 BOUND TO →  filesystem.write           │
 *   │                                          │
 *   │ 🔍 IF       →  Triggering tool = "..."    │
 *   │                                          │
 *   │ ⚡ ACTION   →  Run LSP diagnostics         │
 *   │   • inject_result: yes                    │
 *   │   • publish: yes                          │
 *   │                                          │
 *   │ ↪ EFFECT   →  Agent sees lint output      │
 *   │   and can self-correct                    │
 *   └──────────────────────────────────────────┘
 */
export default function HookCard({ flow, compact }: Props) {
  return (
    <div
      className={clsx(
        "rounded-xl border border-border-subtle bg-surface-1 overflow-hidden",
        "border-l-[3px] border-l-kind-hook",
        flow.intercepts && "ring-1 ring-status-warn/30",
      )}
    >
      {/* Header */}
      <div className="flex items-center justify-between gap-2 px-3 py-2 border-b border-border-subtle bg-surface-2/40">
        <div className="flex items-center gap-2 min-w-0">
          <Webhook className="w-3.5 h-3.5 text-kind-hook flex-shrink-0" />
          <span
            className={clsx(
              "text-[12px] font-mono font-semibold truncate",
              compact ? "text-ink-muted" : "text-ink",
            )}
          >
            {flow.id}
          </span>
          {flow.intercepts && (
            <span
              className="text-[9px] font-mono uppercase tracking-wider px-1.5 py-0.5 rounded bg-status-warn/15 text-status-warn"
              title="This hook can intercept, block, or modify the trigger"
            >
              intercepts
            </span>
          )}
        </div>
        <div className="flex items-center gap-1.5 flex-shrink-0">
          {flow.cooldown > 0 && (
            <span
              className="text-[9px] font-mono text-ink-dim flex items-center gap-1"
              title={`Min ${flow.cooldown}s between fires`}
            >
              <Pause className="w-2.5 h-2.5" />
              {flow.cooldown}s
            </span>
          )}
          <span
            className={clsx(
              "text-[9px] font-mono uppercase tracking-wider px-1.5 py-0.5 rounded",
              flow.enabled
                ? "bg-status-ok/15 text-status-ok"
                : "bg-status-error/15 text-status-error",
            )}
          >
            {flow.enabled ? "enabled" : "disabled"}
          </span>
        </div>
      </div>

      {/* Vertical flow */}
      <div className="px-3 py-3 space-y-2">
        <Step
          icon={Clock}
          tone="info"
          label="WHEN"
          value={flow.when}
          hint={flow.whenHint}
        />

        <Connector />

        <Step
          icon={Zap}
          tone="info"
          label="BOUND TO"
          value={flow.bound}
          mono
        />

        <Connector />

        <Step
          icon={Filter}
          tone="muted"
          label="IF"
          value={flow.condition}
          mono
        />

        <Connector />

        <Step
          icon={ArrowRight}
          tone="warn"
          label="THEN"
          value={flow.actionLabel}
          extras={flow.actionDetails}
        />

        {flow.effect && flow.effect !== "—" && (
          <>
            <Connector />
            <Step
              icon={AlertOctagon}
              tone="ok"
              label="EFFECT"
              value={flow.effect}
            />
          </>
        )}
      </div>
    </div>
  );
}

function Connector() {
  return (
    <div className="ml-[14px] my-0.5 h-3 border-l border-dashed border-border" aria-hidden />
  );
}

interface StepProps {
  icon: typeof Clock;
  tone: "info" | "warn" | "ok" | "muted";
  label: string;
  value: string;
  hint?: string;
  mono?: boolean;
  extras?: string[];
}

const TONE: Record<StepProps["tone"], { iconBg: string; iconFg: string; valueFg: string }> = {
  info: { iconBg: "bg-status-running/15", iconFg: "text-status-running", valueFg: "text-ink" },
  warn: { iconBg: "bg-status-warn/15", iconFg: "text-status-warn", valueFg: "text-ink" },
  ok: { iconBg: "bg-status-ok/15", iconFg: "text-status-ok", valueFg: "text-ink-muted" },
  muted: { iconBg: "bg-surface-3 ", iconFg: "text-ink-muted", valueFg: "text-ink" },
};

function Step({ icon: Icon, tone, label, value, hint, mono, extras }: StepProps) {
  const t = TONE[tone];
  return (
    <div className="flex items-start gap-2.5" title={hint}>
      <div
        className={clsx(
          "flex-shrink-0 w-7 h-7 rounded-md flex items-center justify-center",
          t.iconBg,
        )}
      >
        <Icon className={clsx("w-3.5 h-3.5", t.iconFg)} strokeWidth={2.5} />
      </div>
      <div className="flex-1 min-w-0 pt-0.5">
        <div className="text-[9px] uppercase tracking-wider text-ink-dim font-medium">{label}</div>
        <div
          className={clsx(
            "text-[12px] mt-0.5 break-words",
            mono && "font-mono",
            t.valueFg,
          )}
        >
          {value}
        </div>
        {extras && extras.length > 0 && (
          <ul className="mt-1.5 space-y-0.5">
            {extras.map((d, i) => (
              <li
                key={i}
                className="text-[11px] font-mono text-ink-muted before:content-['•'] before:mr-1.5 before:text-ink-dim"
              >
                {d}
              </li>
            ))}
          </ul>
        )}
      </div>
    </div>
  );
}
