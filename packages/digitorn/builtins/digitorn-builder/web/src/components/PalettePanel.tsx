/**
 * Left-side palette panel — draggable kind cards that, when dropped
 * on the canvas, instantiate a default template at the right place
 * in the YAML.
 *
 * Templates are grouped into 6 sections that mirror the 8-block YAML
 * language (`agents`, `tools`, `triggers/channels`, `security`, `flow`,
 * `ui & dev`). Each section is independently collapsible so the user
 * can keep the relevant block open while they work and ignore the rest.
 */
import { useState } from "react";
import {
  Bot, Webhook, Wrench, FileCode, Zap, Mail, Shield,
  ShieldCheck, Layout, ChevronLeft, ChevronRight, ChevronDown,
  Plus, Users, Workflow, Lock, Layers, Settings,
} from "lucide-react";
import clsx from "clsx";
import { TEMPLATES, type NodeTemplate } from "../lib/templates";

const ICON_MAP: Record<string, typeof Bot> = {
  agent: Bot,
  hook: Webhook,
  module: Wrench,
  skill: FileCode,
  trigger: Zap,
  trigger_cron: Zap,
  trigger_webhook: Zap,
  trigger_watch: Zap,
  channel: Mail,
  capability: Shield,
  approval: ShieldCheck,
  behavior: Shield,
  rule_definition: Shield,
  workspace: Layout,
  mcp: Wrench,
  mcp_server: Wrench,
  flow: Workflow,
  flow_agent: Bot,
  flow_tool: Wrench,
  flow_parallel: Workflow,
  flow_approval: ShieldCheck,
  flow_decision: Workflow,
  flow_terminal: Workflow,
  sandbox: Lock,
  credentials_schema: Lock,
  payload_schema: Lock,
  ui: Layout,
  pipeline_step: Layers,
  coordination: Users,
  instructions: FileCode,
  dependencies: Settings,
};

/** Section definitions — each section gets a label, an icon, and the
 *  list of template kinds that belong in it. Order here is the order
 *  shown in the panel. Anything not listed here lands in "Other". */
type Section = {
  id: string;
  label: string;
  icon: typeof Bot;
  kinds: string[];
};

const SECTIONS: Section[] = [
  {
    id: "agents",
    label: "Agents",
    icon: Users,
    kinds: ["agent", "coordination", "instructions"],
  },
  {
    id: "tools",
    label: "Tools & modules",
    icon: Wrench,
    kinds: ["module", "mcp", "mcp_server", "skill", "capability", "approval"],
  },
  {
    id: "triggers",
    label: "Triggers & channels",
    icon: Zap,
    kinds: [
      "trigger", "trigger_cron", "trigger_webhook", "trigger_watch",
      "channel",
    ],
  },
  {
    id: "behavior",
    label: "Behavior & hooks",
    icon: Shield,
    kinds: ["behavior", "rule_definition", "hook"],
  },
  {
    id: "flow",
    label: "Flow",
    icon: Workflow,
    kinds: [
      "flow", "flow_agent", "flow_tool", "flow_parallel",
      "flow_approval", "flow_decision", "flow_terminal",
    ],
  },
  {
    id: "security",
    label: "Security & schema",
    icon: Lock,
    kinds: ["sandbox", "credentials_schema", "payload_schema"],
  },
  {
    id: "ui",
    label: "UI & workspace",
    icon: Layout,
    kinds: ["ui", "workspace", "pipeline_step", "dependencies"],
  },
];

interface Props {
  onAdd: (template: NodeTemplate) => void;
  collapsed: boolean;
  onToggle: () => void;
}

