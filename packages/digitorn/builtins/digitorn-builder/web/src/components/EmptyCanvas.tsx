/**
 * Empty-canvas coach.
 *
 * Renders when the parsed YAML produced no real nodes (or only the
 * skeletal app-root). Drops 4 ghost cards in lifecycle order with
 * captions explaining what to do — clicking any of them dispatches
 * the matching palette template via `onAdd`.
 */
import { Bot, Wrench, FileCode, Sparkles, ArrowRight, Plus } from "lucide-react";
import type { NodeTemplate } from "../lib/templates";

interface Props {
  templates: NodeTemplate[];
  onAdd: (kind: string) => void;
}

const STARTER_FLOW: Array<{ kind: string; title: string; body: string; icon: typeof Bot }> = [
  { kind: "agent",   title: "1. Add an Agent",     body: "The brain that reads + thinks + decides. Start here.",                       icon: Bot },
  { kind: "module",  title: "2. Add a Module",     body: "A capability the agent can call (filesystem, shell, web, lsp, memory…).",   icon: Wrench },
  { kind: "skill",   title: "3. Add a Skill",      body: "Optional. Pre-written /command procedures the user can invoke.",            icon: FileCode },
  { kind: "approval", title: "4. Protect risky ops", body: "Optional. Approval gates for tools like shell.bash or filesystem.write.", icon: Sparkles },
];

export default function EmptyCanvas({ templates, onAdd }: Props) {
  const knownKinds = new Set(templates.map((t) => t.kind));
  return (
    <div className="absolute inset-0 z-10 flex items-center justify-center p-8 pointer-events-none">
      <div className="max-w-3xl w-full pointer-events-auto">
        <div className="text-center mb-6">
          <div className="text-2xl font-bold text-ink mb-2">Build an app from scratch</div>
          <div className="text-sm text-ink-muted">
            Drop these in order — or drag any card from the palette on the left.
          </div>
        </div>
        <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
          {STARTER_FLOW.map((s, i) => {
            const Icon = s.icon;
            const enabled = knownKinds.has(s.kind);
            return (
              <button
                key={s.kind}
                disabled={!enabled}
                onClick={() => onAdd(s.kind)}
                className="group flex items-start gap-3 p-4 rounded-xl bg-surface-1/80 hover:bg-surface-1 border border-border-subtle hover:border-accent/50 transition-all text-left disabled:opacity-50 disabled:cursor-not-allowed"
              >
                <div className="flex-shrink-0 w-10 h-10 rounded-lg bg-accent/10 text-accent flex items-center justify-center group-hover:bg-accent/20">
                  <Icon className="w-4 h-4" />
                </div>
                <div className="flex-1 min-w-0">
                  <div className="flex items-center gap-1.5 mb-1">
                    <span className="text-sm font-semibold text-ink">{s.title}</span>
                    <Plus className="w-3 h-3 text-accent opacity-0 group-hover:opacity-100 transition-opacity" />
                  </div>
                  <div className="text-[11px] text-ink-muted leading-relaxed">
                    {s.body}
                  </div>
                </div>
                {i < STARTER_FLOW.length - 1 && (
                  <ArrowRight className="hidden md:block w-3 h-3 text-ink-dim flex-shrink-0 mt-3.5" />
                )}
              </button>
            );
          })}
        </div>
        <div className="text-center mt-6 text-[11px] text-ink-dim">
          Or click <span className="font-bold text-accent">📘 Tutorial</span> at the top-right for a guided walkthrough.
        </div>
      </div>
    </div>
  );
}
