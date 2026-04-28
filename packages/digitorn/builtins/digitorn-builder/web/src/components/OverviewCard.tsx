import {
  Folder, Terminal, Globe, Database, Users, Code, Search, Layout, Mail,
  Settings, Shield, Box, Eye, AlertCircle, Zap, Brain, Send, Tag, Clock,
  GitBranch, Sparkles, Webhook, Check, FileText, Play, MessageCircle,
  type LucideIcon,
} from "lucide-react";
import clsx from "clsx";
import type { OverviewProfile, OverviewBadge } from "../lib/summarize";

const ICON_MAP: Record<string, LucideIcon> = {
  folder: Folder, terminal: Terminal, globe: Globe, database: Database,
  users: Users, code: Code, search: Search, layout: Layout, mail: Mail,
  settings: Settings, shield: Shield, box: Box, eye: Eye, alert: AlertCircle,
  zap: Zap, brain: Brain, send: Send, tag: Tag, clock: Clock, branch: GitBranch,
  sparkles: Sparkles, webhook: Webhook, check: Check, doc: FileText, play: Play,
  list: GitBranch, msg: MessageCircle, category: Tag,
};

const RISK_THEME: Record<"low" | "medium" | "high", { ring: string; bg: string; text: string; label: string }> = {
  low: { ring: "ring-status-ok/40", bg: "bg-status-ok/10", text: "text-status-ok", label: "low risk" },
  medium: { ring: "ring-status-warn/40", bg: "bg-status-warn/10", text: "text-status-warn", label: "medium risk" },
  high: { ring: "ring-status-error/40", bg: "bg-status-error/10", text: "text-status-error", label: "high risk" },
};

const BADGE_TONE: Record<OverviewBadge["variant"], string> = {
  ok: "bg-status-ok/15 text-status-ok ring-status-ok/30",
  warn: "bg-status-warn/15 text-status-warn ring-status-warn/30",
  info: "bg-kind-module/15 text-kind-module ring-kind-module/30",
  danger: "bg-status-error/15 text-status-error ring-status-error/30",
  muted: "bg-surface-3/60 text-ink-muted ring-border-subtle",
};

export default function OverviewCard({ profile }: { profile: OverviewProfile }) {
  const risk = profile.riskLevel ? RISK_THEME[profile.riskLevel] : null;
  return (
    <div className="px-4 pt-4 pb-3 space-y-3 border-b border-border-subtle bg-gradient-to-b from-surface-2/40 to-transparent">
      {(profile.topNote || risk) && (
        <div className="flex items-start justify-between gap-3">
          {profile.topNote ? (
            <div className="text-[11px] font-mono text-ink-muted truncate min-w-0">{profile.topNote}</div>
          ) : <div />}
          {risk && (
            <span
              className={clsx(
                "inline-flex items-center gap-1 px-2 py-0.5 rounded-md text-[10px] font-medium uppercase tracking-wider ring-1 flex-shrink-0",
                risk.ring,
                risk.bg,
                risk.text,
              )}
            >
              <Shield className="w-3 h-3" strokeWidth={2.5} />
              {risk.label}
            </span>
          )}
        </div>
      )}

      <p className="text-[12px] leading-relaxed text-ink-muted">
        {renderInline(profile.summary)}
      </p>

      {profile.badges.length > 0 && (
        <div className="flex flex-wrap gap-1.5">
          {profile.badges.map((b, i) => {
            const Icon = ICON_MAP[b.icon] ?? Box;
            return (
              <span
                key={`${b.label}-${i}`}
                className={clsx(
                  "inline-flex items-center gap-1 px-1.5 py-0.5 rounded text-[10px] font-mono ring-1",
                  BADGE_TONE[b.variant],
                )}
              >
                <Icon className="w-2.5 h-2.5" />
                {b.label}
              </span>
            );
          })}
        </div>
      )}

      {profile.keyFacts.length > 0 && (
        <div className={clsx(
          "grid gap-2 pt-1",
          profile.keyFacts.length <= 2 ? "grid-cols-2" :
          profile.keyFacts.length === 3 ? "grid-cols-3" :
          "grid-cols-2 sm:grid-cols-4",
        )}>
          {profile.keyFacts.map((f, i) => (
            <div
              key={`${f.label}-${i}`}
              className={clsx(
                "rounded-lg border border-border-subtle bg-surface-2/60 px-2 py-1.5",
                f.accent && "border-status-warn/30 bg-status-warn/5",
              )}
              title={f.hint}
            >
              <div className="text-[9px] uppercase tracking-wider text-ink-dim">
                {f.label}
              </div>
              <div className={clsx(
                "text-sm font-mono mt-0.5 truncate",
                f.accent ? "text-status-warn" : "text-ink",
              )}>
                {f.value}
              </div>
            </div>
          ))}
        </div>
      )}
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
