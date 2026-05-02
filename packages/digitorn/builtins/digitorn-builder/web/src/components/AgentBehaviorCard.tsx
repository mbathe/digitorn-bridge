import {
  Brain, Folder, Terminal, Globe, Database, Users, Code, Search,
  Layout, Mail, Box, Settings, Shield, Thermometer, Sparkles, AlertTriangle,
  type LucideIcon,
} from "lucide-react";
import clsx from "clsx";
import type { AgentBehaviorProfile, Capability } from "../lib/summarize-agent";

interface Props {
  profile: AgentBehaviorProfile;
}

const ICON_MAP: Record<string, LucideIcon> = {
  folder: Folder, terminal: Terminal, globe: Globe, database: Database,
  users: Users, code: Code, search: Search, layout: Layout, mail: Mail,
  settings: Settings, box: Box,
};

const RISK_THEME: Record<"low" | "medium" | "high", { ring: string; bg: string; text: string; label: string }> = {
  low: { ring: "ring-status-ok/40", bg: "bg-status-ok/10", text: "text-status-ok", label: "low risk" },
  medium: { ring: "ring-status-warn/40", bg: "bg-status-warn/10", text: "text-status-warn", label: "medium risk" },
  high: { ring: "ring-status-error/40", bg: "bg-status-error/10", text: "text-status-error", label: "high risk" },
};

const TEMP_DESCR: Record<string, string> = {
  deterministic: "deterministic — same prompt → same answer",
  precise: "precise — high consistency, low creativity",
  balanced: "balanced — pragmatic mix of consistency + flexibility",
  creative: "creative — willing to explore alternatives",
  random: "random — outputs vary widely",
};

export default function AgentBehaviorCard({ profile }: Props) {
  const risk = RISK_THEME[profile.risk];
  return (
    <div className="px-4 pt-4 pb-3 space-y-3 border-b border-border-subtle bg-gradient-to-b from-surface-2/40 to-transparent">
      {/* Top: brain + risk */}
      <div className="flex items-start justify-between gap-3">
        <div className="flex items-center gap-2 min-w-0">
          <div className="w-7 h-7 flex items-center justify-center rounded-lg bg-kind-agent/15 text-kind-agent">
            <Brain className="w-4 h-4" />
          </div>
          <div className="min-w-0">
            <div className="text-xs font-mono text-ink truncate" title={profile.brainLabel}>
              {profile.brainLabel}
            </div>
            {profile.fallbackBrainLabel && (
              <div
                className="text-[10px] font-mono text-status-warn truncate flex items-center gap-1 mt-0.5"
                title={`Billing-failover brain: when ${profile.brainLabel} returns 402 / Insufficient Balance, the daemon switches to ${profile.fallbackBrainLabel} for that turn.`}
              >
                <span className="opacity-80">↩</span>
                <span className="truncate">{profile.fallbackBrainLabel}</span>
              </div>
            )}
            <div className="text-[10px] text-ink-dim">
              {profile.toolCount > 0 ? `${profile.toolCount} tools available` : "no external tools"}
            </div>
          </div>
        </div>
        <span
          className={clsx(
            "inline-flex items-center gap-1 px-2 py-0.5 rounded-md text-[10px] font-medium uppercase tracking-wider ring-1",
            risk.ring,
            risk.bg,
            risk.text,
          )}
          title={`Inferred from granted actions${profile.guarded ? " · behavior engine guards in place" : ""}`}
        >
          <Shield className="w-3 h-3" strokeWidth={2.5} />
          {risk.label}
        </span>
      </div>

      {/* Plain-language summary */}
      <p className="text-[12px] leading-relaxed text-ink-muted">
        {renderInline(profile.summary)}
      </p>

      {/* Capability badges */}
      {profile.capabilities.length > 0 && (
        <div className="flex flex-wrap gap-1.5">
          {profile.capabilities.map((c) => (
            <CapBadge key={c.key} cap={c} />
          ))}
        </div>
      )}

      {/* Quick facts row */}
      <div className="grid grid-cols-3 gap-2 pt-1">
        <Fact
          icon={Thermometer}
          label="Temp"
          value={profile.temperature !== undefined ? String(profile.temperature) : "?"}
          hint={TEMP_DESCR[profile.tempTone]}
          accent={profile.tempTone === "creative" || profile.tempTone === "random"}
        />
        <Fact
          icon={Sparkles}
          label="Plans"
          value={profile.plansFirst ? "yes" : "no"}
          hint={profile.plansFirst ? "Plans before acting" : "Acts directly"}
        />
        <Fact
          icon={profile.spawnsSubAgents ? Users : AlertTriangle}
          label="Sub-agents"
          value={profile.spawnsSubAgents ? "yes" : "no"}
          hint={profile.spawnsSubAgents ? "Can spawn specialists" : "Solo agent"}
        />
      </div>
    </div>
  );
}

function CapBadge({ cap }: { cap: Capability }) {
  const Icon = ICON_MAP[cap.icon] ?? Box;
  const tone = cap.variant === "warn"
    ? "bg-status-warn/15 text-status-warn ring-status-warn/30"
    : cap.variant === "info"
    ? "bg-kind-module/15 text-kind-module ring-kind-module/30"
    : "bg-status-ok/15 text-status-ok ring-status-ok/30";
  return (
    <span
      className={clsx(
        "inline-flex items-center gap-1 px-1.5 py-0.5 rounded text-[10px] font-mono ring-1",
        tone,
      )}
    >
      <Icon className="w-2.5 h-2.5" />
      {cap.label}
    </span>
  );
}

function Fact({
  icon: Icon, label, value, hint, accent,
}: {
  icon: LucideIcon; label: string; value: string; hint?: string; accent?: boolean;
}) {
  return (
    <div
      className={clsx(
        "rounded-lg border border-border-subtle bg-surface-2/60 px-2 py-1.5",
        accent && "border-status-warn/30 bg-status-warn/5",
      )}
      title={hint}
    >
      <div className="flex items-center gap-1 text-[9px] uppercase tracking-wider text-ink-dim">
        <Icon className="w-2.5 h-2.5" />
        {label}
      </div>
      <div className={clsx("text-sm font-mono mt-0.5", accent ? "text-status-warn" : "text-ink")}>
        {value}
      </div>
    </div>
  );
}

/** Render markdown-lite **bold** spans inside a plain string. */
function renderInline(s: string): React.ReactNode {
  const parts: React.ReactNode[] = [];
  let i = 0;
  let key = 0;
  while (i < s.length) {
    const start = s.indexOf("**", i);
    if (start === -1) { parts.push(s.slice(i)); break; }
    const end = s.indexOf("**", start + 2);
    if (end === -1) { parts.push(s.slice(i)); break; }
    if (start > i) parts.push(s.slice(i, start));
    parts.push(<strong key={key++} className="text-ink font-semibold">{s.slice(start + 2, end)}</strong>);
    i = end + 2;
  }
  return parts;
}
