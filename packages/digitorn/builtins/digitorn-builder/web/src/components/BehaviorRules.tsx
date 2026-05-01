/**
 * Behavior rule_definitions drilldown.
 *
 * Renders each rule as an inspectable card with: id, trigger tools,
 * when (pre_tool / post_tool / on_text), action (block / warn / remind),
 * condition expression, message template.
 */
import { useState } from "react";
import { Shield, AlertTriangle, MessageCircle, ChevronDown, ChevronRight, Ban, Info } from "lucide-react";
import clsx from "clsx";

interface Rule {
  id?: string;
  description?: string;
  trigger?: string | string[];
  when?: string;
  action?: string;
  condition?: Record<string, unknown> | string;
  message?: string;
  [k: string]: unknown;
}

interface BehaviorBlock {
  profile?: string;
  classify_turns?: boolean;
  classifier?: Record<string, unknown>;
  rule_definitions?: Rule[];
  custom?: Rule[];
  state_tracking?: Record<string, unknown>;
}

interface Props {
  behavior: BehaviorBlock;
}

const ACTION_TONE: Record<string, { bg: string; fg: string; icon: typeof Shield; label: string }> = {
  block:  { bg: "bg-status-error/15",   fg: "text-status-error",   icon: Ban,             label: "BLOCK" },
  warn:   { bg: "bg-status-warn/15",    fg: "text-status-warn",    icon: AlertTriangle,   label: "WARN" },
  remind: { bg: "bg-status-running/15", fg: "text-status-running", icon: MessageCircle,   label: "REMIND" },
};

export default function BehaviorRules({ behavior }: Props) {
  const rules: Rule[] = [
    ...(behavior.rule_definitions ?? []),
    ...(behavior.custom ?? []),
  ];

  if (rules.length === 0) {
    return (
      <div className="p-4 text-xs text-ink-dim italic">
        No rule_definitions declared.
        {behavior.classify_turns
          ? " Classifier alone runs — it injects strategic directives BEFORE the LLM but doesn't enforce per-tool checks."
          : " Add `behavior.rule_definitions: [...]` to enforce runtime checks around each tool call."}
      </div>
    );
  }

  return (
    <div className="p-4 space-y-2">
      <div className="text-[10px] uppercase tracking-wider text-ink-dim font-semibold mb-2">
        {rules.length} rule{rules.length > 1 ? "s" : ""} · classifier {behavior.classify_turns ? "ON" : "OFF"} · profile: {behavior.profile ?? "custom"}
      </div>
      {rules.map((rule, i) => (
        <RuleCard key={rule.id ?? i} rule={rule} index={i} />
      ))}
    </div>
  );
}

function RuleCard({ rule, index }: { rule: Rule; index: number }) {
  const [expanded, setExpanded] = useState(false);
  const action = String(rule.action ?? "warn").toLowerCase();
  const tone = ACTION_TONE[action] ?? ACTION_TONE.warn;
  const ToneIcon = tone.icon;
  const triggers = Array.isArray(rule.trigger) ? rule.trigger : (rule.trigger ? [rule.trigger] : []);
  const when = rule.when ?? "(any time)";
  const condString = rule.condition
    ? (typeof rule.condition === "string" ? rule.condition : Object.keys(rule.condition).join(", "))
    : "always";

  return (
    <div
      className={clsx(
        "rounded-lg border bg-surface-2/40",
        "border-border-subtle hover:border-border transition-colors",
      )}
    >
      <button
        onClick={() => setExpanded((e) => !e)}
        className="w-full flex items-start gap-3 p-3 text-left"
      >
        <div className={clsx("flex-shrink-0 w-7 h-7 rounded-md flex items-center justify-center", tone.bg, tone.fg)}>
          <ToneIcon className="w-3.5 h-3.5" />
        </div>
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2 mb-0.5">
            <span className="text-xs font-mono font-semibold text-ink truncate">
              {rule.id ?? `rule-${index}`}
            </span>
            <span className={clsx("px-1.5 py-0.5 rounded text-[9px] font-bold uppercase tracking-wider", tone.bg, tone.fg)}>
              {tone.label}
            </span>
            <span className="text-[9px] uppercase font-mono text-ink-dim">{when}</span>
          </div>
          {rule.description && (
            <div className="text-[11px] text-ink-muted leading-snug">{rule.description}</div>
          )}
          <div className="flex flex-wrap gap-1 mt-1.5">
            {triggers.length > 0 ? triggers.slice(0, 5).map((t) => (
              <span key={String(t)} className="text-[9px] font-mono px-1.5 py-0.5 rounded bg-surface-3 text-ink-muted">
                {String(t)}
              </span>
            )) : (
              <span className="text-[9px] text-ink-dim italic">all tools</span>
            )}
          </div>
        </div>
        {expanded ? <ChevronDown className="w-3 h-3 text-ink-dim mt-2" /> : <ChevronRight className="w-3 h-3 text-ink-dim mt-2" />}
      </button>
      {expanded && (
        <div className="px-3 pb-3 space-y-2 border-t border-border-subtle/50 pt-2">
          <div>
            <div className="text-[9px] uppercase tracking-wider text-ink-dim font-semibold">Condition</div>
            <div className="text-[10px] font-mono text-ink-muted mt-0.5">{condString}</div>
          </div>
          {rule.message && (
            <div>
              <div className="text-[9px] uppercase tracking-wider text-ink-dim font-semibold flex items-center gap-1">
                <Info className="w-2.5 h-2.5" /> Message injected
              </div>
              <div className="text-[10px] text-ink mt-0.5 italic leading-snug">"{rule.message}"</div>
            </div>
          )}
          <pre className="text-[9px] font-mono text-ink-dim bg-surface-0 rounded p-2 overflow-x-auto whitespace-pre-wrap">
            {JSON.stringify(rule, null, 2)}
          </pre>
        </div>
      )}
    </div>
  );
}
