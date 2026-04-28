import { useState, useMemo, useEffect } from "react";
import {
  X, FileText, Settings, Code, Brain, Wrench, Webhook, Zap, Mail,
  ChevronRight, Copy, Check, Eye, Network, Sparkles,
} from "lucide-react";
import clsx from "clsx";
import yaml from "js-yaml";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { useFile, readSession } from "@digitorn/preview-sdk";
import { getFixtureFile } from "../lib/fixtures";
import type { NodeData, ParsedYaml } from "../lib/yaml-to-graph";
import { summarizeAgent } from "../lib/summarize-agent";
import { summarizeNode } from "../lib/summarize";
import AgentBehaviorCard from "./AgentBehaviorCard";
import AgentToolsTab from "./AgentToolsTab";
import OverviewCard from "./OverviewCard";
import HookCard from "./HookCard";
import { describeHook } from "../lib/describe-hook";

type Section =
  | "overview"
  | "config"
  | "prompt"
  | "tools"
  | "hooks"
  | "deps"
  | "yaml";

interface DepsInfo {
  uses: { id: string; label: string; via?: string }[];
  usedBy: { id: string; label: string; via?: string }[];
}

interface Props {
  data: NodeData | null;
  deps?: DepsInfo;
  doc?: ParsedYaml | null;
  onSelectNode?: (id: string) => void;
  onClose: () => void;
}

const SECTION_META: Record<Section, { label: string; icon: typeof FileText; group: "core" | "audit" }> = {
  overview: { label: "Overview", icon: Sparkles, group: "core" },
  config: { label: "Configuration", icon: Settings, group: "core" },
  prompt: { label: "Prompt", icon: FileText, group: "core" },
  tools: { label: "Tools", icon: Wrench, group: "audit" },
  hooks: { label: "Hooks", icon: Webhook, group: "audit" },
  deps: { label: "Dependencies", icon: Network, group: "audit" },
  yaml: { label: "YAML", icon: Code, group: "audit" },
};

