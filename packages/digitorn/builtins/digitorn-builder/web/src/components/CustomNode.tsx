import { memo } from "react";
import { Handle, Position, useStore, type ReactFlowState } from "reactflow";
import {
  User, Box, Brain, Folder, Terminal, Database, Globe, Eye, Users,
  Cpu, Cloud, Search, Archive, Zap, Clock, Send, Activity, AlertCircle,
  ArrowDown, ArrowUp, Settings, Sparkles, Bot, Wrench, Layers, Layout,
  GitBranch, Mail, Webhook, Repeat, FileCode, Network, MessageCircle,
  Shield, FileText, Palette, ShieldCheck,
  type LucideIcon,
} from "lucide-react";
import clsx from "clsx";
import type { NodeData, NodeKind } from "../lib/yaml-to-graph";
import type { ExtraNodeData, AnyKind } from "../lib/extra-nodes";

/* ─────────────────────────────────────────────────────────────────
   Icon registry — maps the legacy yaml-to-graph icon names to
   modern Lucide icons. Keeps backward compat with the parser.
   ─────────────────────────────────────────────────────────────── */

const ICON_MAP: Record<string, LucideIcon> = {
  user: User, cube: Box, brain: Brain, folder: Folder, terminal: Terminal,
  database: Database, globe: Globe, eye: Eye, users: Users, cpu: Cpu,
  cloud: Cloud, search: Search, archive: Archive, box: Box, zap: Zap,
  clock: Clock, send: Send, activity: Activity, alert: AlertCircle,
  "arrow-down": ArrowDown, "arrow-up": ArrowUp, settings: Settings,
  shield: Shield, layout: Layout, doc: FileText, palette: Palette,
  "shield-check": ShieldCheck,
  // Modern aliases for kinds without a yaml-to-graph icon
  agent: Bot, app: Sparkles, module: Wrench, skill: FileCode, hook: Webhook,
  trigger: Zap, channel: Mail, loop: Repeat, hierarchy: Layers,
  branch: GitBranch, network: Network, msg: MessageCircle, code: FileCode,
};

function pickIcon(kind: AnyKind, name?: string): LucideIcon {
  if (name && ICON_MAP[name]) return ICON_MAP[name];
  switch (kind) {
    case "agent": return Bot;
    case "module": return Wrench;
    case "trigger": return Zap;
    case "channel": return Mail;
    case "hook": return Webhook;
    case "user":
    case "input":
    case "output": return User;
    case "app": return Sparkles;
    case "variable":
    case "variables": return Settings;
    case "error": return AlertCircle;
    case "skill": return FileCode;
    case "palette": return FileCode;
    case "workspace": return Layout;
    case "behavior": return Shield;
    case "widgets": return Layout;
    case "preview": return Eye;
    case "capabilities": return Shield;
    case "middleware": return GitBranch;
    case "theme": return Palette;
    case "features": return Eye;
    case "mcp_server": return ShieldCheck;
    case "credentials": return ShieldCheck;
    case "credential_provider": return ShieldCheck;
    case "fallback_brain": return Brain;
    case "include": return GitBranch;
    case "approval_gate": return Shield;
    case "sandbox": return Shield;
    case "pipeline_step": return Sparkles;
    default: return Box;
  }
}

/* ─────────────────────────────────────────────────────────────────
   Kind → tailwind theme tokens
   ─────────────────────────────────────────────────────────────── */

type ThemeEntry = {
  ring: string;
  iconBg: string;
  iconFg: string;
  badge: string;
  shape: "rounded-2xl" | "rounded-xl" | "rounded-lg";
  width: number;
};

