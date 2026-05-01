/**
 * Preset gallery — lets the user load a complete starting YAML for
 * one of the common app archetypes (chatbot, coding-agent, research,
 * multi-agent team).
 *
 * Triggered from the empty-canvas coach or the Tutorial overlay.
 */
import { X, ArrowRight } from "lucide-react";
import { PRESETS, type AppPreset } from "../lib/presets";

interface Props {
  open: boolean;
  onLoad: (preset: AppPreset) => void;
  onClose: () => void;
}

export default function PresetGallery({ open, onLoad, onClose }: Props) {
  if (!open) return null;
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center p-6">
      <div className="absolute inset-0 bg-black/40" onClick={onClose} />
      <div className="relative w-[min(820px,100%)] max-h-[80vh] overflow-y-auto rounded-xl bg-surface-1 border border-border shadow-2xl">
        <div className="flex items-center gap-3 px-5 h-12 border-b border-border-subtle sticky top-0 bg-surface-1 z-10">
          <span className="text-sm font-bold text-ink">Load a starter</span>
          <span className="text-[10px] text-ink-dim">Pick an archetype — you can rename and edit everything after.</span>
          <div className="flex-1" />
          <button onClick={onClose} className="text-ink-muted hover:text-ink"><X className="w-4 h-4" /></button>
        </div>
        <div className="p-5 grid grid-cols-1 md:grid-cols-2 gap-3">
          {PRESETS.map((p) => (
            <button
              key={p.id}
              onClick={() => { onLoad(p); onClose(); }}
              className="group flex items-start gap-3 p-4 rounded-xl bg-surface-2/50 hover:bg-surface-2 border border-border-subtle hover:border-accent/50 transition-all text-left"
            >
              <div className="flex-shrink-0 w-10 h-10 rounded-lg bg-accent/10 text-2xl flex items-center justify-center">
                {p.emoji}
              </div>
              <div className="flex-1 min-w-0">
                <div className="flex items-center gap-1.5 mb-1">
                  <span className="text-sm font-semibold text-ink">{p.label}</span>
                  <ArrowRight className="w-3 h-3 text-accent opacity-0 group-hover:opacity-100 transition-opacity" />
                </div>
                <div className="text-[11px] text-ink-muted leading-relaxed">{p.description}</div>
              </div>
            </button>
          ))}
        </div>
        <div className="px-5 pb-4 text-[10px] text-ink-dim text-center">
          Loading a preset replaces the current canvas. Your in-progress edits are lost (use Undo to restore).
        </div>
      </div>
    </div>
  );
}