export default function Inspector({ data: rawData, deps, doc, onSelectNode, onClose }: Props) {
  const [section, setSection] = useState<Section>("overview");
  // For skill click-through: when user clicks a skill's file path, we
  // override the prompt-section path with the skill's .md.
  const [externalFilePath, setExternalFilePath] = useState<string | null>(null);
  // Palette drilldown: when a user opens the "Command Palette" card, we
  // show the list and let them click a row to inspect a single skill.
  // The drilled skill shadows `data` so all panes (overview, prompt,
  // YAML) render as if that skill had been selected directly.
  const [drilledSkillIndex, setDrilledSkillIndex] = useState<number | null>(null);

  const isPalette = (rawData?.kind as string | undefined) === "palette";
  const skillsList: Array<{ command: string; description?: string; path?: string }> = useMemo(() => {
    if (!isPalette) return [];
    const r = rawData?.raw;
    return Array.isArray(r) ? (r as Array<{ command: string; description?: string; path?: string }>) : [];
  }, [isPalette, rawData?.raw]);

  // Effective node data — when the user has drilled into a specific
  // skill, swap in a synthesized skill NodeData so the rest of the
  // Inspector reuses its existing per-kind rendering.
  const data: NodeData | null = useMemo(() => {
    if (isPalette && drilledSkillIndex != null && skillsList[drilledSkillIndex]) {
      const s = skillsList[drilledSkillIndex];
      const synth = {
        kind: "skill",
        label: s.command,
        subtitle: s.description ?? s.path ?? "skill",
        icon: "code",
        color: "rgb(16, 185, 129)",
        raw: s,
      };
      return synth as unknown as NodeData;
    }
    return rawData;
  }, [isPalette, drilledSkillIndex, skillsList, rawData]);

  // Reset section + external file + drilldown when the SOURCE node changes.
  useEffect(() => {
    setExternalFilePath(null);
    setDrilledSkillIndex(null);
    setSection(defaultSection(rawData));
  }, [rawData?.label, rawData?.kind]);

  // Reset section when drilling in/out so the user lands on Overview.
  useEffect(() => {
    setExternalFilePath(null);
    setSection(defaultSection(data));
  }, [drilledSkillIndex]);

  // ESC: pop a drilldown if any, otherwise close the drawer.
  useEffect(() => {
    if (!rawData) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        const tag = (e.target as HTMLElement | null)?.tagName?.toLowerCase();
        if (tag === "input" || tag === "textarea") return;
        if (isPalette && drilledSkillIndex != null) {
          setDrilledSkillIndex(null);
        } else {
          onClose();
        }
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [rawData, onClose, isPalette, drilledSkillIndex]);

  if (!data) return null;

  const promptPath = extractPromptPath(data);
  const dataKind = data.kind as string;
  const isSkillOrFileBacked = dataKind === "skill" && (data.raw as { path?: string } | undefined)?.path;
  const isAgent = dataKind === "agent";

  const agentProfile = useMemo(() => {
    if (!isAgent) return null;
    return summarizeAgent((data.raw ?? {}) as Record<string, unknown>, doc ?? null);
  }, [isAgent, data.raw, doc]);

  const genericOverview = useMemo(
    () => (isAgent ? null : summarizeNode(data, doc ?? null)),
    [isAgent, data, doc],
  );

  const hasOverview = Boolean(agentProfile || genericOverview);

  // Compute which sections are available for this node kind.
  const sections: Section[] = ["config"];
  if (hasOverview) sections.unshift("overview");
  if (promptPath || isAgent || isSkillOrFileBacked) sections.push("prompt");
  if (isAgent) sections.push("tools");
  if (isAgent && agentProfile && agentProfile.affectingHooks.length > 0) sections.push("hooks");
  if (deps && (deps.uses.length > 0 || deps.usedBy.length > 0)) sections.push("deps");
  sections.push("yaml");

  const safeSection: Section = sections.includes(section) ? section : sections[0];

  return (
    <aside
      className={clsx(
        "w-[600px] flex-shrink-0 border-l border-border-subtle bg-surface-1",
        "flex flex-row h-full overflow-hidden",
        "animate-in slide-in-from-right",
      )}
    >
      {/* Section nav (left column inside the drawer) */}
      <nav className="w-[160px] flex-shrink-0 bg-surface-0/40 border-r border-border-subtle flex flex-col py-3">
        {/* Header (small): kind badge */}
        <div className="px-3 mb-3 flex items-center gap-2">
          <KindBadge kind={dataKind} />
          <div className="min-w-0">
            <div className="text-[9px] uppercase tracking-wider text-ink-dim">{dataKind}</div>
            <div className="text-xs font-semibold text-ink truncate" title={data.label}>
              {data.label}
            </div>
          </div>
        </div>

        <SectionGroup
          title="Core"
          items={sections.filter((s) => SECTION_META[s].group === "core")}
          active={safeSection}
          onSelect={setSection}
        />
        <SectionGroup
          title="Audit"
          items={sections.filter((s) => SECTION_META[s].group === "audit")}
          active={safeSection}
          onSelect={setSection}
        />

        <div className="flex-1" />

        <button
          onClick={onClose}
          className="mx-2 mb-2 mt-2 inline-flex items-center justify-center gap-1.5 h-8 rounded-lg bg-surface-2 hover:bg-surface-3 text-ink-muted hover:text-ink text-xs"
          title="Close (Esc)"
        >
          <X className="w-3.5 h-3.5" />
          Close
        </button>
      </nav>

      {/* Content area */}
      <div className="flex-1 min-w-0 overflow-y-auto">
        {/* Top header strip */}
        <header className="sticky top-0 z-10 flex items-center gap-3 px-4 py-3 border-b border-border-subtle bg-surface-1/95 backdrop-blur-sm">
          <SectionTitleIcon section={safeSection} />
          <div className="min-w-0 flex-1">
            <div className="text-sm font-semibold text-ink truncate">
              {SECTION_META[safeSection].label}
            </div>
            {data.subtitle && (
              <div className="text-[11px] text-ink-muted truncate">
                {data.subtitle}
              </div>
            )}
          </div>
        </header>

        {safeSection === "overview" && agentProfile && (
          <AgentBehaviorCard profile={agentProfile} />
        )}
        {safeSection === "overview" && !agentProfile && genericOverview && (
          <OverviewCard profile={genericOverview} />
        )}
        {/* Palette top-level: show the list of skills under the overview,
            with a click-to-drill-down. When drilled in, the synthetic
            skill data already replaces `data` upstream so the standard
            skill panes render. */}
        {isPalette && drilledSkillIndex == null && safeSection === "overview" && (
          <PaletteList
            skills={skillsList}
            onSelect={(i) => setDrilledSkillIndex(i)}
          />
        )}
        {isPalette && drilledSkillIndex != null && (
          <div className="px-4 pt-3">
            <button
              onClick={() => setDrilledSkillIndex(null)}
              className="inline-flex items-center gap-1.5 text-[11px] text-ink-muted hover:text-ink"
            >
              <ChevronRight className="w-3 h-3 rotate-180" />
              Back to Command Palette
            </button>
          </div>
        )}
        {safeSection === "config" && (
          <ConfigTab
            data={data}
            onOpenPrompt={() => setSection("prompt")}
            onOpenSkill={(path) => {
              setExternalFilePath(path);
              setSection("prompt");
            }}
          />
        )}
        {safeSection === "prompt" && (
          <PromptTab data={data} promptPath={externalFilePath ?? promptPath} />
        )}
        {safeSection === "tools" && agentProfile && (
          <AgentToolsTab profile={agentProfile} />
        )}
        {safeSection === "hooks" && agentProfile && (
          <HooksSection hooks={agentProfile.affectingHooks} doc={doc} />
        )}
        {safeSection === "deps" && deps && (
          <DepsPanel deps={deps} onSelect={onSelectNode} />
        )}
        {safeSection === "yaml" && <YamlTab data={data} />}
      </div>
    </aside>
  );
}

function defaultSection(data: NodeData | null): Section {
  if (!data) return "overview";
  // Default to Overview for kinds that have a summarizer; everything else
  // jumps straight to Configuration.
  const HAS_OVERVIEW = new Set([
    "agent", "module", "skill", "palette", "hook", "trigger", "channel", "app",
    "capabilities", "workspace", "behavior", "widgets", "preview",
    "variables", "middleware",
  ]);
  if (HAS_OVERVIEW.has(data.kind as string)) return "overview";
  return "config";
}

function SectionGroup({
  title,
  items,
  active,
  onSelect,
}: {
  title: string;
  items: Section[];
  active: Section;
  onSelect: (s: Section) => void;
}) {
  if (items.length === 0) return null;
  return (
    <div className="px-2 mb-3">
      <div className="px-2 text-[9px] uppercase tracking-wider text-ink-dim font-medium mb-1">
        {title}
      </div>
      <div className="space-y-0.5">
        {items.map((s) => {
          const meta = SECTION_META[s];
          const Icon = meta.icon;
          const selected = active === s;
          return (
            <button
              key={s}
              onClick={() => onSelect(s)}
              className={clsx(
                "w-full inline-flex items-center gap-2 h-8 px-2 rounded-md text-xs transition-colors",
                selected
                  ? "bg-accent/15 text-accent font-medium"
                  : "text-ink-muted hover:bg-surface-2 hover:text-ink",
              )}
            >
              <Icon className="w-3.5 h-3.5 flex-shrink-0" />
              <span className="truncate">{meta.label}</span>
            </button>
          );
        })}
      </div>
    </div>
  );
}

function SectionTitleIcon({ section }: { section: Section }) {
  const Icon = SECTION_META[section].icon;
  return (
    <div className="flex-shrink-0 w-8 h-8 flex items-center justify-center rounded-lg bg-accent/15 text-accent">
      <Icon className="w-4 h-4" />
    </div>
  );
}

function PaletteList({
  skills,
  onSelect,
}: {
  skills: Array<{ command: string; description?: string; path?: string }>;
  onSelect: (index: number) => void;
}) {
  if (skills.length === 0) {
    return (
      <div className="p-4 text-xs text-ink-dim italic">
        No skills declared in this app.
      </div>
    );
  }
  return (
    <div className="px-4 pb-4">
      <div className="text-[10px] uppercase tracking-wider text-ink-dim font-semibold mb-2">
        Slash commands · click to inspect
      </div>
      <div className="space-y-1">
        {skills.map((s, i) => (
          <button
            key={s.command + i}
            onClick={() => onSelect(i)}
            className="group w-full flex items-start gap-3 p-2.5 rounded-lg bg-surface-2/40 hover:bg-surface-2 border border-border-subtle hover:border-border transition-colors text-left"
          >
            <div className="w-7 h-7 flex-shrink-0 rounded-md bg-kind-skill/15 text-kind-skill flex items-center justify-center">
              <FileText className="w-3.5 h-3.5" />
            </div>
            <div className="flex-1 min-w-0">
              <div className="text-xs font-mono text-ink font-semibold truncate">
                {s.command}
              </div>
              {s.description && (
                <div className="text-[11px] text-ink-muted truncate">
                  {s.description}
                </div>
              )}
              {s.path && (
                <div className="text-[10px] text-ink-dim font-mono truncate mt-0.5">
                  {s.path.replace(/^\.\//, "")}
                </div>
              )}
            </div>
            <ChevronRight className="w-3.5 h-3.5 text-ink-dim group-hover:text-ink flex-shrink-0 mt-1.5" />
          </button>
        ))}
      </div>
    </div>
  );
}

function HooksSection({
  hooks,
  doc,
}: {
  hooks: { id: string; on: string; condition?: string; action: string }[];
  doc?: ParsedYaml | null;
}) {
  if (hooks.length === 0) {
    return <div className="p-4 text-xs text-ink-dim italic">No hooks affecting this node.</div>;
  }
  // Look up the raw hook block from `doc.execution.hooks` so we can render
  // a real HookCard (with parameters, conditions, effect) — the lighter
  // shape from `summarizeAgent` only carries display strings.
  const rawHooks = (doc?.execution?.hooks ?? []) as Array<Record<string, unknown>>;
  const findRaw = (id: string) =>
    rawHooks.find((h) => (h.id as string | undefined) === id);
  return (
    <div className="p-4 space-y-3">
      <div className="text-[10px] uppercase tracking-wider text-ink-dim font-medium">
        {hooks.length} hook{hooks.length > 1 ? "s" : ""} may fire on this node's events
      </div>
      {hooks.map((h) => {
        const raw = findRaw(h.id) ?? { id: h.id, event: h.on, condition: h.condition, action: h.action };
        const flow = describeHook(raw as Record<string, unknown>);
        return <HookCard key={h.id} flow={flow} compact />;
      })}
    </div>
  );
}

/* ─────────────────────────────────────────────────────────────────
   Kind badge (left of nav header)
   ─────────────────────────────────────────────────────────────── */

const KIND_TINT: Record<string, string> = {
  app: "bg-kind-app/15 text-kind-app",
  agent: "bg-kind-agent/15 text-kind-agent",
  module: "bg-kind-module/15 text-kind-module",
  hook: "bg-kind-hook/15 text-kind-hook",
  trigger: "bg-kind-trigger/15 text-kind-trigger",
  channel: "bg-kind-channel/15 text-kind-channel",
  user: "bg-kind-io/15 text-kind-io",
  input: "bg-kind-io/15 text-kind-io",
  output: "bg-kind-io/15 text-kind-io",
  variable: "bg-kind-io/15 text-kind-io",
  skill: "bg-kind-skill/15 text-kind-skill",
  workspace: "bg-kind-subagent/15 text-kind-subagent",
  behavior: "bg-kind-hook/15 text-kind-hook",
  widgets: "bg-kind-subagent/15 text-kind-subagent",
  capabilities: "bg-status-ok/15 text-status-ok",
  preview: "bg-kind-module/15 text-kind-module",
  middleware: "bg-kind-subagent/15 text-kind-subagent",
  variables: "bg-kind-io/15 text-kind-io",
  error: "bg-status-error/15 text-status-error",
};

function KindBadge({ kind }: { kind: string }) {
  const tint = KIND_TINT[kind] ?? KIND_TINT.module;
  const Icon =
    kind === "agent" ? Brain :
    kind === "module" ? Wrench :
    kind === "hook" ? Webhook :
    kind === "trigger" ? Zap :
    kind === "channel" ? Mail :
    kind === "skill" ? FileText :
    kind === "preview" ? Eye :
    Settings;
  return (
    <div className={clsx("flex-shrink-0 w-7 h-7 rounded-lg flex items-center justify-center", tint)}>
      <Icon className="w-3.5 h-3.5" />
    </div>
  );
}

/* ─────────────────────────────────────────────────────────────────
   Config tab — kind-specific structured rendering
   ─────────────────────────────────────────────────────────────── */

function ConfigTab({ data, onOpenPrompt, onOpenSkill }: {
  data: NodeData;
  onOpenPrompt?: () => void;
  onOpenSkill?: (path: string) => void;
}) {
  const raw = (data.raw ?? {}) as Record<string, unknown>;

  if (data.kind === "agent") return <AgentConfig agent={raw} actionsCount={data.actionsCount} onOpenPrompt={onOpenPrompt} />;
  if (data.kind === "module") return <ModuleConfig raw={raw} actions={data.grantedActions} />;
  if (data.kind === "hook") return <HookConfig raw={raw} />;
  if (data.kind === "trigger") return <TriggerConfig raw={raw} />;
  if (data.kind === "channel") return <ChannelConfig raw={raw} />;
  if (data.kind === "app") return <AppConfig raw={raw} />;
  // Extra kinds
  const k = data.kind as string;
  if (k === "skill") return <SkillConfig raw={raw} onOpen={onOpenSkill} />;
  if (k === "workspace") return <WorkspaceConfig raw={raw} />;
  if (k === "behavior") return <BehaviorConfig raw={raw} />;
  if (k === "widgets") return <WidgetsConfig raw={raw} />;
  if (k === "capabilities") return <CapabilitiesConfig raw={raw} />;
  return <RawJson value={raw} />;
}

function SkillConfig({ raw, onOpen }: { raw: Record<string, unknown>; onOpen?: (path: string) => void }) {
  const path = raw.path as string | undefined;
  return (
    <div className="p-4 space-y-3">
      <KV label="Command" value={raw.command as string} mono />
      <KV label="Description" value={raw.description as string} />
      {path && (
        <Section title="File" icon={FileText}>
          <button
            onClick={() => onOpen?.(path.replace(/^\.\//, ""))}
            className="w-full text-left flex items-center gap-2 px-2.5 py-2 rounded-lg bg-surface-2 hover:bg-surface-3 border border-border-subtle text-xs text-ink-muted hover:text-ink transition-colors group"
          >
            <FileText className="w-3.5 h-3.5 text-accent flex-shrink-0" />
            <span className="font-mono truncate flex-1">{path}</span>
            <ChevronRight className="w-3.5 h-3.5 opacity-0 group-hover:opacity-100 transition-opacity" />
          </button>
        </Section>
      )}
    </div>
  );
}

function WorkspaceConfig({ raw }: { raw: Record<string, unknown> }) {
  return (
    <div className="p-4 space-y-3">
      <KV label="Render mode" value={raw.render_mode as string} mono />
      <KV label="Title" value={raw.title as string} />
      <KV label="Entry file" value={raw.entry_file as string} mono />
      {raw.sync_to_disk !== undefined && <KV label="Sync to disk" value={String(raw.sync_to_disk)} mono />}
      {raw.lint !== undefined && <KV label="Lint" value={String(raw.lint)} mono />}
    </div>
  );
}

function BehaviorConfig({ raw }: { raw: Record<string, unknown> }) {
  const customCount = Array.isArray(raw.custom) ? raw.custom.length : 0;
  const ruleCount = Array.isArray(raw.rule_definitions) ? raw.rule_definitions.length : 0;
  return (
    <div className="p-4 space-y-3">
      <KV label="Profile" value={raw.profile as string} mono />
      <KV label="Classify turns" value={raw.classify_turns !== undefined ? String(raw.classify_turns) : undefined} mono />
      {customCount > 0 && <KV label="Custom rules" value={String(customCount)} />}
      {ruleCount > 0 && <KV label="Rule definitions" value={String(ruleCount)} />}
      {raw.brain ? (
        <Section title="Classifier brain" icon={Brain}>
          <KV label="Provider" value={(raw.brain as Record<string, unknown>).provider as string} mono />
          <KV label="Model" value={(raw.brain as Record<string, unknown>).model as string} mono />
        </Section>
      ) : null}
    </div>
  );
}

function WidgetsConfig({ raw }: { raw: Record<string, unknown> }) {
  return (
    <div className="p-4 space-y-3">
      {Array.isArray(raw.chat_side) && <KV label="Chat sidebar" value={String(raw.chat_side.length)} />}
      {Array.isArray(raw.workspace_tabs) && <KV label="Workspace tabs" value={String(raw.workspace_tabs.length)} />}
      {Array.isArray(raw.modals) && <KV label="Modals" value={String(raw.modals.length)} />}
      {Array.isArray(raw.inline) && <KV label="Inline" value={String(raw.inline.length)} />}
    </div>
  );
}

function CapabilitiesConfig({ raw }: { raw: Record<string, unknown> }) {
  const grant = (raw.grant as unknown[] | undefined) ?? [];
  const approve = (raw.approve as unknown[] | undefined) ?? [];
  const deny = (raw.deny as unknown[] | undefined) ?? [];
  return (
    <div className="p-4 space-y-3">
      <KV label="Default policy" value={raw.default_policy as string} mono />
      <KV label="Max risk level" value={raw.max_risk_level as string} mono />
      {grant.length > 0 && (
        <Section title={`Grant (${grant.length})`} icon={Settings}>
          <RawJson value={grant} compact />
        </Section>
      )}
      {approve.length > 0 && (
        <Section title={`Approve (${approve.length})`} icon={Settings}>
          <RawJson value={approve} compact />
        </Section>
      )}
      {deny.length > 0 && (
        <Section title={`Deny (${deny.length})`} icon={Settings}>
          <RawJson value={deny} compact />
        </Section>
      )}
    </div>
  );
}

function AgentConfig({
  agent,
  actionsCount,
  onOpenPrompt,
}: {
  agent: Record<string, unknown>;
  actionsCount?: number;
  onOpenPrompt?: () => void;
}) {
  const brain = (agent.brain ?? {}) as Record<string, unknown>;
  const fallback = brain?.fallback as Record<string, unknown> | undefined;
  const ctx = brain?.context as Record<string, unknown> | undefined;
  const sysPrompt = agent.system_prompt as string | undefined;
  const mods = agent.modules as unknown[] | undefined;
  const restricted: Array<{ module: string; actions: string[] }> = [];
  if (Array.isArray(mods)) {
    for (const m of mods) {
      if (typeof m === "string") restricted.push({ module: m, actions: [] });
      else if (m && typeof m === "object") {
        for (const [k, v] of Object.entries(m as Record<string, unknown>)) {
          restricted.push({ module: k, actions: Array.isArray(v) ? (v as string[]) : [] });
        }
      }
    }
  }

  return (
    <div className="p-4 space-y-4">
      <KV label="ID" value={agent.id as string | undefined} mono />
      <KV label="Role" value={agent.role as string | undefined} />
      <KV label="Specialty" value={agent.specialty as string | undefined} />
      {actionsCount !== undefined && <KV label="Tools" value={String(actionsCount)} />}

      {sysPrompt && (
        <Section title="System Prompt" icon={FileText}>
          <button
            onClick={onOpenPrompt}
            className="w-full text-left flex items-center gap-2 px-2.5 py-2 rounded-lg bg-surface-2 hover:bg-surface-3 border border-border-subtle text-xs text-ink-muted hover:text-ink transition-colors group"
          >
            <FileText className="w-3.5 h-3.5 text-accent flex-shrink-0" />
            <span className="font-mono truncate flex-1">{sysPrompt}</span>
            <ChevronRight className="w-3.5 h-3.5 opacity-0 group-hover:opacity-100 transition-opacity" />
          </button>
        </Section>
      )}

      {Object.keys(brain).length > 0 && (
        <Section title="Brain" icon={Brain}>
          <KV label="Provider" value={brain.provider as string} mono />
          <KV label="Model" value={brain.model as string} mono />
          <KV label="Backend" value={brain.backend as string} mono />
          <KV label="Temperature" value={brain.temperature !== undefined ? String(brain.temperature) : undefined} mono />
          <KV label="Max tokens" value={brain.max_tokens !== undefined ? String(brain.max_tokens) : undefined} mono />
          {ctx && (
            <div className="ml-3 mt-2 pl-3 border-l border-border-subtle">
              <div className="text-[10px] uppercase tracking-wider text-ink-dim mb-1">Context</div>
              <KV label="Window" value={ctx.max_tokens !== undefined ? `${ctx.max_tokens} tokens` : undefined} mono />
              <KV label="Strategy" value={ctx.strategy as string} mono />
              <KV label="Keep recent" value={ctx.keep_recent !== undefined ? String(ctx.keep_recent) : undefined} mono />
            </div>
          )}
        </Section>
      )}

      {fallback && (
        <Section title="Fallback brain" icon={Brain}>
          <KV label="Provider" value={fallback.provider as string} mono />
          <KV label="Model" value={fallback.model as string} mono />
        </Section>
      )}

      {restricted.length > 0 && (
        <Section title={`Module access (${restricted.length} restricted)`} icon={Wrench}>
          <div className="space-y-1.5">
            {restricted.map((m) => (
              <div
                key={m.module}
                className="px-2.5 py-1.5 rounded-lg bg-surface-2 border border-border-subtle"
              >
                <div className="text-[11px] font-mono font-semibold text-kind-module">{m.module}</div>
                {m.actions.length > 0 ? (
                  <div className="flex flex-wrap gap-1 mt-1">
                    {m.actions.map((a) => (
                      <span
                        key={a}
                        className="px-1.5 py-0.5 rounded text-[10px] font-mono bg-surface-3 text-ink-muted border border-border-subtle"
                      >
                        {a}
                      </span>
                    ))}
                  </div>
                ) : (
                  <div className="text-[10px] text-ink-dim italic mt-0.5">all actions</div>
                )}
              </div>
            ))}
          </div>
        </Section>
      )}
    </div>
  );
}

function ModuleConfig({ raw, actions }: { raw: Record<string, unknown>; actions?: string[] }) {
  const config = raw.config as Record<string, unknown> | undefined;
  return (
    <div className="p-4 space-y-4">
      {actions && actions.length > 0 && (
        <Section title={`Granted actions (${actions.length})`} icon={Wrench}>
          <div className="flex flex-wrap gap-1">
            {actions.map((a) => (
              <span
                key={a}
                className="px-2 py-0.5 rounded bg-surface-2 border border-border-subtle text-[11px] font-mono text-ink-muted"
              >
                {a}
              </span>
            ))}
          </div>
        </Section>
      )}
      {config && Object.keys(config).length > 0 && (
        <Section title="Config" icon={Settings}>
          <RawJson value={config} compact />
        </Section>
      )}
      {!config && (!actions || actions.length === 0) && (
        <div className="text-xs text-ink-dim italic">Default config — no overrides.</div>
      )}
    </div>
  );
}

function HookConfig({ raw }: { raw: Record<string, unknown> }) {
  const flow = describeHook(raw);
  return (
    <div className="p-4">
      <HookCard flow={flow} />
    </div>
  );
}

function TriggerConfig({ raw }: { raw: Record<string, unknown> }) {
  return (
    <div className="p-4 space-y-3">
      <KV label="Type" value={raw.type as string} mono />
      <KV label="Schedule" value={raw.schedule as string} mono />
      <KV label="Path" value={raw.path as string} mono />
      <RawJson value={raw} compact />
    </div>
  );
}

function ChannelConfig({ raw }: { raw: Record<string, unknown> }) {
  return (
    <div className="p-4 space-y-3">
      <KV label="Type" value={raw.type as string} mono />
      <KV label="User resolver" value={raw.user_resolver as string} mono />
      <Section title="Config" icon={Settings}>
        <RawJson value={raw.config} compact />
      </Section>
    </div>
  );
}

function AppConfig({ raw }: { raw: Record<string, unknown> }) {
  return (
    <div className="p-4 space-y-3">
      <KV label="App ID" value={raw.app_id as string} mono />
      <KV label="Name" value={raw.name as string} />
      <KV label="Version" value={raw.version as string} mono />
      <KV label="Category" value={raw.category as string} />
      <KV label="Author" value={raw.author as string} />
      {Array.isArray(raw.tags) && raw.tags.length > 0 ? (
        <div>
          <div className="text-[10px] uppercase tracking-wider text-ink-dim mb-1.5">Tags</div>
          <div className="flex flex-wrap gap-1">
            {(raw.tags as string[]).map((t) => (
              <span key={t} className="px-1.5 py-0.5 rounded bg-surface-2 border border-border-subtle text-[10px] font-mono text-ink-muted">
                {t}
              </span>
            ))}
          </div>
        </div>
      ) : null}
    </div>
  );
}

/* ─────────────────────────────────────────────────────────────────
   Prompt section
   ─────────────────────────────────────────────────────────────── */

function PromptTab({ data, promptPath }: { data: NodeData; promptPath: string | null }) {
  const liveContent = useFile(promptPath ?? "__noop__");
  const inlinePrompt = useMemo(() => {
    const raw = data.raw as Record<string, unknown> | undefined;
    const prompt = raw?.system_prompt as string | undefined;
    if (!prompt) return null;
    if (prompt.startsWith("{{") && prompt.endsWith("}}")) return null;
    return prompt;
  }, [data]);
  // In standalone dev mode the workspace has no live files — fall back
  // to the bundled fixtures we ship with the canvas.
  const isDev = readSession().sessionId === "_dev_";
  const fixtureContent = isDev && promptPath ? getFixtureFile(promptPath) : null;

  const content = liveContent || fixtureContent || inlinePrompt;

  if (!content) {
    const raw = data.raw as Record<string, unknown> | undefined;
    const tpl = raw?.system_prompt as string | undefined;
    return (
      <div className="p-4 space-y-3">
        <div className="text-xs text-ink-dim italic">
          The prompt file is not loaded in this preview.
        </div>
        {tpl && (
          <div className="rounded-lg border border-border-subtle bg-surface-2 p-3 font-mono text-[11px] text-ink-muted">
            <div className="text-[9px] uppercase tracking-wider text-ink-dim mb-1.5">Template reference</div>
            {tpl}
          </div>
        )}
        {promptPath && (
          <div className="flex items-center gap-1.5 text-[11px] text-ink-dim font-mono">
            <FileText className="w-3 h-3" />
            Expected at: {promptPath}
          </div>
        )}
        <div className="text-[11px] text-ink-dim">
          Open this canvas with a live session to load the file from the workspace,
          or paste the file into <span className="font-mono">prompts/</span>.
        </div>
      </div>
    );
  }

  return (
    <div className="p-4">
      {promptPath && (
        <div className="flex items-center gap-1.5 text-[11px] text-ink-dim mb-3 font-mono">
          <FileText className="w-3 h-3" />
          {promptPath}
        </div>
      )}
      <div className="prose-inspector">
        <ReactMarkdown remarkPlugins={[remarkGfm]}>{content}</ReactMarkdown>
      </div>
    </div>
  );
}

function extractPromptPath(data: NodeData): string | null {
  if (data.kind !== "agent") return null;
  const raw = data.raw as Record<string, unknown> | undefined;
  const prompt = raw?.system_prompt as string | undefined;
  if (!prompt) return null;
  const m = prompt.match(/^\{\{\s*prompt\.([\w-]+)\s*\}\}$/);
  if (m) return `prompts/${m[1]}.md`;
  const inc = prompt.match(/^\{\{\s*include:([^}]+)\s*\}\}$/);
  if (inc) return inc[1].trim();
  return null;
}

/* ─────────────────────────────────────────────────────────────────
   YAML section — scoped to the selected block
   ─────────────────────────────────────────────────────────────── */

function YamlTab({ data }: { data: NodeData }) {
  const yamlText = useMemo(() => scopedYaml(data), [data]);
  return (
    <div className="p-4">
      <div className="text-[10px] uppercase tracking-wider text-ink-dim mb-2 font-medium">
        YAML scope: {scopedYamlBreadcrumb(data)}
      </div>
      <CopyableCode code={yamlText} />
    </div>
  );
}

function scopedYaml(data: NodeData): string {
  const kind = data.kind as string;
  let payload: unknown;
  try {
    if (kind === "app") {
      const raw = (data.raw ?? {}) as Record<string, unknown>;
      payload = { app: raw.app ?? {} };
    } else if (kind === "agent") {
      payload = { agents: [data.raw] };
    } else if (kind === "module") {
      const raw = (data.raw ?? {}) as Record<string, unknown>;
      const id = (raw.id as string | undefined) ?? "<module>";
      const cfg = (raw.config as unknown) ?? raw;
      payload = { modules: { [id]: cfg } };
    } else if (kind === "skill") {
      payload = { skills: [data.raw] };
    } else if (kind === "hook") {
      payload = { execution: { hooks: [data.raw] } };
    } else if (kind === "trigger") {
      payload = { execution: { triggers: [data.raw] } };
    } else if (kind === "channel") {
      const raw = (data.raw ?? {}) as Record<string, unknown>;
      const name = (raw.name as string | undefined) ?? "<channel>";
      payload = { channels: { [name]: raw } };
    } else if (kind === "workspace") {
      payload = { workspace: data.raw };
    } else if (kind === "behavior") {
      payload = { behavior: data.raw };
    } else if (kind === "widgets") {
      payload = { widgets: data.raw };
    } else if (kind === "preview") {
      payload = { preview: data.raw };
    } else if (kind === "capabilities") {
      payload = { capabilities: data.raw };
    } else if (kind === "variables") {
      payload = { variables: data.raw };
    } else if (kind === "middleware") {
      payload = { middleware: data.raw };
    } else {
      payload = data.raw ?? {};
    }
    return yaml.dump(payload, { indent: 2, lineWidth: 100 });
  } catch {
    return JSON.stringify(data.raw, null, 2);
  }
}

function scopedYamlBreadcrumb(data: NodeData): string {
  const kind = data.kind as string;
  switch (kind) {
    case "app": return "app:";
    case "agent": return `agents[].id="${(data.raw as { id?: string } | undefined)?.id ?? data.label}"`;
    case "module": return `modules.${(data.raw as { id?: string } | undefined)?.id ?? data.label}`;
    case "skill": return `skills[].command="${(data.raw as { command?: string } | undefined)?.command ?? data.label}"`;
    case "hook": return "execution.hooks[]";
    case "trigger": return "execution.triggers[]";
    case "channel": return `channels.${(data.raw as { name?: string } | undefined)?.name ?? data.label}`;
    case "workspace": return "workspace:";
    case "behavior": return "behavior:";
    case "widgets": return "widgets:";
    case "preview": return "preview:";
    case "capabilities": return "capabilities:";
    case "variables": return "variables:";
    case "middleware": return "middleware:";
    default: return kind;
  }
}

/* ─────────────────────────────────────────────────────────────────
   Building blocks
   ─────────────────────────────────────────────────────────────── */

function KV({ label, value, mono }: { label: string; value?: string | number | undefined; mono?: boolean }) {
  if (value === undefined || value === "") return null;
  return (
    <div className="flex items-baseline gap-3 py-1">
      <span className="text-[10px] uppercase tracking-wider text-ink-dim w-24 flex-shrink-0">{label}</span>
      <span className={clsx("text-xs text-ink min-w-0 break-words", mono && "font-mono text-ink-muted")}>
        {String(value)}
      </span>
    </div>
  );
}

function Section({ title, icon: Icon, children }: { title: string; icon: typeof Settings; children: React.ReactNode }) {
  return (
    <div>
      <div className="flex items-center gap-1.5 mb-2 text-[11px] uppercase tracking-wider text-ink-dim font-medium">
        <Icon className="w-3 h-3" />
        {title}
      </div>
      <div className="space-y-1">{children}</div>
    </div>
  );
}

function RawJson({ value, compact }: { value: unknown; compact?: boolean }) {
  if (value === undefined || value === null) {
    return <div className="text-xs text-ink-dim italic">empty</div>;
  }
  const json = JSON.stringify(value, null, 2);
  return (
    <pre className={clsx(
      "rounded-lg border border-border-subtle bg-surface-2 p-3 overflow-x-auto",
      compact ? "text-[11px] leading-snug" : "text-xs leading-normal",
      "font-mono text-ink",
    )}>
      {json}
    </pre>
  );
}

function CopyableCode({ code }: { code: string }) {
  const [copied, setCopied] = useState(false);
  return (
    <div className="relative group">
      <button
        onClick={() => {
          navigator.clipboard.writeText(code);
          setCopied(true);
          setTimeout(() => setCopied(false), 1500);
        }}
        className="absolute top-2 right-2 opacity-0 group-hover:opacity-100 transition-opacity p-1.5 rounded bg-surface-3 hover:bg-surface-3 text-ink-muted hover:text-ink"
        title="Copy"
      >
        {copied ? <Check className="w-3.5 h-3.5 text-status-ok" /> : <Copy className="w-3.5 h-3.5" />}
      </button>
      <pre className="rounded-lg border border-border-subtle bg-surface-2 p-3 overflow-x-auto text-xs font-mono text-ink leading-relaxed">
        {code}
      </pre>
    </div>
  );
}

/* ─────────────────────────────────────────────────────────────────
   Deps panel — used by / uses (clickable navigation)
   ─────────────────────────────────────────────────────────────── */

function DepsPanel({
  deps,
  onSelect,
}: {
  deps: DepsInfo;
  onSelect?: (id: string) => void;
}) {
  return (
    <div className="px-4 py-4 space-y-4">
      {deps.usedBy.length > 0 && (
        <div>
          <div className="text-[10px] uppercase tracking-wider text-ink-dim mb-1.5 font-medium">
            Used by ({deps.usedBy.length})
          </div>
          <div className="space-y-1">
            {deps.usedBy.map((d) => (
              <button
                key={d.id}
                onClick={() => onSelect?.(d.id)}
                className="w-full text-left flex items-center gap-2 px-2.5 py-1.5 rounded-lg bg-surface-2 hover:bg-surface-3 border border-border-subtle text-xs text-ink-muted hover:text-ink transition-colors group"
              >
                <ChevronRight className="w-3 h-3 text-ink-dim rotate-180 flex-shrink-0" />
                <span className="font-mono truncate flex-1">{d.label}</span>
                {d.via && (
                  <span className="text-[10px] text-ink-dim font-mono px-1.5 py-0.5 rounded bg-surface-3 flex-shrink-0">
                    {d.via}
                  </span>
                )}
              </button>
            ))}
          </div>
        </div>
      )}
      {deps.uses.length > 0 && (
        <div>
          <div className="text-[10px] uppercase tracking-wider text-ink-dim mb-1.5 font-medium">
            Uses ({deps.uses.length})
          </div>
          <div className="space-y-1">
            {deps.uses.map((d) => (
              <button
                key={d.id}
                onClick={() => onSelect?.(d.id)}
                className="w-full text-left flex items-center gap-2 px-2.5 py-1.5 rounded-lg bg-surface-2 hover:bg-surface-3 border border-border-subtle text-xs text-ink-muted hover:text-ink transition-colors group"
              >
                <ChevronRight className="w-3 h-3 text-ink-dim flex-shrink-0" />
                <span className="font-mono truncate flex-1">{d.label}</span>
                {d.via && (
                  <span className="text-[10px] text-ink-dim font-mono px-1.5 py-0.5 rounded bg-surface-3 flex-shrink-0">
                    {d.via}
                  </span>
                )}
              </button>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}