const KIND_THEME: Record<AnyKind, ThemeEntry> = {
  user:        { ring: "ring-kind-io",       iconBg: "bg-kind-io/15",       iconFg: "text-kind-io",       badge: "bg-kind-io/15 text-kind-io",       shape: "rounded-2xl", width: 200 },
  input:       { ring: "ring-kind-io",       iconBg: "bg-kind-io/15",       iconFg: "text-kind-io",       badge: "bg-kind-io/15 text-kind-io",       shape: "rounded-2xl", width: 200 },
  output:      { ring: "ring-kind-io",       iconBg: "bg-kind-io/15",       iconFg: "text-kind-io",       badge: "bg-kind-io/15 text-kind-io",       shape: "rounded-2xl", width: 200 },
  app:         { ring: "ring-kind-app",      iconBg: "bg-kind-app/15",      iconFg: "text-kind-app",      badge: "bg-kind-app/15 text-kind-app",     shape: "rounded-xl",  width: 260 },
  agent:       { ring: "ring-kind-agent",    iconBg: "bg-kind-agent/15",    iconFg: "text-kind-agent",    badge: "bg-kind-agent/15 text-kind-agent", shape: "rounded-xl",  width: 260 },
  module:      { ring: "ring-kind-module",   iconBg: "bg-kind-module/15",   iconFg: "text-kind-module",   badge: "bg-kind-module/15 text-kind-module",   shape: "rounded-lg", width: 220 },
  trigger:     { ring: "ring-kind-trigger",  iconBg: "bg-kind-trigger/15",  iconFg: "text-kind-trigger",  badge: "bg-kind-trigger/15 text-kind-trigger", shape: "rounded-lg", width: 220 },
  channel:     { ring: "ring-kind-channel",  iconBg: "bg-kind-channel/15",  iconFg: "text-kind-channel",  badge: "bg-kind-channel/15 text-kind-channel", shape: "rounded-lg", width: 220 },
  hook:        { ring: "ring-kind-hook",     iconBg: "bg-kind-hook/15",     iconFg: "text-kind-hook",     badge: "bg-kind-hook/15 text-kind-hook",      shape: "rounded-lg", width: 220 },
  variable:    { ring: "ring-kind-io",       iconBg: "bg-kind-io/15",       iconFg: "text-kind-io",       badge: "bg-kind-io/15 text-kind-io",       shape: "rounded-lg", width: 200 },
  error:       { ring: "ring-status-error",  iconBg: "bg-status-error/15",  iconFg: "text-status-error",  badge: "bg-status-error/15 text-status-error", shape: "rounded-lg", width: 280 },
  // ── Extra (extra-nodes.ts) ──────────────────────────────────────
  skill:       { ring: "ring-kind-skill",    iconBg: "bg-kind-skill/15",    iconFg: "text-kind-skill",    badge: "bg-kind-skill/15 text-kind-skill",    shape: "rounded-lg", width: 200 },
  palette:     { ring: "ring-kind-skill",    iconBg: "bg-kind-skill/15",    iconFg: "text-kind-skill",    badge: "bg-kind-skill/15 text-kind-skill",    shape: "rounded-xl", width: 240 },
  workspace:   { ring: "ring-kind-subagent", iconBg: "bg-kind-subagent/15", iconFg: "text-kind-subagent", badge: "bg-kind-subagent/15 text-kind-subagent", shape: "rounded-lg", width: 220 },
  behavior:    { ring: "ring-kind-hook",     iconBg: "bg-kind-hook/15",     iconFg: "text-kind-hook",     badge: "bg-kind-hook/15 text-kind-hook",      shape: "rounded-lg", width: 220 },
  widgets:     { ring: "ring-kind-subagent", iconBg: "bg-kind-subagent/15", iconFg: "text-kind-subagent", badge: "bg-kind-subagent/15 text-kind-subagent", shape: "rounded-lg", width: 220 },
  preview:     { ring: "ring-kind-module",   iconBg: "bg-kind-module/15",   iconFg: "text-kind-module",   badge: "bg-kind-module/15 text-kind-module",   shape: "rounded-lg", width: 220 },
  capabilities:{ ring: "ring-status-ok",     iconBg: "bg-status-ok/15",     iconFg: "text-status-ok",     badge: "bg-status-ok/15 text-status-ok",      shape: "rounded-lg", width: 220 },
  variables:   { ring: "ring-kind-io",       iconBg: "bg-kind-io/15",       iconFg: "text-kind-io",       badge: "bg-kind-io/15 text-kind-io",       shape: "rounded-lg", width: 200 },
  middleware:  { ring: "ring-kind-subagent", iconBg: "bg-kind-subagent/15", iconFg: "text-kind-subagent", badge: "bg-kind-subagent/15 text-kind-subagent", shape: "rounded-lg", width: 220 },
  // ── Flow nodes (flow-nodes.ts, flow: block) ─────────────────────
  // The actual hue per flow node type is applied via `data.color`
  // override in flow-nodes.ts; the theme entry here gives ring/badge
  // tokens that match the agent lane.
  flow_node:   { ring: "ring-kind-agent",    iconBg: "bg-kind-agent/15",    iconFg: "text-kind-agent",    badge: "bg-kind-agent/15 text-kind-agent", shape: "rounded-xl", width: 240 },
  // ── New extra kinds (theme/features/mcp_server) added by recent
  // schema work. Reuse existing tone tokens until each gets a real
  // colour pass — the daemon doesn't care about UI colour, but the
  // type-system does and crashes the build without these entries.
  theme:       { ring: "ring-kind-app",      iconBg: "bg-kind-app/15",      iconFg: "text-kind-app",      badge: "bg-kind-app/15 text-kind-app",     shape: "rounded-lg", width: 200 },
  features:    { ring: "ring-kind-app",      iconBg: "bg-kind-app/15",      iconFg: "text-kind-app",      badge: "bg-kind-app/15 text-kind-app",     shape: "rounded-lg", width: 200 },
  mcp_server:  { ring: "ring-kind-module",   iconBg: "bg-kind-module/15",   iconFg: "text-kind-module",   badge: "bg-kind-module/15 text-kind-module", shape: "rounded-lg", width: 220 },
  credentials:         { ring: "ring-kind-trigger",  iconBg: "bg-kind-trigger/15",  iconFg: "text-kind-trigger",  badge: "bg-kind-trigger/15 text-kind-trigger", shape: "rounded-lg", width: 220 },
  credential_provider: { ring: "ring-kind-trigger",  iconBg: "bg-kind-trigger/15",  iconFg: "text-kind-trigger",  badge: "bg-kind-trigger/15 text-kind-trigger", shape: "rounded-lg", width: 200 },
  fallback_brain:      { ring: "ring-kind-agent",    iconBg: "bg-kind-agent/15",    iconFg: "text-kind-agent",    badge: "bg-kind-agent/15 text-kind-agent",     shape: "rounded-xl", width: 220 },
  approval_gate:       { ring: "ring-kind-trigger",  iconBg: "bg-kind-trigger/15",  iconFg: "text-kind-trigger",  badge: "bg-kind-trigger/15 text-kind-trigger", shape: "rounded-lg", width: 200 },
  sandbox:             { ring: "ring-status-warn",   iconBg: "bg-status-warn/15",   iconFg: "text-status-warn",   badge: "bg-status-warn/15 text-status-warn",   shape: "rounded-2xl", width: 280 },
  pipeline_step:       { ring: "ring-kind-app",      iconBg: "bg-kind-app/15",      iconFg: "text-kind-app",      badge: "bg-kind-app/15 text-kind-app",         shape: "rounded-xl", width: 220 },
  // dev.include — fragment imports node. Reuse the app tone since
  // it's metadata about how the app is composed, not a runtime stage.
  include:             { ring: "ring-kind-app",      iconBg: "bg-kind-app/15",      iconFg: "text-kind-app",      badge: "bg-kind-app/15 text-kind-app",         shape: "rounded-lg", width: 220 },
};

