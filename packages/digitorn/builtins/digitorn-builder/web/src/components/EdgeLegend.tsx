/**
 * Edge legend popover — explains the 7 semantic edge colors so the
 * user can decode arrows at a glance without hovering each one.
 *
 * Triggered from a small "?" button in the toolbar.
 */
import { useEffect, useRef, useState } from "react";
import { Palette } from "lucide-react";
import { EDGE_KIND_LEGEND, EDGE_KIND_GROUPS, styleForEdgeKind, type EdgeKind } from "../lib/edge-kinds";

interface Props {
  /** Optional live edge kinds present in the canvas. When provided, each
   *  entry shows "× N" so the user knows what's actually drawn vs only
   *  documented. Edges absent from the canvas get dimmed. */
  edgeKindCounts?: Record<string, number>;
}

export default function EdgeLegend({ edgeKindCounts }: Props) {
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

  const lookup = Object.fromEntries(EDGE_KIND_LEGEND.map((e) => [e.kind, e]));

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
        <div className="absolute right-0 top-9 z-30 w-[340px] max-h-[80vh] overflow-y-auto rounded-xl bg-surface-1 border border-border shadow-2xl p-3">
          <div className="text-[10px] uppercase tracking-wider font-bold text-accent mb-2">
            Edge color legend
          </div>
          <div className="space-y-3">
            {EDGE_KIND_GROUPS.map((group) => (
              <div key={group.label} className="space-y-1.5">
                <div className="text-[10px] uppercase tracking-wider text-ink-muted font-semibold border-b border-border-subtle pb-1">
                  {group.label}
                  <span className="ml-2 normal-case text-ink-dim font-normal">{group.hint}</span>
                </div>
                {group.kinds.map((kind) => {
                  const entry = lookup[kind];
                  if (!entry) return null;
                  const s = styleForEdgeKind(kind);
                  const count = edgeKindCounts?.[kind] ?? 0;
                  const present = count > 0;
                  return (
                    <div
                      key={kind}
                      className={`flex items-start gap-2.5 ${edgeKindCounts && !present ? "opacity-40" : ""}`}
                    >
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
                        <div className="text-[11px] font-semibold text-ink flex items-center gap-1.5">
                          <span className="font-mono">{kind}</span>
                          <span className="text-ink-muted">— {entry.label}</span>
                          {edgeKindCounts && (
                            <span className="ml-auto text-[10px] font-mono text-ink-dim">
                              {present ? `× ${count}` : "—"}
                            </span>
                          )}
                        </div>
                        <div className="text-[10px] text-ink-muted leading-snug">
                          {entry.hint}
                        </div>
                      </div>
                    </div>
                  );
                })}
              </div>
            ))}
          </div>
          <div className="text-[10px] text-ink-dim mt-3 pt-2 border-t border-border-subtle">
            {edgeKindCounts
              ? "Counts reflect what's currently drawn on the canvas. Hover any edge for a contextual tooltip."
              : "Hover any edge in the canvas for a contextual tooltip."}
          </div>
        </div>
      )}
    </div>
  );
}

/** Helper for callers to compute the live edge-kind counts. */
export function countEdgeKinds(edges: Array<{ data?: { edgeKind?: EdgeKind | string } }>): Record<string, number> {
  const out: Record<string, number> = {};
  for (const e of edges) {
    const k = e.data?.edgeKind;
    if (typeof k === "string") out[k] = (out[k] ?? 0) + 1;
  }
  return out;
}
