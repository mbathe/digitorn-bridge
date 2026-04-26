import { useState, useMemo, useEffect } from "react";
import {
  X, FileText, Settings, Code, Brain, Wrench, Webhook, Zap, Mail,
  ChevronRight, Copy, Check,
} from "lucide-react";
import clsx from "clsx";
import yaml from "js-yaml";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { useFile } from "@digitorn/preview-sdk";
import type { NodeData, ParsedYaml } from "../lib/yaml-to-graph";
import { summarizeAgent } from "../lib/summarize-agent";
import AgentBehaviorCard from "./AgentBehaviorCard";
import AgentToolsTab from "./AgentToolsTab";

type Tab = "config" | "prompt" | "tools" | "yaml";

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

export default function Inspector({ data, deps, doc, onSelectNode, onClose }: Props) {
  const [tab, setTab] = useState<Tab>("config");
  // For skill click-through: when user clicks a skill's file path, we
  // override the prompt-tab path with the skill's .md.
  const [externalFilePath, setExternalFilePath] = useState<string | null>(null);

  // Reset tab + external file when selecting a new node
  useEffect(() => {
    setTab("config");
    setExternalFilePath(null);
  }, [data?.label, data?.kind]);

  if (!data) return null;

  // Available tabs depend on node kind.
  const promptPath = extractPromptPath(data);
  const dataKind = data.kind as string;
  const isSkillOrFileBacked = dataKind === "skill" && (data.raw as any)?.path;
  const isAgent = dataKind === "agent";
  const tabs: { id: Tab; label: string; icon: typeof FileText }[] = [
    { id: "config", label: "Config", icon: Settings },
  ];
  if (promptPath || isAgent || isSkillOrFileBacked) {
    tabs.push({
      id: "prompt",
      label: dataKind === "skill" ? "Content" : "Prompt",
      icon: FileText,
    });
  }
  if (isAgent) {
    tabs.push({ id: "tools", label: "Tools", icon: Wrench });
  }
  tabs.push({ id: "yaml", label: "YAML", icon: Code });

  // Behaviour profile (computed once per agent selection)
  const agentProfile = useMemo(() => {
    if (!isAgent) return null;
    return summarizeAgent((data.raw ?? {}) as Record<string, unknown>, doc ?? null);
  }, [isAgent, data.raw, doc]);

  return (
    <aside className="w-[440px] flex-shrink-0 border-l border-border-subtle bg-surface-1 flex flex-col h-full">
      {/* Header */}
      <header className="flex items-start gap-3 p-4 border-b border-border-subtle">
        <KindBadge kind={data.kind} icon={data.icon} />
        <div className="flex-1 min-w-0">
          <div className="text-sm font-semibold text-ink truncate">
            {data.label}
          </div>
          {data.subtitle && (
            <div className="text-xs text-ink-muted mt-0.5 truncate">
              {data.subtitle}
            </div>
          )}
        </div>
        <button
          onClick={onClose}
          className="text-ink-dim hover:text-ink p-1 rounded transition-colors"
          title="Close"
        >
          <X className="w-4 h-4" />
        </button>
      </header>

      {/* Tabs */}
      <div className="flex items-center gap-0.5 px-2 pt-2 border-b border-border-subtle bg-surface-1">
        {tabs.map((t) => {
          const Icon = t.icon;
          return (
            <button
              key={t.id}
              onClick={() => setTab(t.id)}
              className={clsx(
                "h-8 px-3 inline-flex items-center gap-1.5 text-xs rounded-t-lg border-b-2 transition-colors",
                tab === t.id
                  ? "text-accent border-accent bg-surface-2/60"
                  : "text-ink-muted border-transparent hover:text-ink",
              )}
            >
              <Icon className="w-3.5 h-3.5" />
              {t.label}
            </button>
          );
        })}
      </div>

      {/* Body */}
      <div className="flex-1 overflow-y-auto">
        {tab === "config" && (
          <>
            {agentProfile && <AgentBehaviorCard profile={agentProfile} />}
            <ConfigTab
              data={data}
              onOpenPrompt={() => setTab("prompt")}
              onOpenSkill={(path) => {
                setExternalFilePath(path);
                setTab("prompt");
              }}
            />
            {deps && (deps.uses.length > 0 || deps.usedBy.length > 0) && (
              <DepsPanel deps={deps} onSelect={onSelectNode} />
            )}
          </>
        )}
        {tab === "prompt" && (
          <PromptTab
            data={data}
            promptPath={externalFilePath ?? promptPath}
          />
        )}
        {tab === "tools" && agentProfile && <AgentToolsTab profile={agentProfile} />}
        {tab === "yaml" && <YamlTab data={data} />}
      </div>
    </aside>
  );
}