/* ─────────────────────────────────────────────────────────────────
   Status pill — derived from data.status (idle/running/ok/warn/error)
   ─────────────────────────────────────────────────────────────── */

function StatusPill({ status }: { status?: string }) {
  if (!status) return null;
  const map: Record<string, { dot: string; label: string }> = {
    running: { dot: "bg-status-running animate-pulse", label: "running" },
    ok: { dot: "bg-status-ok", label: "ready" },
    warn: { dot: "bg-status-warn", label: "warn" },
    error: { dot: "bg-status-error", label: "error" },
    idle: { dot: "bg-status-idle", label: "idle" },
  };
  const e = map[status];
  if (!e) return null;
  return (
    <span className="inline-flex items-center gap-1 text-[10px] font-medium text-ink-muted">
      <span className={clsx("w-1.5 h-1.5 rounded-full", e.dot)} />
      {e.label}
    </span>
  );
}

function BrainChip({ label }: { label?: string }) {
  if (!label) return null;
  return (
    <span className="inline-flex items-center gap-1 px-1.5 py-0.5 rounded text-[10px] font-mono bg-surface-3/60 text-ink-muted border border-border-subtle">
      <Brain className="w-2.5 h-2.5" />
      {label}
    </span>
  );
}

/* ─────────────────────────────────────────────────────────────────
   Component
   ─────────────────────────────────────────────────────────────── */

