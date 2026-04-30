/**
 * Left-side palette panel — draggable kind cards that, when dropped
 * on the canvas, instantiate a default template at the right place
 * in the YAML.
 *
 * This is the foundation of the visual builder. A click-to-add path
 * is also provided for users without drag-drop devices.
 */
import { useState } from "react";
import {
  Bot, Webhook, Wrench, FileCode, Zap, Mail, Shield,
  ShieldCheck, Layout, ChevronLeft, ChevronRight, Plus,
} from "lucide-react";
import clsx from "clsx";
import { TEMPLATES, type NodeTemplate } from "../lib/templates";

const ICON_MAP: Record<string, typeof Bot> = {
  agent: Bot,
  hook: Webhook,
  module: Wrench,
  skill: FileCode,
  trigger: Zap,
  channel: Mail,
  capability: Shield,
  approval: ShieldCheck,
  behavior: Shield,
  workspace: Layout,
};

interface Props {
  onAdd: (template: NodeTemplate) => void;
  collapsed: boolean;
  onToggle: () => void;
}

export default function PalettePanel({ onAdd, collapsed, onToggle }: Props) {
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

  return (
    <aside className="w-[200px] flex-shrink-0 border-r border-border-subtle bg-surface-1 flex flex-col">
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
      <div className="flex-1 overflow-y-auto p-2 space-y-1.5">
        {TEMPLATES.map((tpl) => (
          <PaletteCard key={tpl.kind} template={tpl} onAdd={onAdd} />
        ))}
      </div>
      <div className="px-3 py-2 border-t border-border-subtle text-[10px] text-ink-dim leading-snug">
        Drag a card onto the canvas, or click <span className="font-bold text-accent">+</span> to add.
      </div>
    </aside>
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
      className="group relative p-2.5 rounded-lg bg-surface-2/50 hover:bg-surface-2 border border-border-subtle hover:border-border cursor-grab active:cursor-grabbing transition-colors"
    >
      <div className="flex items-start gap-2">
        <div className="flex-shrink-0 w-7 h-7 rounded-md bg-accent/10 text-accent flex items-center justify-center">
          <Icon className="w-3.5 h-3.5" />
        </div>
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-1.5">
            <span className="text-xs font-semibold text-ink truncate">
              {template.label}
            </span>
            <button
              onClick={(e) => { e.stopPropagation(); onAdd(template); }}
              className={clsx(
                "ml-auto w-5 h-5 inline-flex items-center justify-center rounded text-accent hover:bg-accent/15",
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
