import { useState, useMemo, useEffect } from "react";
import {
  X, FileText, Settings, Code, Brain, Wrench, Webhook, Zap, Mail,
  ChevronRight, ChevronDown, Copy, Check, Eye, Network, Sparkles, AlertTriangle,
  ShieldCheck,
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
import EditableConfig from "./EditableConfig";
import BrainEditor from "./BrainEditor";
import { hintsForKind } from "../lib/schema-hints";
import { Save, RotateCcw, Download } from "lucide-react";
import GrantMatrix from "./GrantMatrix";
import BehaviorRules from "./BehaviorRules";
import WidgetTreeEditor from "./WidgetTreeEditor";
import TriggerWizard from "./TriggerWizard";
import McpSandboxMatrix from "./McpSandboxMatrix";
import ThemeColorPicker from "./ThemeColorPicker";
import FeaturesToggleGrid from "./FeaturesToggleGrid";

type Section =
  | "overview"
  | "config"
  | "prompt"
  | "tools"
  | "hooks"
  | "deps"
  | "validation"
  | "yaml";

interface ValidationIssueItem {
  severity: "error" | "warn" | "info";
  message: string;
  hint?: string;
  fix?: {
    label: string;
    patches: Array<{ path: string; value: unknown }>;
  };
}

interface DepsInfo {
  uses: { id: string; label: string; via?: string }[];
  usedBy: { id: string; label: string; via?: string }[];
}

interface Props {
  data: NodeData | null;
  deps?: DepsInfo;
  doc?: ParsedYaml | null;
  /** Validation issues filtered to the currently-selected node. */
  validationIssues?: ValidationIssueItem[];
  /** YAML dotted path of the selected node. When set, Configuration
   *  becomes an editable form instead of a read-only dump. */
  yamlPath?: string | null;
  /** Whether the YAML has unsaved edits. */
  edited?: boolean;
  /** Update one field by absolute YAML path. */
  onEditField?: (absolutePath: string, value: unknown) => void;
  /** Delete a field / array item by absolute YAML path. */
  onDeleteField?: (absolutePath: string) => void;
  /** Discard all local edits and revert to the source YAML. */
  onResetEdits?: () => void;
  /** Download the current YAML as a file. */
  onDownloadYaml?: () => void;
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
  validation: { label: "Validation", icon: AlertTriangle, group: "audit" },
  yaml: { label: "YAML", icon: Code, group: "audit" },
};

export default function Inspector({
  data: rawData,
  deps,
  doc,
  validationIssues,
  yamlPath,
  edited,
  onEditField,
  onDeleteField,
  onResetEdits,
  onDownloadYaml,
  onSelectNode,
  onClose,
}: Props) {
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

  // MCP module drilldown — when the user selects the module-mcp parent
  // container (yamlPath === "modules.mcp"), surface its declared servers
  // as a clickable list that drills into a per-server editor.
  const isMcpParent = yamlPath === "modules.mcp";
  const mcpServerList: Array<{ name: string; cfg: Record<string, unknown> }> = useMemo(() => {
    if (!isMcpParent) return [];
    const servers = (doc as { modules?: { mcp?: { config?: { servers?: Record<string, unknown> } } } } | null | undefined)
      ?.modules?.mcp?.config?.servers;
    if (!servers || typeof servers !== "object") return [];
    return Object.entries(servers).map(([name, cfg]) => ({ name, cfg: (cfg ?? {}) as Record<string, unknown> }));
  }, [doc, isMcpParent]);
  const [drilledMcpServer, setDrilledMcpServer] = useState<string | null>(null);
  // Reset MCP drilldown when source node changes
  useEffect(() => { setDrilledMcpServer(null); }, [yamlPath]);

  // Channel drilldown — same pattern as MCP. The channel container
  // declares sub-providers (smtp + sendgrid for an `email` channel,
  // for instance); user clicks one to edit its specific config.
  const isChannelParent = !!yamlPath?.startsWith("channels.")
    && yamlPath.split(".").length === 2;
  const channelProvidersList: Array<{ name: string; cfg: Record<string, unknown> }> = useMemo(() => {
    if (!isChannelParent) return [];
    const chName = yamlPath!.split(".")[1];
    const ch = (doc as { channels?: Record<string, { config?: { providers?: Record<string, unknown> } }> } | null | undefined)?.channels?.[chName];
    const providers = ch?.config?.providers;
    if (!providers || typeof providers !== "object") return [];
    return Object.entries(providers).map(([name, cfg]) => ({ name, cfg: (cfg ?? {}) as Record<string, unknown> }));
  }, [doc, isChannelParent, yamlPath]);
  const [drilledChannelProvider, setDrilledChannelProvider] = useState<string | null>(null);
  useEffect(() => { setDrilledChannelProvider(null); }, [yamlPath]);

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
  if (validationIssues && validationIssues.length > 0) sections.push("validation");
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
          isMcpParent ? (
            <McpServersDrilldown
              servers={mcpServerList}
              drilled={drilledMcpServer}
              onSelectServer={setDrilledMcpServer}
              onEditField={onEditField!}
              onDeleteField={onDeleteField!}
              doc={doc}
              edited={edited}
              onResetEdits={onResetEdits}
              onDownloadYaml={onDownloadYaml}
            />
          ) : isChannelParent && channelProvidersList.length > 0 ? (
            <ChannelProvidersDrilldown
              channelName={yamlPath!.split(".")[1]}
              providers={channelProvidersList}
              drilled={drilledChannelProvider}
              onSelectProvider={setDrilledChannelProvider}
              onEditField={onEditField!}
              onDeleteField={onDeleteField!}
              doc={doc}
              edited={edited}
              onResetEdits={onResetEdits}
              onDownloadYaml={onDownloadYaml}
            />
          ) : yamlPath && onEditField && onDeleteField ? (
            <EditableConfigSection
              data={data}
              yamlPath={yamlPath}
              edited={edited}
              doc={doc}
              onEditField={onEditField}
              onDeleteField={onDeleteField}
              onResetEdits={onResetEdits}
              onDownloadYaml={onDownloadYaml}
              onOpenPromptFile={(path) => {
                setExternalFilePath(path);
                setSection("prompt");
              }}
            />
          ) : (
            <ConfigTab
              data={data}
              onOpenPrompt={() => setSection("prompt")}
              onOpenSkill={(path) => {
                setExternalFilePath(path);
                setSection("prompt");
              }}
            />
          )
        )}
        {safeSection === "prompt" && (
          <PromptTab
            data={data}
            promptPath={externalFilePath ?? promptPath}
            yamlPath={yamlPath ?? undefined}
            onEditField={onEditField}
          />
        )}
        {safeSection === "tools" && agentProfile && (
          <AgentToolsTab profile={agentProfile} doc={doc} />
        )}
        {safeSection === "hooks" && agentProfile && (
          <HooksSection hooks={agentProfile.affectingHooks} doc={doc} />
        )}
        {safeSection === "deps" && deps && (
          <DepsPanel deps={deps} onSelect={onSelectNode} />
        )}
        {safeSection === "validation" && validationIssues && (
          <ValidationPanel issues={validationIssues} onApplyFix={onEditField} />
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
    "theme", "features", "mcp_server", "pipeline_step",
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

function ChannelProvidersDrilldown({
  channelName,
  providers,
  drilled,
  onSelectProvider,
  onEditField,
  onDeleteField,
  doc,
  edited,
  onResetEdits,
  onDownloadYaml,
}: {
  channelName: string;
  providers: Array<{ name: string; cfg: Record<string, unknown> }>;
  drilled: string | null;
  onSelectProvider: (name: string | null) => void;
  onEditField: (path: string, value: unknown) => void;
  onDeleteField: (path: string) => void;
  doc?: ParsedYaml | null;
  edited?: boolean;
  onResetEdits?: () => void;
  onDownloadYaml?: () => void;
}) {
  const drilledCfg = drilled ? providers.find((p) => p.name === drilled) : null;
  if (drilled && drilledCfg) {
    const basePath = `channels.${channelName}.config.providers.${drilled}`;
    return (
      <div>
        <div className="px-4 pt-3 pb-2 flex items-center justify-between border-b border-border-subtle">
          <button
            onClick={() => onSelectProvider(null)}
            className="inline-flex items-center gap-1.5 text-[11px] text-ink-muted hover:text-ink"
          >
            <ChevronRight className="w-3 h-3 rotate-180" />
            Back to {channelName} providers
          </button>
          <div className="flex items-center gap-1.5">
            <span className="text-[10px] uppercase tracking-wider text-ink-dim">Editing</span>
            <span className="text-[11px] font-mono font-semibold text-ink">{drilled}</span>
          </div>
        </div>
        <EditableConfigSection
          data={{ kind: "channel", label: drilled, raw: drilledCfg.cfg } as never}
          yamlPath={basePath}
          edited={edited}
          doc={doc}
          onEditField={onEditField}
          onDeleteField={onDeleteField}
          onResetEdits={onResetEdits}
          onDownloadYaml={onDownloadYaml}
        />
      </div>
    );
  }
  return (
    <div className="p-4 space-y-3">
      <div className="text-[10px] uppercase tracking-wider text-ink-dim font-semibold">
        {providers.length} provider{providers.length !== 1 ? "s" : ""} on {channelName}
      </div>
      <div className="space-y-1">
        {providers.map((p) => {
          const cfg = p.cfg as { adapter?: string; type?: string; url?: string; api_key?: string };
          return (
            <button
              key={p.name}
              onClick={() => onSelectProvider(p.name)}
              className="group w-full flex items-start gap-3 p-2.5 rounded-lg bg-surface-2/40 hover:bg-surface-2 border border-border-subtle hover:border-border transition-colors text-left"
            >
              <div className="w-7 h-7 flex-shrink-0 rounded-md bg-kind-channel/15 text-kind-channel flex items-center justify-center">
                <span className="text-[10px] font-mono">{(cfg.adapter ?? cfg.type ?? p.name).slice(0, 4)}</span>
              </div>
              <div className="flex-1 min-w-0">
                <div className="text-xs font-mono font-semibold text-ink truncate">{p.name}</div>
                <div className="text-[10px] text-ink-muted truncate mt-0.5">
                  {cfg.adapter ? `adapter: ${cfg.adapter}` : cfg.type ? `type: ${cfg.type}` : "—"}
                  {cfg.url ? ` · ${cfg.url}` : ""}
                </div>
              </div>
              <ChevronRight className="w-3.5 h-3.5 text-ink-dim group-hover:text-ink flex-shrink-0 mt-1.5" />
            </button>
          );
        })}
      </div>
    </div>
  );
}

function McpServersDrilldown({
  servers,
  drilled,
  onSelectServer,
  onEditField,
  onDeleteField,
  doc,
  edited,
  onResetEdits,
  onDownloadYaml,
}: {
  servers: Array<{ name: string; cfg: Record<string, unknown> }>;
  drilled: string | null;
  onSelectServer: (name: string | null) => void;
  onEditField: (path: string, value: unknown) => void;
  onDeleteField: (path: string) => void;
  doc?: ParsedYaml | null;
  edited?: boolean;
  onResetEdits?: () => void;
  onDownloadYaml?: () => void;
}) {
  const drilledCfg = drilled ? servers.find((s) => s.name === drilled) : null;

  if (drilled && drilledCfg) {
    const basePath = `modules.mcp.config.servers.${drilled}`;
    return (
      <div>
        <div className="px-4 pt-3 pb-2 flex items-center justify-between border-b border-border-subtle">
          <button
            onClick={() => onSelectServer(null)}
            className="inline-flex items-center gap-1.5 text-[11px] text-ink-muted hover:text-ink"
          >
            <ChevronRight className="w-3 h-3 rotate-180" />
            Back to servers
          </button>
          <div className="flex items-center gap-1.5">
            <span className="text-[10px] uppercase tracking-wider text-ink-dim">Editing</span>
            <span className="text-[11px] font-mono font-semibold text-ink">{drilled}</span>
          </div>
        </div>
        <EditableConfigSection
          data={{ kind: "module", label: drilled, raw: drilledCfg.cfg } as never}
          yamlPath={basePath}
          edited={edited}
          doc={doc}
          onEditField={onEditField}
          onDeleteField={onDeleteField}
          onResetEdits={onResetEdits}
          onDownloadYaml={onDownloadYaml}
        />
      </div>
    );
  }

  return (
    <div className="p-4 space-y-3">
      <div className="text-[10px] uppercase tracking-wider text-ink-dim font-semibold">
        {servers.length} MCP server{servers.length !== 1 ? "s" : ""} mounted
      </div>
      {servers.length === 0 && (
        <div className="text-xs text-ink-dim italic">
          No MCP servers declared. Use the palette + MCP server card to add one.
        </div>
      )}
      <div className="space-y-1">
        {servers.map((s) => {
          const cfg = s.cfg as { transport?: string; command?: string; url?: string; sandbox?: { permissions?: string[]; allowed_hosts?: string[]; paths?: { read?: string[]; write?: string[] } } };
          const perms = Array.isArray(cfg.sandbox?.permissions) ? cfg.sandbox!.permissions! : [];
          return (
            <button
              key={s.name}
              onClick={() => onSelectServer(s.name)}
              className="group w-full flex items-start gap-3 p-2.5 rounded-lg bg-surface-2/40 hover:bg-surface-2 border border-border-subtle hover:border-border transition-colors text-left"
            >
              <div className="w-7 h-7 flex-shrink-0 rounded-md bg-kind-module/15 text-kind-module flex items-center justify-center">
                <span className="text-[10px] font-mono">{(cfg.transport ?? "stdio").slice(0, 4)}</span>
              </div>
              <div className="flex-1 min-w-0">
                <div className="text-xs font-mono font-semibold text-ink truncate">{s.name}</div>
                <div className="text-[10px] text-ink-muted truncate mt-0.5">
                  {cfg.transport === "stdio" && cfg.command ? `${cfg.command} ${(cfg as { args?: string[] }).args?.slice(0, 2).join(" ") ?? ""}`
                    : cfg.url ? cfg.url
                    : "—"}
                </div>
                {perms.length > 0 && (
                  <div className="flex flex-wrap gap-1 mt-1.5">
                    {perms.slice(0, 5).map((p) => (
                      <span key={p} className="text-[9px] font-mono px-1.5 py-0.5 rounded bg-status-warn/10 text-status-warn">
                        🛡 {p}
                      </span>
                    ))}
                  </div>
                )}
              </div>
              <ChevronRight className="w-3.5 h-3.5 text-ink-dim group-hover:text-ink flex-shrink-0 mt-1.5" />
            </button>
          );
        })}
      </div>
    </div>
  );
}

function EditableConfigSection({
  data,
  yamlPath,
  edited,
  doc,
  onEditField,
  onDeleteField,
  onResetEdits,
  onDownloadYaml,
  onOpenPromptFile,
}: {
  data: NodeData;
  yamlPath: string;
  edited?: boolean;
  doc?: ParsedYaml | null;
  onEditField: (absolutePath: string, value: unknown) => void;
  onDeleteField: (absolutePath: string) => void;
  onResetEdits?: () => void;
  onDownloadYaml?: () => void;
  onOpenPromptFile?: (path: string) => void;
}) {
  const kind = data.kind as string;
  const hints = hintsForKind(kind);
  const value = data.raw ?? {};
  return (
    <div className="p-4 space-y-3">
      <div className="flex items-center gap-2 mb-1">
        <div className="text-[10px] uppercase tracking-wider text-ink-dim font-semibold">
          Editable · {yamlPath}
        </div>
        <div className="flex-1" />
        {edited && onResetEdits && (
          <button
            onClick={onResetEdits}
            className="inline-flex items-center gap-1 h-7 px-2 rounded-md text-[11px] text-ink-muted hover:text-ink hover:bg-surface-2"
            title="Discard all local edits and revert to the source YAML"
          >
            <RotateCcw className="w-3 h-3" />
            Reset
          </button>
        )}
        {onDownloadYaml && (
          <button
            onClick={onDownloadYaml}
            className={clsx(
              "inline-flex items-center gap-1 h-7 px-2 rounded-md text-[11px]",
              edited
                ? "bg-accent/15 text-accent hover:bg-accent/25"
                : "text-ink-muted hover:text-ink hover:bg-surface-2",
            )}
            title="Download the current YAML"
          >
            {edited ? <Save className="w-3 h-3" /> : <Download className="w-3 h-3" />}
            {edited ? "Save .yaml" : "Download .yaml"}
          </button>
        )}
      </div>
      {edited && (
        <div className="text-[10px] text-status-warn bg-status-warn/10 border border-status-warn/30 rounded px-2 py-1.5">
          You have unsaved edits. Click "Save .yaml" to download the modified app.
        </div>
      )}
      {/* Capabilities get a dedicated matrix editor (per-action chips
          for grant / approve / deny) — much better UX than the generic
          recursive form. The generic form is still rendered below for
          fields the matrix doesn't cover (default_policy, max_risk_level). */}
      {kind === "capabilities" && (
        <GrantMatrix
          capabilities={value as never}
          declaredModules={Object.keys(((doc as { modules?: Record<string, unknown> } | null | undefined)?.modules) ?? {})}
          basePath={yamlPath}
          onEdit={onEditField}
        />
      )}
      {kind === "behavior" && <BehaviorRules behavior={value as never} />}
      {kind === "widgets" ? (
        <WidgetTreeEditor
          raw={value as Record<string, unknown>}
          basePath={yamlPath}
          doc={doc}
          onEdit={onEditField}
          onDelete={onDeleteField}
        />
      ) : kind === "trigger" ? (
        <TriggerWizard
          raw={value as Record<string, unknown>}
          basePath={yamlPath}
          onEdit={onEditField}
          onDelete={onDeleteField}
        />
      ) : kind === "mcp_server" ? (
        <McpSandboxMatrix
          raw={value as Record<string, unknown>}
          basePath={yamlPath}
          onEdit={onEditField}
        />
      ) : kind === "theme" ? (
        <ThemeColorPicker
          raw={value as Record<string, string>}
          basePath={yamlPath}
          onEdit={onEditField}
        />
      ) : kind === "features" ? (
        <FeaturesToggleGrid
          raw={value as Record<string, boolean>}
          basePath={yamlPath}
          onEdit={onEditField}
          onDelete={onDeleteField}
        />
      ) : kind === "agent" ? (
        // Agents get a dedicated editor for the `brain` block (and its
        // nested `brain.fallback`) so users see structured fields with
        // friendly labels instead of a recursive JSON-like tree.
        // Other agent fields (id, role, system_prompt, modules, ...)
        // still go through the generic EditableConfig below.
        <AgentEditableTabs
          value={value as Record<string, unknown>}
          basePath={yamlPath}
          schemaHints={hints}
          doc={doc}
          onEdit={onEditField}
          onDelete={onDeleteField}
          onOpenPromptFile={onOpenPromptFile}
        />
      ) : kind === "fallback_brain" ? (
        // Synthetic fallback-brain card: route directly to BrainEditor
        // with isFallback so the user gets the same structured form
        // (provider, model, backend, context, config, credential) as
        // the primary brain -- not a JSON dump.
        <BrainEditor
          value={value}
          basePath={yamlPath}
          title="Fallback brain"
          hint="Used when the primary brain returns a billing or rate-limit error (HTTP 402, 'Insufficient Balance'). Switches back to primary on the next turn."
          isFallback
          onEdit={onEditField}
          onDelete={onDeleteField}
        />
      ) : (
        <EditableConfig
          value={value}
          basePath={yamlPath}
          schemaHints={hints}
          doc={doc}
          onEdit={onEditField}
          onDelete={onDeleteField}
          onOpenPromptFile={onOpenPromptFile}
        />
      )}
    </div>
  );
}

/**
 * Agent-specific editable form: BrainEditor for the brain block
 * (clean structured fields) + the generic EditableConfig for the
 * rest of the agent (id, role, system_prompt, modules, etc., minus
 * brain so we don't render it twice).
 */
function AgentEditableTabs({
  value,
  basePath,
  schemaHints,
  doc,
  onEdit,
  onDelete,
  onOpenPromptFile,
}: {
  value: Record<string, unknown>;
  basePath: string;
  schemaHints?: Record<string, import("./EditableConfig").SchemaHint>;
  doc?: ParsedYaml | null;
  onEdit: (absolutePath: string, value: unknown) => void;
  onDelete: (absolutePath: string) => void;
  onOpenPromptFile?: (path: string) => void;
}) {
  // Strip `brain` from the value passed to the generic editor — we
  // render it separately above with the dedicated BrainEditor. Same
  // for `fallback` if it ever leaks at the top level.
  const restValue = useMemo(() => {
    const { brain: _brain, ...rest } = value;
    void _brain;
    return rest;
  }, [value]);

  return (
    <div className="space-y-4">
      <div className="rounded-lg border border-border-subtle/60 bg-surface-1/40 p-3">
        <BrainEditor
          value={value.brain}
          basePath={`${basePath}.brain`}
          title="Brain"
          hint="The LLM that runs this agent's reasoning loop."
          onEdit={onEdit}
          onDelete={onDelete}
        />
      </div>
      <div className="rounded-lg border border-border-subtle/60 bg-surface-1/40 p-3">
        <div className="text-[10px] uppercase tracking-wider text-ink-dim font-semibold mb-2">
          Other agent fields
        </div>
        <EditableConfig
          value={restValue}
          basePath={basePath}
          schemaHints={schemaHints}
          doc={doc}
          onEdit={onEdit}
          onDelete={onDelete}
          onOpenPromptFile={onOpenPromptFile}
        />
      </div>
    </div>
  );
}

function ValidationPanel({
  issues,
  onApplyFix,
}: {
  issues: ValidationIssueItem[];
  onApplyFix?: (path: string, value: unknown) => void;
}) {
  const [openErrors, setOpenErrors] = useState(true);
  const [openWarns, setOpenWarns] = useState(true);
  const [openInfos, setOpenInfos] = useState(false);
  const errs = issues.filter((i) => i.severity === "error");
  const warns = issues.filter((i) => i.severity === "warn");
  const infos = issues.filter((i) => i.severity === "info");
  const groups: Array<{
    sev: ValidationIssueItem["severity"]; label: string; items: ValidationIssueItem[];
    open: boolean; setOpen: (b: boolean) => void;
  }> = [
    { sev: "error", label: "Errors",   items: errs,  open: openErrors, setOpen: setOpenErrors },
    { sev: "warn",  label: "Warnings", items: warns, open: openWarns,  setOpen: setOpenWarns },
    { sev: "info",  label: "Info",     items: infos, open: openInfos,  setOpen: setOpenInfos },
  ];
  return (
    <div className="p-4 space-y-3">
      <div className="text-[10px] uppercase tracking-wider text-ink-dim font-medium">
        {issues.length} issue{issues.length > 1 ? "s" : ""} ·
        {" "}<span className="text-status-error font-mono">{errs.length}</span> err ·
        {" "}<span className="text-status-warn font-mono">{warns.length}</span> warn ·
        {" "}<span className="text-status-running font-mono">{infos.length}</span> info
      </div>
      {groups.map((g) => {
        if (g.items.length === 0) return null;
        return (
          <div key={g.sev} className="space-y-1">
            <button
              onClick={() => g.setOpen(!g.open)}
              className="w-full flex items-center gap-1.5 text-[10px] uppercase tracking-wider font-semibold text-ink-muted hover:text-ink"
            >
              {g.open ? <ChevronDown className="w-2.5 h-2.5" /> : <ChevronRight className="w-2.5 h-2.5" />}
              <span>{g.label}</span>
              <span className="font-mono text-ink-dim">{g.items.length}</span>
            </button>
            {g.open && g.items.map((issue, i) => (
              <div
                key={i}
                className={clsx(
                  "flex items-start gap-3 p-3 rounded-lg border ml-3",
                  issue.severity === "error" && "bg-status-error/10 border-status-error/40",
                  issue.severity === "warn" && "bg-status-warn/10 border-status-warn/40",
                  issue.severity === "info" && "bg-status-running/10 border-status-running/40",
                )}
              >
                <span
                  className={clsx(
                    "flex-shrink-0 w-2.5 h-2.5 rounded-full mt-1.5",
                    issue.severity === "error" && "bg-status-error",
                    issue.severity === "warn" && "bg-status-warn",
                    issue.severity === "info" && "bg-status-running",
                  )}
                />
                <div className="flex-1 min-w-0">
                  <div className="text-xs text-ink">{issue.message}</div>
                  {issue.hint && (
                    <div className="text-[11px] text-ink-muted mt-1.5 italic">
                      {issue.hint}
                    </div>
                  )}
                  {issue.fix && onApplyFix && (
                    <button
                      onClick={() => {
                        for (const p of issue.fix!.patches) onApplyFix(p.path, p.value);
                      }}
                      className="mt-2 inline-flex items-center gap-1 h-6 px-2 rounded-md text-[10px] font-semibold bg-accent/15 text-accent hover:bg-accent/25"
                    >
                      ⚡ {issue.fix.label}
                    </button>
                  )}
                </div>
              </div>
            ))}
          </div>
        );
      })}
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
  // Look up the raw hook block from `doc.runtime.hooks` so we can render
  // a real HookCard (with parameters, conditions, effect) — the lighter
  // shape from `summarizeAgent` only carries display strings.
  const rawHooks = (doc?.runtime?.hooks ?? []) as Array<Record<string, unknown>>;
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
  if (data.kind === "flow_node") return <FlowNodeConfig raw={raw} />;
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
          <div className="text-[10px] text-ink-dim italic mb-2">
            Used when the primary brain returns a billing / rate-limit error
            (HTTP 402, "Insufficient Balance", "credit"...). Switches back to
            primary on the next turn.
          </div>
          <KV label="Provider" value={fallback.provider as string} mono />
          <KV label="Model" value={fallback.model as string} mono />
          <KV label="Backend" value={fallback.backend as string} mono />
          <KV label="Temperature" value={fallback.temperature !== undefined ? String(fallback.temperature) : undefined} mono />
          <KV label="Max tokens" value={fallback.max_tokens !== undefined ? String(fallback.max_tokens) : undefined} mono />
          {Boolean(fallback.context && typeof fallback.context === "object") && (() => {
            const fctx = fallback.context as Record<string, unknown>;
            return (
              <div className="ml-3 mt-2 pl-3 border-l border-border-subtle">
                <div className="text-[10px] uppercase tracking-wider text-ink-dim mb-1">Context</div>
                <KV label="Window" value={fctx.max_tokens !== undefined ? `${fctx.max_tokens} tokens` : undefined} mono />
                <KV label="Strategy" value={fctx.strategy as string} mono />
                <KV label="Keep recent" value={fctx.keep_recent !== undefined ? String(fctx.keep_recent) : undefined} mono />
              </div>
            );
          })()}
          {Boolean(fallback.config && typeof fallback.config === "object" && Object.keys(fallback.config as Record<string, unknown>).length > 0) && (
            <div className="ml-3 mt-2 pl-3 border-l border-border-subtle">
              <div className="text-[10px] uppercase tracking-wider text-ink-dim mb-1">Backend config</div>
              {Object.entries(fallback.config as Record<string, unknown>).map(([k, v]) => (
                <KV
                  key={k}
                  label={k}
                  // Mask anything that smells like a credential -- show the
                  // first 4 chars then dots so the user can confirm the
                  // YAML wired the right key without leaking it.
                  value={
                    /key|token|secret|password/i.test(k) && typeof v === "string"
                      ? `${v.slice(0, 4)}${v.length > 4 ? "..." : ""}`
                      : typeof v === "object" ? JSON.stringify(v) : String(v)
                  }
                  mono
                />
              ))}
            </div>
          )}
          {Boolean(fallback.credential) && (
            <KV label="Credential" value={typeof fallback.credential === "string" ? fallback.credential : (fallback.credential as { ref?: string }).ref ?? "(complex)"} mono />
          )}
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

/* ─────────────────────────────────────────────────────────────────
   Flow node config (Phase 9 declarative orchestration graph)
   Per-type fields rendered for the 6 node types. Read-only inspector;
   edits flow through the YamlPane like every other inspector panel.
   ─────────────────────────────────────────────────────────────── */

function FlowNodeConfig({ raw }: { raw: Record<string, unknown> }) {
  const nodeType = (raw.type as string) || "?";
  const id = (raw.id as string) || "?";
  const description = (raw.description as string) || "";
  const routes = (raw.routes ?? []) as Array<{ when?: string; to?: string }>;
  const onError = (raw.on_error ?? []) as Array<{ match?: string; default?: boolean; to?: string }>;

  return (
    <div className="p-4 space-y-3">
      <KV label="Node id" value={id} mono />
      <KV label="Type" value={nodeType} mono />
      {description && <KV label="Description" value={description} />}

      {nodeType === "agent" && (
        <Section title="Agent" icon={Brain}>
          <KV label="Agent ref" value={raw.agent as string} mono />
          {raw.input ? (
            <RawJson value={raw.input} compact />
          ) : null}
        </Section>
      )}

      {nodeType === "tool" && (
        <Section title="Tool" icon={Wrench}>
          <KV label="Tool" value={raw.tool as string} mono />
          {!!raw.params && Object.keys(raw.params as object).length > 0 && (
            <div className="mt-2">
              <div className="text-[10px] uppercase tracking-wider text-ink-dim mb-1.5">Params</div>
              <RawJson value={raw.params} compact />
            </div>
          )}
        </Section>
      )}

      {nodeType === "parallel" && (
        <Section title="Parallel" icon={Zap}>
          <KV
            label="Branches"
            value={`${(raw.branches as unknown[] | undefined)?.length ?? 0} branches`}
            mono
          />
          {raw.join ? (
            <div className="mt-2">
              <div className="text-[10px] uppercase tracking-wider text-ink-dim mb-1.5">Join</div>
              <RawJson value={raw.join} compact />
            </div>
          ) : null}
          {Array.isArray(raw.branches) && (raw.branches as unknown[]).length > 0 ? (
            <div className="mt-2">
              <div className="text-[10px] uppercase tracking-wider text-ink-dim mb-1.5">Branch targets</div>
              <ul className="text-[12px] font-mono space-y-1">
                {(raw.branches as Array<{ when?: string; to?: string }>).map((b, i) => (
                  <li key={i} className="text-ink-muted">
                    <span className="text-ink-dim">→</span> {b.to || "?"}
                    {b.when && b.when !== "default" && (
                      <span className="text-ink-dim"> if {b.when}</span>
                    )}
                  </li>
                ))}
              </ul>
            </div>
          ) : null}
        </Section>
      )}

      {nodeType === "approval" && (
        <Section title="Approval gate" icon={ShieldCheck}>
          <KV label="Message" value={raw.message as string} />
          {Array.isArray(raw.choices) && (raw.choices as unknown[]).length > 0 && (
            <div className="mt-2">
              <div className="text-[10px] uppercase tracking-wider text-ink-dim mb-1.5">Choices</div>
              <div className="flex flex-wrap gap-1">
                {(raw.choices as string[]).map((c) => (
                  <span
                    key={c}
                    className="px-1.5 py-0.5 rounded bg-surface-2 border border-border-subtle text-[10px] font-mono text-ink-muted"
                  >
                    {c}
                  </span>
                ))}
              </div>
            </div>
          )}
        </Section>
      )}

      {nodeType === "decision" && (
        <Section title="Decision" icon={Webhook}>
          <KV label="Expression" value={raw.expr as string} mono />
        </Section>
      )}

      {nodeType === "terminal" && (
        <Section title="Terminal" icon={FileText}>
          {!!raw.output && Object.keys(raw.output as object).length > 0 ? (
            <RawJson value={raw.output} compact />
          ) : (
            <div className="text-[12px] text-ink-dim italic">No output payload</div>
          )}
        </Section>
      )}

      {/* Routes (every node type) */}
      {routes.length > 0 && (
        <Section title={`Routes (${routes.length})`} icon={Mail}>
          <ul className="text-[12px] font-mono space-y-1">
            {routes.map((r, i) => (
              <li key={i} className="text-ink-muted">
                <span className="text-ink-dim">→</span> {r.to || "?"}
                {r.when && r.when !== "default" && (
                  <span className="text-ink-dim"> if {r.when}</span>
                )}
              </li>
            ))}
          </ul>
        </Section>
      )}

      {/* On-error routes */}
      {onError.length > 0 && (
        <Section title="Error handling" icon={Settings}>
          <ul className="text-[12px] font-mono space-y-1">
            {onError.map((r, i) => (
              <li key={i} className="text-ink-muted">
                <span className="text-ink-dim">→</span> {r.to || "?"}
                {r.default ? (
                  <span className="text-ink-dim"> (default catch-all)</span>
                ) : r.match ? (
                  <span className="text-ink-dim"> on match: {r.match}</span>
                ) : null}
              </li>
            ))}
          </ul>
        </Section>
      )}
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

function PromptTab({
  data,
  promptPath,
  yamlPath,
  onEditField,
}: {
  data: NodeData;
  promptPath: string | null;
  yamlPath?: string;
  onEditField?: (path: string, value: unknown) => void;
}) {
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
  const session = readSession();
  const isDev = session.sessionId === "_dev_";
  const fixtureContent = isDev && promptPath ? getFixtureFile(promptPath) : null;

  const content = liveContent || fixtureContent || inlinePrompt;

  // ── Edit-mode state ──────────────────────────────────────────────
  // Two write paths:
  //   * Inline prompt (system_prompt: "literal") → mutate the YAML via
  //     the existing comment-preserving onEditField pipeline.
  //   * File-backed prompt (system_prompt: "{{prompt.X}}") → PUT to the
  //     workspace files endpoint. Live sessions only — dev mode is
  //     read-only because there's no real workspace to write to.
  const isFileBacked = !!promptPath;
  const isInlineEditable =
    !isFileBacked && !!inlinePrompt && !!onEditField && !!yamlPath;
  const isFileEditable = isFileBacked && !isDev;

  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState<string>(content ?? "");
  const [savingState, setSavingState] = useState<
    | { kind: "idle" }
    | { kind: "saving" }
    | { kind: "ok"; at: number }
    | { kind: "err"; msg: string }
  >({ kind: "idle" });

  // Reset draft whenever content reloads (file change, node switch).
  useEffect(() => {
    if (!editing) setDraft(content ?? "");
  }, [content, editing]);

  // Reset everything when target node changes.
  useEffect(() => {
    setEditing(false);
    setSavingState({ kind: "idle" });
  }, [yamlPath, promptPath]);

  const onSave = async () => {
    setSavingState({ kind: "saving" });
    try {
      if (isInlineEditable && onEditField && yamlPath) {
        // Path: agents.0.system_prompt or skills.2.prompt — caller's
        // yamlPath is the agent/skill scope, we append the prompt key.
        onEditField(`${yamlPath}.system_prompt`, draft);
        setSavingState({ kind: "ok", at: Date.now() });
        setEditing(false);
        return;
      }
      if (isFileEditable && promptPath) {
        const url = `${session.baseUrl}/api/apps/${session.appId}/sessions/${session.sessionId}/workspace/files/${promptPath}`;
        const r = await fetch(url, {
          method: "PUT",
          headers: {
            "Content-Type": "application/json",
            ...(session.token ? { Authorization: `Bearer ${session.token}` } : {}),
          },
          body: JSON.stringify({ content: draft, auto_approve: true }),
        });
        if (!r.ok) {
          const text = await r.text().catch(() => "");
          setSavingState({ kind: "err", msg: `HTTP ${r.status} ${text.slice(0, 80)}` });
          return;
        }
        setSavingState({ kind: "ok", at: Date.now() });
        setEditing(false);
        return;
      }
      setSavingState({ kind: "err", msg: "No writable target for this prompt." });
    } catch (e) {
      setSavingState({ kind: "err", msg: String(e).slice(0, 120) });
    }
  };

  const canEdit = isInlineEditable || isFileEditable;
  const dirty = editing && draft !== (content ?? "");

  // ── Empty state ─────────────────────────────────────────────────
  if (!content && !editing) {
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
        {isFileEditable && (
          <button
            onClick={() => { setDraft(""); setEditing(true); }}
            className="w-full px-3 py-2 rounded-md text-xs font-medium bg-accent/15 text-accent border border-accent/30 hover:bg-accent/25 transition-colors"
          >
            Create this prompt file
          </button>
        )}
        <div className="text-[11px] text-ink-dim">
          Open this canvas with a live session to load the file from the workspace,
          or paste the file into <span className="font-mono">prompts/</span>.
        </div>
      </div>
    );
  }

  // ── View / Edit toggle ─────────────────────────────────────────
  return (
    <div className="p-4">
      <div className="flex items-center justify-between mb-3 gap-2">
        <div className="flex items-center gap-1.5 text-[11px] text-ink-dim font-mono min-w-0 flex-1">
          {promptPath ? (
            <>
              <FileText className="w-3 h-3 flex-shrink-0" />
              <span className="truncate">{promptPath}</span>
            </>
          ) : (
            <span className="italic">inline · {yamlPath ?? "system_prompt"}</span>
          )}
        </div>
        {canEdit && !editing && (
          <button
            onClick={() => { setDraft(content ?? ""); setEditing(true); setSavingState({ kind: "idle" }); }}
            className="px-2.5 py-1 rounded text-[11px] font-medium bg-surface-2 hover:bg-surface-3 border border-border-subtle text-ink"
          >
            Edit
          </button>
        )}
        {editing && (
          <div className="flex items-center gap-1">
            <button
              onClick={() => { setEditing(false); setDraft(content ?? ""); setSavingState({ kind: "idle" }); }}
              className="px-2.5 py-1 rounded text-[11px] font-medium bg-surface-2 hover:bg-surface-3 border border-border-subtle text-ink-muted"
              disabled={savingState.kind === "saving"}
            >
              Cancel
            </button>
            <button
              onClick={onSave}
              disabled={!dirty || savingState.kind === "saving"}
              className="px-2.5 py-1 rounded text-[11px] font-medium bg-accent text-white hover:bg-accent/90 disabled:opacity-50 disabled:cursor-not-allowed"
            >
              {savingState.kind === "saving" ? "Saving…" : "Save"}
            </button>
          </div>
        )}
        {!canEdit && (
          <span className="text-[10px] text-ink-dim italic" title={isDev ? "Dev mode: workspace files are read-only here." : "This prompt has no writable target."}>
            read-only
          </span>
        )}
      </div>

      {savingState.kind === "ok" && Date.now() - savingState.at < 4000 && (
        <div className="mb-2 px-2 py-1 rounded text-[10px] bg-status-ok/10 text-status-ok border border-status-ok/30">
          Saved.
        </div>
      )}
      {savingState.kind === "err" && (
        <div className="mb-2 px-2 py-1 rounded text-[10px] bg-status-error/10 text-status-error border border-status-error/30">
          {savingState.msg}
        </div>
      )}

      {editing ? (
        <textarea
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          spellCheck={false}
          className="w-full min-h-[400px] resize-y rounded-md border border-border-subtle bg-surface-1 p-3 font-mono text-[12px] leading-relaxed text-ink focus:outline-none focus:border-accent focus:ring-1 focus:ring-accent/40"
          placeholder="Write the system prompt…"
        />
      ) : (
        <div className="prose-inspector">
          <ReactMarkdown remarkPlugins={[remarkGfm]}>{content ?? ""}</ReactMarkdown>
        </div>
      )}
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
    // v2 canonical shape: scoped previews emit the YAML in the
    // canonical nested form so what the user sees in the inspector
    // matches what the saved file looks like.
    if (kind === "app") {
      const raw = (data.raw ?? {}) as Record<string, unknown>;
      payload = { app: raw.app ?? {} };
    } else if (kind === "agent") {
      payload = { agents: [data.raw] };
    } else if (kind === "module") {
      const raw = (data.raw ?? {}) as Record<string, unknown>;
      const id = (raw.id as string | undefined) ?? "<module>";
      const cfg = (raw.config as unknown) ?? raw;
      payload = { tools: { modules: { [id]: cfg } } };
    } else if (kind === "skill") {
      payload = { dev: { skills: [data.raw] } };
    } else if (kind === "hook") {
      payload = { runtime: { hooks: [data.raw] } };
    } else if (kind === "trigger") {
      payload = { runtime: { triggers: [data.raw] } };
    } else if (kind === "channel") {
      const raw = (data.raw ?? {}) as Record<string, unknown>;
      const name = (raw.name as string | undefined) ?? "<channel>";
      payload = { tools: { channels: { [name]: raw } } };
    } else if (kind === "workspace") {
      payload = { ui: { workspace: data.raw } };
    } else if (kind === "behavior") {
      payload = { security: { behavior: data.raw } };
    } else if (kind === "widgets") {
      payload = { ui: { widgets: data.raw } };
    } else if (kind === "preview") {
      payload = { ui: { preview: data.raw } };
    } else if (kind === "capabilities") {
      payload = { tools: { capabilities: data.raw } };
    } else if (kind === "variables") {
      payload = { dev: { variables: data.raw } };
    } else if (kind === "middleware") {
      payload = { runtime: { middleware: data.raw } };
    } else if (kind === "theme") {
      payload = { ui: { theme: data.raw } };
    } else if (kind === "features") {
      payload = { ui: { features: data.raw } };
    } else if (kind === "mcp_server") {
      const raw = (data.raw ?? {}) as Record<string, unknown>;
      const name = (data.label as string | undefined) ?? "<server>";
      payload = { tools: { modules: { mcp: { config: { servers: { [name]: raw } } } } } };
    } else if (kind === "pipeline_step") {
      payload = { runtime: { pipeline: [data.raw] } };
    } else if (kind === "sandbox") {
      payload = { security: { sandbox: data.raw } };
    } else if (kind === "credentials" || kind === "credential_provider") {
      payload = { security: { credentials_schema: data.raw } };
    } else if (kind === "fallback_brain") {
      // Synthetic node - represents an agent's brain.fallback block.
      payload = { agents: [{ brain: { fallback: data.raw } }] };
    } else if (kind === "approval_gate") {
      // HITL approval gate is a capabilities.approve[] entry.
      payload = { tools: { capabilities: { approve: [data.raw] } } };
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
    case "module": return `tools.modules.${(data.raw as { id?: string } | undefined)?.id ?? data.label}`;
    case "skill": return `dev.skills[].command="${(data.raw as { command?: string } | undefined)?.command ?? data.label}"`;
    case "hook": return "runtime.hooks[]";
    case "trigger": return "runtime.triggers[]";
    case "channel": return `tools.channels.${(data.raw as { name?: string } | undefined)?.name ?? data.label}`;
    case "workspace": return "ui.workspace:";
    case "behavior": return "security.behavior:";
    case "widgets": return "ui.widgets:";
    case "preview": return "ui.preview:";
    case "capabilities": return "tools.capabilities:";
    case "variables": return "dev.variables:";
    case "middleware": return "runtime.middleware:";
    case "theme": return "ui.theme:";
    case "features": return "ui.features:";
    case "mcp_server": return `tools.modules.mcp.config.servers.${(data.raw as { name?: string } | undefined)?.name ?? data.label}`;
    case "pipeline_step": return "runtime.pipeline[]";
    case "sandbox": return "security.sandbox:";
    case "credentials": return "security.credentials_schema:";
    case "credential_provider": return "security.credentials_schema.providers[]";
    case "fallback_brain": return "agents[].brain.fallback:";
    case "approval_gate": return "tools.capabilities.approve[]";
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