interface ExtraProps {
  data: (NodeData | ExtraNodeData) & {
    status?: string;
    brainLabel?: string;
    toolCount?: number;
    isParent?: boolean;
    promptPreview?: string;
    modeLabel?: string;
    restrictedModules?: Array<{ module: string; actions: string[] }>;
    validation?: "error" | "warn" | "info";
    beginnerLabel?: string;
    dimmed?: boolean;
    approveActions?: string[];
    density?: "comfortable" | "compact" | "list";
    fallbackLabel?: string;
    poolFanOut?: number;
    contextLabel?: string;
    isDirect?: boolean;
  };
  selected?: boolean;
}

/* ─────────────────────────────────────────────────────────────────
   Compact + List density variants — 4× more nodes fit in a viewport.
   Compact = icon + label, ~140px wide.
   List    = full lane row, single line: icon + label + 1-line meta.
   ─────────────────────────────────────────────────────────────── */

interface VariantProps {
  data: ExtraProps["data"];
  kind: AnyKind;
  Icon: typeof Box;
  theme: ThemeEntry;
  selected?: boolean;
}

/**
 * Tier-3 LOD: at zoom < 0.4 the card text is unreadable anyway, so we
 * render a colored dot + tiny icon. Critical at scale -- 500 nodes
 * in dot-mode is still browsable; 500 nodes in compact mode kills the
 * frame rate and saturates visually.
 */
function DotNode({ kind, Icon, theme, data, selected }: VariantProps) {
  return (
    <div
      className={clsx(
        "relative flex items-center justify-center rounded-full border",
        "transition-all duration-150 cursor-pointer",
        theme.iconBg,
        selected ? ["ring-2 ring-offset-1 ring-offset-surface-0", theme.ring] : "border-border-subtle",
        data.dimmed && "opacity-25",
      )}
      style={{ width: 18, height: 18 }}
      title={`${data.label}  ·  ${kind}`}
    >
      <Icon className={clsx("w-2.5 h-2.5", theme.iconFg)} strokeWidth={2.5} />
      {data.validation && (
        <span
          className={clsx(
            "absolute -top-0.5 -right-0.5 w-1.5 h-1.5 rounded-full ring-1 ring-surface-0",
            data.validation === "error" && "bg-status-error",
            data.validation === "warn"  && "bg-status-warn",
            data.validation === "info"  && "bg-status-running",
          )}
        />
      )}
      <Handle type="target" position={Position.Left}  className="!w-1 !h-1 !bg-transparent !border-0" />
      <Handle type="source" position={Position.Right} className="!w-1 !h-1 !bg-transparent !border-0" />
    </div>
  );
}

