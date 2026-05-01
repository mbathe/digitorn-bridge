/**
 * Edge legend popover — explains the 7 semantic edge colors so the
 * user can decode arrows at a glance without hovering each one.
 *
 * Triggered from a small "?" button in the toolbar.
 */
import { useEffect, useRef, useState } from "react";
import { Palette } from "lucide-react";
import { EDGE_KIND_LEGEND, styleForEdgeKind } from "../lib/edge-kinds";

export default function EdgeLegend() {
  const [open, setOpen] = useState(false);
  const wrapRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!open) return;
    const onClick = (e: MouseEvent) => {
      if (wrapRef.current && !wrapRef.current.contains(e.target as Node)) setOpen(false);
    };
    window.addEventListener("mousedown", onClick);
    return () => window.removeEventListener("mousedown", onClick);
  }, [open]);

  return (
    <div ref={wrapRef} className="relative">
      <button
        onClick={() => setOpen((o) => !o)}
        className="h-8 w-8 inline-flex items-center justify-center rounded-lg text-xs text-ink-muted hover:text-ink hover:bg-surface-2"
        title="Edge color legend"
      >
        <Palette className="w-3.5 h-3.5" />
      </button>
      {open && (
        <div className="absolute right-0 top-9 z-30 w-[300px] rounded-xl bg-surface-1 border border-border shadow-2xl p-3">
          <div className="text-[10px] uppercase tracking-wider font-bold text-accent mb-2">
            Edge color legend
          </div>
          <div className="space-y-1.5">
            {EDGE_KIND_LEGEND.map((e) => {
              const s = styleForEdgeKind(e.kind);
              return (
                <div key={e.kind} className="flex items-start gap-2.5">
                  <svg width={28} height={12} className="flex-shrink-0 mt-0.5">
                    <line
                      x1={2} y1={6} x2={26} y2={6}
                      stroke={s.stroke}
                      strokeWidth={s.strokeWidth + 0.5}
                      strokeDasharray={s.strokeDasharray}
                    />
                    {s.withArrow && (
                      <polygon points="22,2 26,6 22,10" fill={s.stroke} />
                    )}
                  </svg>
                  <div className="flex-1 min-w-0">
                    <div className="text-[11px] font-semibold text-ink">
                      <span className="font-mono">{e.kind}</span>
                      <span className="text-ink-muted ml-1.5">— {e.label}</span>
                    </div>
                    <div className="text-[10px] text-ink-muted leading-snug">
                      {e.hint}
                    </div>
                  </div>
                </div>
              );
            })}
          </div>
          <div className="text-[10px] text-ink-dim mt-2 pt-2 border-t border-border-subtle">
            Hover any edge in the canvas for a contextual tooltip.
          </div>
        </div>
      )}
    </div>
  );
}