export default function PalettePanel({ onAdd, collapsed, onToggle }: Props) {
  // Each section can be opened / closed independently. By default we
  // start with Agents and Tools open (the two blocks every app
  // touches) and the rest collapsed to keep the panel scannable.
  const [open, setOpen] = useState<Record<string, boolean>>(() => ({
    agents: true,
    tools: true,
    triggers: false,
    behavior: false,
    flow: false,
    security: false,
    ui: false,
    other: false,
  }));

  if (collapsed) {
    return (
      <div className="w-9 border-r border-border-subtle bg-surface-1 flex flex-col items-center py-2">
        <button
          onClick={onToggle}
          className="w-7 h-7 inline-flex items-center justify-center rounded-md text-ink-muted hover:text-ink hover:bg-surface-2"
          title="Open palette"
        >
          <ChevronRight className="w-4 h-4" />
        </button>
        <Plus className="w-4 h-4 text-ink-dim mt-2" />
      </div>
    );
  }

  // Build section → templates lookup, plus an "Other" bucket for any
  // template kind not explicitly grouped.
  const byKind = new Map(TEMPLATES.map((t) => [t.kind, t]));
  const used = new Set<string>();
  const sectionTemplates: Array<{
    section: Section;
    templates: NodeTemplate[];
  }> = SECTIONS.map((section) => {
    const templates = section.kinds
      .map((k) => byKind.get(k))
      .filter((t): t is NodeTemplate => !!t);
    templates.forEach((t) => used.add(t.kind));
    return { section, templates };
  });
  const orphan = TEMPLATES.filter((t) => !used.has(t.kind));

  return (
    <aside className="w-[220px] flex-shrink-0 border-r border-border-subtle bg-surface-1 flex flex-col">
      <div className="flex items-center gap-2 px-3 h-11 border-b border-border-subtle">
        <Plus className="w-3.5 h-3.5 text-accent" />
        <span className="text-[10px] uppercase tracking-wider font-bold text-accent">
          Add to App
        </span>
        <div className="flex-1" />
        <button
          onClick={onToggle}
          className="w-6 h-6 inline-flex items-center justify-center rounded text-ink-muted hover:text-ink"
          title="Collapse palette"
        >
          <ChevronLeft className="w-3.5 h-3.5" />
        </button>
      </div>
      <div className="flex-1 overflow-y-auto py-1.5">
        {sectionTemplates.map(({ section, templates }) =>
          templates.length === 0 ? null : (
            <PaletteSection
              key={section.id}
              section={section}
              templates={templates}
              isOpen={!!open[section.id]}
              onToggle={() =>
                setOpen((s) => ({ ...s, [section.id]: !s[section.id] }))
              }
              onAdd={onAdd}
            />
          ),
        )}
        {orphan.length > 0 && (
          <PaletteSection
            section={{
              id: "other",
              label: "Other",
              icon: Layers,
              kinds: orphan.map((t) => t.kind),
            }}
            templates={orphan}
            isOpen={!!open.other}
            onToggle={() => setOpen((s) => ({ ...s, other: !s.other }))}
            onAdd={onAdd}
          />
        )}
      </div>
      <div className="px-3 py-2 border-t border-border-subtle text-[10px] text-ink-dim leading-snug">
        Drag a card onto the canvas, or click <span className="font-bold text-accent">+</span> to add.
      </div>
    </aside>
  );
}

function PaletteSection({
  section,
  templates,
  isOpen,
  onToggle,
  onAdd,
}: {
  section: Section;
  templates: NodeTemplate[];
  isOpen: boolean;
  onToggle: () => void;
  onAdd: (t: NodeTemplate) => void;
}) {
  const Icon = section.icon;
  return (
    <div className="mb-1">
      {/* Section header — clickable strip with a chevron + count */}
      <button
        onClick={onToggle}
        className={clsx(
          "w-full flex items-center gap-1.5 px-2.5 py-1.5 group text-left",
          "hover:bg-surface-2/60 transition-colors",
        )}
      >
        <ChevronDown
          className={clsx(
            "w-3 h-3 text-ink-dim transition-transform flex-shrink-0",
            isOpen ? "rotate-0" : "-rotate-90",
          )}
        />
        <Icon className="w-3.5 h-3.5 text-ink-muted flex-shrink-0" />
        <span className="text-[10px] uppercase tracking-wider font-semibold text-ink-muted truncate">
          {section.label}
        </span>
        <span className="ml-auto text-[10px] font-mono text-ink-dim">
          {templates.length}
        </span>
      </button>
      {/* Card list — one PaletteCard per template, indented under the
          header so the section structure reads at a glance. */}
      {isOpen && (
        <div className="px-2 pt-0.5 pb-1.5 space-y-1">
          {templates.map((tpl) => (
            <PaletteCard key={tpl.kind} template={tpl} onAdd={onAdd} />
          ))}
        </div>
      )}
    </div>
  );
}

function PaletteCard({ template, onAdd }: { template: NodeTemplate; onAdd: (t: NodeTemplate) => void }) {
  const [hover, setHover] = useState(false);
  const Icon = ICON_MAP[template.kind] ?? Wrench;
  return (
    <div
      draggable
      onDragStart={(e) => {
        e.dataTransfer.setData("application/x-digitorn-template", template.kind);
        e.dataTransfer.effectAllowed = "copy";
      }}
      onMouseEnter={() => setHover(true)}
      onMouseLeave={() => setHover(false)}
      className="group relative p-2 rounded-md bg-surface-2/40 hover:bg-surface-2 border border-border-subtle hover:border-border cursor-grab active:cursor-grabbing transition-colors"
    >
      <div className="flex items-start gap-2">
        <div className="flex-shrink-0 w-6 h-6 rounded bg-accent/10 text-accent flex items-center justify-center">
          <Icon className="w-3 h-3" />
        </div>
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-1.5">
            <span className="text-[11px] font-semibold text-ink truncate">
              {template.label}
            </span>
            <button
              onClick={(e) => { e.stopPropagation(); onAdd(template); }}
              className={clsx(
                "ml-auto w-4 h-4 inline-flex items-center justify-center rounded text-accent hover:bg-accent/15",
                "transition-opacity",
                hover ? "opacity-100" : "opacity-0",
              )}
              title="Add to app"
            >
              <Plus className="w-3 h-3" />
            </button>
          </div>
          <div className="text-[10px] text-ink-muted leading-snug mt-0.5 line-clamp-2">
            {template.hint}
          </div>
        </div>
      </div>
    </div>
  );
}