function CompactNode({ data, kind, Icon, theme, selected }: VariantProps) {
  const isHook = kind === "hook";
  return (
    <div
      className={clsx(
        "group relative bg-surface-1 border border-border-subtle rounded-md",
        "transition-all duration-150 cursor-pointer",
        "hover:bg-surface-2 hover:border-border",
        selected && ["ring-2", theme.ring],
        isHook && "border-l-[2px] border-l-kind-hook",
        data.dimmed && "opacity-25",
      )}
      style={{ width: 140 }}
    >
      {data.validation && (
        <span
          className={clsx(
            "absolute -top-1 -left-1 w-2.5 h-2.5 rounded-full ring-2 ring-surface-0",
            data.validation === "error" && "bg-status-error",
            data.validation === "warn" && "bg-status-warn",
            data.validation === "info" && "bg-status-running",
          )}
        />
      )}
      <div className="flex items-center gap-2 px-2 py-1.5">
        <div className={clsx("flex-shrink-0 w-6 h-6 flex items-center justify-center rounded", theme.iconBg)}>
          <Icon className={clsx("w-3 h-3", theme.iconFg)} strokeWidth={2.5} />
        </div>
        <div className="flex-1 min-w-0">
          <div className="text-[11px] font-semibold text-ink truncate">
            {data.label}
          </div>
        </div>
        {data.approveActions && data.approveActions.length > 0 && (
          <span className="text-[10px]" title={`${data.approveActions.length} need${data.approveActions.length === 1 ? "s" : ""} approval`}>
            🔒
          </span>
        )}
      </div>
      <Handle type="target" position={Position.Left} className="!w-1.5 !h-1.5" />
      <Handle type="source" position={Position.Right} className="!w-1.5 !h-1.5" />
    </div>
  );
}

function ListNode({ data, kind, Icon, theme, selected }: VariantProps) {
  const isHook = kind === "hook";
  return (
    <div
      className={clsx(
        "group relative bg-surface-1 border border-border-subtle rounded",
        "transition-colors duration-150 cursor-pointer",
        "hover:bg-surface-2",
        selected && ["ring-1", theme.ring],
        isHook && "border-l-[2px] border-l-kind-hook",
        data.dimmed && "opacity-25",
      )}
      style={{ width: 320, height: 28 }}
    >
      {data.validation && (
        <span
          className={clsx(
            "absolute -top-1 -left-1 w-2 h-2 rounded-full ring-2 ring-surface-0",
            data.validation === "error" && "bg-status-error",
            data.validation === "warn" && "bg-status-warn",
            data.validation === "info" && "bg-status-running",
          )}
        />
      )}
      <div className="flex items-center gap-2 px-2 h-full">
        <div className={clsx("flex-shrink-0 w-4 h-4 flex items-center justify-center rounded", theme.iconBg)}>
          <Icon className={clsx("w-2.5 h-2.5", theme.iconFg)} strokeWidth={2.5} />
        </div>
        <span className="text-[11px] font-medium text-ink truncate">{data.label}</span>
        <span className={clsx("text-[9px] uppercase font-mono px-1 rounded", theme.badge)}>
          {kind}
        </span>
        {data.subtitle && (
          <span className="text-[10px] text-ink-muted truncate flex-1">{data.subtitle}</span>
        )}
        {data.approveActions && data.approveActions.length > 0 && (
          <span className="text-[10px]" title={`${data.approveActions.length} need${data.approveActions.length === 1 ? "s" : ""} approval`}>
            🔒
          </span>
        )}
      </div>
      <Handle type="target" position={Position.Left} className="!w-1.5 !h-1.5" />
      <Handle type="source" position={Position.Right} className="!w-1.5 !h-1.5" />
    </div>
  );
}

// Selector pulls ONLY the zoom scalar so the component re-renders only
// when zoom crosses, never on pan. Industry pattern from
// https://reactflow.dev/examples/interaction/contextual-zoom
const zoomSelector = (s: ReactFlowState) => s.transform[2];

