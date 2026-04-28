import clsx from "clsx";
import type { LaneRibbon } from "../lib/lane-layout";

/**
 * Sticky lane title rail rendered as a sibling of the ReactFlow
 * viewport. Each ribbon spans the lane's vertical range and shows
 * the lane name + count. Pure visual layer — no interactivity beyond
 * the optional "expand" callback for the palette lane.
 */

interface Props {
  ribbons: LaneRibbon[];
  /** Map a viewport y-coord through the current pan/zoom. */
  transform?: { x: number; y: number; zoom: number };
  onExpandPalette?: () => void;
}

const TONE: Record<string, { bg: string; text: string; ring: string }> = {
  info: { bg: "bg-status-running/10", text: "text-status-running", ring: "ring-status-running/30" },
  warn: { bg: "bg-status-warn/10", text: "text-status-warn", ring: "ring-status-warn/30" },
  ok: { bg: "bg-status-ok/10", text: "text-status-ok", ring: "ring-status-ok/30" },
  accent: { bg: "bg-accent/10", text: "text-accent", ring: "ring-accent/30" },
  muted: { bg: "bg-surface-3/40", text: "text-ink-muted", ring: "ring-border-subtle" },
};

export default function LaneRibbons({ ribbons, transform, onExpandPalette }: Props) {
  const tx = transform?.x ?? 0;
  const ty = transform?.y ?? 0;
  const zoom = transform?.zoom ?? 1;

  return (
    <div className="absolute inset-0 pointer-events-none overflow-hidden" aria-hidden="false">
      {ribbons.map((r) => {
        const t = TONE[r.lane.tone] ?? TONE.muted;
        const top = ty + r.y * zoom;
        const height = r.height * zoom;
        return (
          <div
            key={r.lane.id}
            className="absolute left-0 right-0 flex items-start"
            style={{ top, height }}
          >
            <div
              className={clsx(
                "ml-2 mt-2 flex items-center gap-2 px-2 py-1 rounded-md ring-1 backdrop-blur-sm",
                t.bg, t.text, t.ring,
                "pointer-events-auto",
              )}
              title={r.lane.hint}
            >
              <span className="text-[14px] font-mono opacity-80 leading-none">{r.lane.short}</span>
              <span className="text-[11px] uppercase tracking-wider font-semibold whitespace-nowrap">
                {r.lane.label}
              </span>
              <span className="text-[10px] font-mono opacity-70">
                {r.count}
              </span>
              {r.hidden > 0 && (
                <button
                  type="button"
                  onClick={onExpandPalette}
                  className="text-[10px] underline opacity-80 hover:opacity-100"
                  title={`Show all ${r.count} items`}
                >
                  show all
                </button>
              )}
            </div>
            {/* faint horizontal stripe spanning the whole row */}
            <div
              className={clsx(
                "absolute left-[156px] right-2 rounded-lg",
                t.bg,
                "opacity-40",
              )}
              style={{
                top: 0,
                bottom: 0,
              }}
            />
          </div>
        );
      })}
      {/* Lane separators */}
      {ribbons.map((r, i) => {
        if (i === 0) return null;
        const top = ty + r.y * zoom - 8;
        return (
          <div
            key={`sep-${r.lane.id}`}
            className="absolute left-2 right-2 border-t border-border-subtle/50"
            style={{ top }}
          />
        );
      })}

      {/* Lifecycle flow rail — subway-map vertical line on the left
          spelling out the transition between each adjacent lane so the
          user actually SEES the flow direction (user → palette →
          middleware → behavior → ...). */}
      {ribbons.map((r, i) => {
        if (i === ribbons.length - 1) return null;
        const next = ribbons[i + 1];
        const flow = r.lane.flow;
        if (!flow) return null;
        // y = vertical midpoint of the gap between this lane's bottom
        // and the next lane's top, transformed by the current viewport.
        const gapTopCanvas = r.y + r.height;
        const gapBottomCanvas = next.y;
        const yMid = ty + ((gapTopCanvas + gapBottomCanvas) / 2) * zoom;
        return (
          <div
            key={`flow-${r.lane.id}-${next.lane.id}`}
            className="absolute flex items-center gap-1.5 -translate-y-1/2 pointer-events-none"
            style={{ top: yMid, left: 16 }}
          >
            <span className="text-accent font-mono text-[14px] leading-none">↓</span>
            <span className="text-[9px] uppercase tracking-wider font-semibold text-ink-muted whitespace-nowrap px-1.5 py-0.5 rounded bg-surface-1/80 border border-border-subtle backdrop-blur-sm">
              {flow}
            </span>
          </div>
        );
      })}

      {/* Vertical spine joining every flow chip — visually anchors the
          subway-map metaphor so the eye reads top-to-bottom even when
          lanes are sparse. The spine spans from the FIRST lifecycle
          lane to the LAST lifecycle lane (the one OUT of which the
          last chevron is drawn). Lanes after that — e.g. APP, which
          is metadata, not a runtime stage — have no flow caption and
          fall outside the spine on purpose. */}
      {ribbons.length > 1 && (() => {
        const first = ribbons[0];
        // Find the last ribbon that emits a flow chevron INTO the next.
        // It's the last index `i` such that ribbons[i].lane.flow is set
        // AND there's a ribbons[i+1] to receive the chevron.
        let lastFlowIdx = -1;
        for (let i = 0; i < ribbons.length - 1; i++) {
          if (ribbons[i].lane.flow) lastFlowIdx = i;
        }
        if (lastFlowIdx < 0) return null;
        const last = ribbons[lastFlowIdx + 1];
        const top = ty + (first.y + first.height) * zoom;
        const bottom = ty + last.y * zoom;
        if (bottom <= top) return null;
        return (
          <div
            className="absolute pointer-events-none border-l border-dashed border-accent/30"
            style={{ top, height: bottom - top, left: 21 }}
          />
        );
      })()}
    </div>
  );
}