function DepsPanel({
  deps,
  onSelect,
}: {
  deps: DepsInfo;
  onSelect?: (id: string) => void;
}) {
  return (
    <div className="px-4 pb-6 space-y-4 border-t border-border-subtle pt-4 mt-2">
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

/* ─────────────────────────────────────────────────────────────────
   Kind badge (left of header)
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
  error: "bg-status-error/15 text-status-error",
};

function KindBadge({ kind }: { kind: string; icon?: string }) {
  const tint = KIND_TINT[kind] ?? KIND_TINT.module;
  const Icon =
    kind === "agent" ? Brain :
    kind === "module" ? Wrench :
    kind === "hook" ? Webhook :
    kind === "trigger" ? Zap :
    kind === "channel" ? Mail :
    Settings;
  return (
    <div className={clsx("flex-shrink-0 w-9 h-9 rounded-lg flex items-center justify-center", tint)}>
      <Icon className="w-4 h-4" />
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
  if ((data as any).kind === "skill") return <SkillConfig raw={raw} onOpen={onOpenSkill} />;
  if ((data as any).kind === "workspace") return <WorkspaceConfig raw={raw} />;
  if ((data as any).kind === "behavior") return <BehaviorConfig raw={raw} />;
  if ((data as any).kind === "widgets") return <WidgetsConfig raw={raw} />;
  if ((data as any).kind === "capabilities") return <CapabilitiesConfig raw={raw} />;
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
  const brain = agent.brain as Record<string, unknown> | undefined;
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

      {brain && (
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
  return (
    <div className="p-4 space-y-3">
      <KV label="Event" value={raw.event as string} mono />
      <Section title="Condition" icon={Settings}>
        <RawJson value={raw.condition} compact />
      </Section>
      <Section title="Action" icon={Zap}>
        <RawJson value={raw.action} compact />
      </Section>
      {raw.cooldown !== undefined && <KV label="Cooldown (s)" value={String(raw.cooldown)} mono />}
      {raw.priority !== undefined && <KV label="Priority" value={String(raw.priority)} mono />}
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
   Prompt tab — load + render markdown for agent system prompts
   ─────────────────────────────────────────────────────────────── */

function PromptTab({ data, promptPath }: { data: NodeData; promptPath: string | null }) {
  const fileContent = useFile(promptPath ?? "__noop__");
  const inlinePrompt = useMemo(() => {
    const raw = data.raw as Record<string, unknown> | undefined;
    const prompt = raw?.system_prompt as string | undefined;
    if (!prompt) return null;
    if (prompt.startsWith("{{") && prompt.endsWith("}}")) return null;
    return prompt;
  }, [data]);

  const content = fileContent || inlinePrompt;

  if (!content) {
    // Fallback: show the raw template (e.g. "{{prompt.system}}") + a hint.
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
  // Match {{prompt.<name>}} → prompts/<name>.md
  const m = prompt.match(/^\{\{\s*prompt\.([\w-]+)\s*\}\}$/);
  if (m) return `prompts/${m[1]}.md`;
  // Match {{include:<path>}}
  const inc = prompt.match(/^\{\{\s*include:([^}]+)\s*\}\}$/);
  if (inc) return inc[1].trim();
  return null;
}

/* ─────────────────────────────────────────────────────────────────
   YAML tab — show the raw block for this node
   ─────────────────────────────────────────────────────────────── */

function YamlTab({ data }: { data: NodeData }) {
  const yamlText = useMemo(() => {
    return scopedYaml(data);
  }, [data]);

  return (
    <div className="p-4">
      <div className="text-[10px] uppercase tracking-wider text-ink-dim mb-2 font-medium">
        YAML scope: {scopedYamlBreadcrumb(data)}
      </div>
      <CopyableCode code={yamlText} language="yaml" />
    </div>
  );
}

/**
 * Wrap the raw block in its proper YAML parent so the user sees a
 * meaningful snippet (e.g. `agents:\n  - id: ...`) instead of a flat
 * dump. For the app node specifically we slim the raw down to just
 * the `app:` section (the parser merges `app + execution` into raw).
 */
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
      // raw IS already the module's config — wrap with module id
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
    case "agent": return `agents[].id="${(data.raw as any)?.id ?? data.label}"`;
    case "module": return `modules.${(data.raw as any)?.id ?? data.label}`;
    case "skill": return `skills[].command="${(data.raw as any)?.command ?? data.label}"`;
    case "hook": return "execution.hooks[]";
    case "trigger": return "execution.triggers[]";
    case "channel": return `channels.${(data.raw as any)?.name ?? data.label}`;
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

function CopyableCode({ code }: { code: string; language?: string }) {
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

export function Breadcrumb({ items }: { items: string[] }) {
  return (
    <div className="flex items-center gap-1 text-[10px] text-ink-dim">
      {items.map((it, i) => (
        <span key={i} className="inline-flex items-center gap-1">
          {i > 0 && <ChevronRight className="w-2.5 h-2.5" />}
          {it}
        </span>
      ))}
    </div>
  );
}