function Node({ data, selected }: ExtraProps) {
  const kind = data.kind as AnyKind;
  const Icon = pickIcon(kind, data.icon);
  const theme = KIND_THEME[kind] ?? KIND_THEME.module;
  const isHook = kind === "hook";
  const density = data.density ?? "comfortable";
  const zoom = useStore(zoomSelector);

  // ── Zoom-aware LOD ─────────────────────────────────────────────
  // Three tiers: dot (z<0.4), compact (z<0.85), full (z>=0.85).
  // The user's explicit density choice is treated as an UPPER BOUND --
  // picking "list" forces list, picking "comfortable" allows the LOD
  // to pick a smaller variant when zoomed out.
  if (density === "list") {
    return <ListNode data={data} kind={kind} Icon={Icon} theme={theme} selected={selected} />;
  }
  if (zoom < 0.4) {
    return <DotNode data={data} kind={kind} Icon={Icon} theme={theme} selected={selected} />;
  }
  if (density === "compact" || zoom < 0.85) {
    return <CompactNode data={data} kind={kind} Icon={Icon} theme={theme} selected={selected} />;
  }

  // Visual cap: the layout books a per-kind height (160 for agents,
  // 108 for modules, 72 for hooks/skills). We mirror that cap on the
  // DOM card with overflow-hidden + a subtle bottom fade, so a card
  // OVERFULL with badges never bleeds into the row below it. The
  // user can still see everything in the Inspector / hover-expand.
  const maxH =
    kind === "agent" || kind === "app" ? 160
    : kind === "hook" || kind === "skill" ? 78
    : 108;

  return (
    <div
      className={clsx(
        "group relative bg-surface-1 border border-border-subtle",
        theme.shape,
        "transition-all duration-150 cursor-pointer",
        "hover:bg-surface-2 hover:border-border hover:shadow-node-hover hover:-translate-y-px hover:max-h-none hover:z-10",
        "shadow-node overflow-hidden",
        selected && [
          "shadow-node-active ring-2 ring-offset-2 ring-offset-surface-0",
          theme.ring,
          "max-h-none z-10",
        ],
        isHook && "border-l-[3px] border-l-kind-hook",
        data.dimmed && "opacity-25",
      )}
      style={{ width: theme.width, maxHeight: maxH }}
    >
      {isHook && (
        <span
          className="absolute -left-2 top-1/2 -translate-y-1/2 w-3 h-3 rotate-45 bg-kind-hook border border-surface-0"
          aria-hidden
        />
      )}
      {data.status && data.status !== "idle" && (
        <span
          className={clsx(
            "absolute top-2 right-2 w-2 h-2 rounded-full",
            data.status === "running" && "bg-status-running animate-pulse",
            data.status === "ok" && "bg-status-ok",
            data.status === "warn" && "bg-status-warn",
            data.status === "error" && "bg-status-error",
          )}
        />
      )}
      {data.validation && (
        <span
          className={clsx(
            "absolute -top-1 -left-1 w-3 h-3 rounded-full ring-2 ring-surface-0",
            data.validation === "error" && "bg-status-error",
            data.validation === "warn" && "bg-status-warn",
            data.validation === "info" && "bg-status-running",
          )}
          title={
            data.validation === "error" ? "Configuration error — open the Validation panel"
            : data.validation === "warn" ? "Risk or inconsistency — open the Validation panel"
            : "Hint — open the Validation panel"
          }
        />
      )}

      <div className="flex items-start gap-3 p-3">
        <div className={clsx("flex-shrink-0 w-9 h-9 flex items-center justify-center rounded-lg", theme.iconBg)}>
          <Icon className={clsx("w-4 h-4", theme.iconFg)} strokeWidth={2} />
        </div>

        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2 min-w-0">
            <span className="text-sm font-semibold text-ink truncate">
              {data.label}
            </span>
            <span
              className={clsx(
                "px-1.5 py-0.5 rounded text-[9px] font-medium uppercase tracking-wider flex-shrink-0",
                theme.badge,
              )}
            >
              {kind}
            </span>
          </div>

          {data.beginnerLabel ? (
            <div className="text-[11px] text-accent/90 italic truncate mt-0.5">
              {data.beginnerLabel}
            </div>
          ) : data.subtitle ? (
            <div className="text-xs text-ink-muted truncate mt-0.5">
              {data.subtitle}
            </div>
          ) : null}

          {(data.brainLabel || data.modeLabel || (data.toolCount !== undefined && data.toolCount > 0) || (data.actionsCount !== undefined && data.actionsCount > 0) || data.restrictedModules) && (
            <div className="flex flex-wrap items-center gap-1 mt-2">
              {data.modeLabel && (
                <span className="inline-flex items-center gap-1 px-1.5 py-0.5 rounded text-[10px] font-mono bg-kind-app/15 text-kind-app border border-kind-app/30">
                  {data.modeLabel}
                </span>
              )}
              <BrainChip label={data.brainLabel} />
              {data.fallbackLabel && (
                <span
                  className="inline-flex items-center gap-1 px-1.5 py-0.5 rounded text-[10px] font-mono bg-status-warn/10 text-status-warn border border-status-warn/30"
                  title={`Fallback brain on billing/rate-limit errors: ${data.fallbackLabel}`}
                >
                  ↩ fallback
                </span>
              )}
              {data.poolFanOut && data.poolFanOut > 1 && (
                <span
                  className="inline-flex items-center gap-1 px-1.5 py-0.5 rounded text-[10px] font-mono bg-kind-agent/15 text-kind-agent border border-kind-agent/30"
                  title={`This coordinator can fan-out up to ${data.poolFanOut} parallel sub-agents`}
                >
                  ⇉ pool {data.poolFanOut}
                </span>
              )}
              {data.contextLabel && (
                <span
                  className="inline-flex items-center gap-1 px-1.5 py-0.5 rounded text-[10px] font-mono bg-kind-app/10 text-kind-app border border-kind-app/30"
                  title={`Brain context window: ${data.contextLabel} tokens`}
                >
                  ctx {data.contextLabel}
                </span>
              )}
              {data.isDirect && (
                <span
                  className="inline-flex items-center gap-1 px-1.5 py-0.5 rounded text-[10px] font-mono bg-status-ok/15 text-status-ok border border-status-ok/30"
                  title="This module's tools are exposed DIRECTLY to the LLM (in runtime.direct_modules) — the agent sees them as native tool calls."
                >
                  ⚡ direct
                </span>
              )}
              {data.toolCount !== undefined && data.toolCount > 0 && (
                <span className="inline-flex items-center gap-1 px-1.5 py-0.5 rounded text-[10px] font-mono bg-surface-3/60 text-ink-muted border border-border-subtle">
                  <Wrench className="w-2.5 h-2.5" />
                  {data.toolCount} tools
                </span>
              )}
              {data.actionsCount !== undefined && data.actionsCount > 0 && (
                <span className="inline-flex items-center gap-1 px-1.5 py-0.5 rounded text-[10px] font-mono bg-surface-3/60 text-ink-muted border border-border-subtle">
                  {data.actionsCount} actions
                </span>
              )}
              {data.restrictedModules && data.restrictedModules.length > 0 && (
                <span
                  className="inline-flex items-center gap-1 px-1.5 py-0.5 rounded text-[10px] font-mono bg-status-warn/15 text-status-warn border border-status-warn/30"
                  title={data.restrictedModules.map(m => `${m.module}${m.actions.length ? `: [${m.actions.join(", ")}]` : ""}`).join(" · ")}
                >
                  <Shield className="w-2.5 h-2.5" />
                  {data.restrictedModules.length} mod{data.restrictedModules.length > 1 ? "s" : ""} only
                </span>
              )}
              {data.approveActions && data.approveActions.length > 0 && (
                <span
                  className="inline-flex items-center gap-1 px-1.5 py-0.5 rounded text-[10px] font-mono bg-status-warn/20 text-status-warn border border-status-warn/40"
                  title={`Requires user approval before running: ${data.approveActions.join(", ")}. The daemon blocks the loop on ApprovalQueue until the user clicks approve.`}
                >
                  🔒 {data.approveActions.length} need{data.approveActions.length === 1 ? "s" : ""} approval
                </span>
              )}
              <StatusPill status={data.status} />
            </div>
          )}
        </div>
      </div>

      {data.promptPreview && (
        <div className="px-3 pb-2.5 pt-0">
          <div
            className="text-[11px] text-ink-dim italic truncate border-l-2 border-border-subtle pl-2"
            title={data.promptPreview}
          >
            "{data.promptPreview}"
          </div>
        </div>
      )}

      <Handle type="target" position={Position.Left} className="!w-2 !h-2" />
      <Handle type="source" position={Position.Right} className="!w-2 !h-2" />
    </div>
  );
}

export default memo(Node);
