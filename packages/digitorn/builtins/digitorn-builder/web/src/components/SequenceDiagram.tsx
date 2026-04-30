/**
 * Pure-SVG UML sequence diagram renderer.
 *
 * Layout:
 *   - Each lifeline is a vertical column with a header card at top
 *     and a vertical line going down through the message timeline.
 *   - Each message is a horizontal arrow from source to target lifeline,
 *     placed at y = HEADER_H + step * MSG_H + MSG_H/2.
 *   - Self-messages (kind === "self") loop back to the same lifeline.
 *   - Frames (loop / alt / opt / par) are rounded rectangles spanning
 *     the lifelines they touch, with a labelled tab in the top-left.
 *
 * The renderer is pure — it doesn't care about ReactFlow. Drop it
 * into any container with overflow scrolling.
 */
import { useMemo, useState, useEffect } from "react";
import clsx from "clsx";
import { Play, Pause, SkipForward, SkipBack, RotateCcw, X } from "lucide-react";
import { SEQUENCE_SCENARIOS, type SeqLifelineKind, type SequenceDiagram, type SequenceScenario } from "../lib/sequence-diagram";

interface Props {
  diagram: SequenceDiagram;
  /** Selected node id from the architecture canvas, used to highlight
   *  related messages so the two views can cross-link. */
  selectedNodeId?: string | null;
  onSelectNode?: (id: string) => void;
  onClose?: () => void;
  /** Active scenario — drives which builder produced `diagram`. */
  scenario?: SequenceScenario;
  onScenarioChange?: (s: SequenceScenario) => void;
}

const LIFELINE_W = 180;
const HEADER_H = 90;
const MSG_H = 56;
const FRAME_PAD_X = 12;
const FRAME_PAD_Y = 14;
const FRAME_LABEL_H = 18;
const ARROW_HEAD = 8;
const TOP_PAD = 18;
const BOTTOM_PAD = 32;

const KIND_TINT: Record<SeqLifelineKind, { bg: string; ring: string; fg: string }> = {
  user:       { bg: "rgb(59, 130, 246)",  ring: "rgb(59, 130, 246)",  fg: "rgb(219, 234, 254)" },
  system:     { bg: "rgb(100, 116, 139)", ring: "rgb(100, 116, 139)", fg: "rgb(226, 232, 240)" },
  behavior:   { bg: "rgb(245, 158, 11)",  ring: "rgb(245, 158, 11)",  fg: "rgb(254, 243, 199)" },
  middleware: { bg: "rgb(165, 180, 252)", ring: "rgb(165, 180, 252)", fg: "rgb(238, 242, 255)" },
  agent:      { bg: "rgb(168, 85, 247)",  ring: "rgb(168, 85, 247)",  fg: "rgb(243, 232, 255)" },
  approval:   { bg: "rgb(251, 113, 133)", ring: "rgb(251, 113, 133)", fg: "rgb(255, 228, 230)" },
  module:     { bg: "rgb(56, 189, 248)",  ring: "rgb(56, 189, 248)",  fg: "rgb(224, 242, 254)" },
  hook:       { bg: "rgb(192, 132, 252)", ring: "rgb(192, 132, 252)", fg: "rgb(245, 230, 255)" },
  memory:     { bg: "rgb(16, 185, 129)",  ring: "rgb(16, 185, 129)",  fg: "rgb(209, 250, 229)" },
  workspace:  { bg: "rgb(132, 204, 22)",  ring: "rgb(132, 204, 22)",  fg: "rgb(236, 252, 203)" },
};

const FRAME_TINT: Record<string, string> = {
  loop: "rgb(56, 189, 248)",
  alt: "rgb(251, 113, 133)",
  opt: "rgb(165, 180, 252)",
  par: "rgb(192, 132, 252)",
  ref: "rgb(100, 116, 139)",
  critical: "rgb(245, 158, 11)",
};

export default function SequenceDiagram({ diagram, selectedNodeId, onSelectNode, onClose, scenario, onScenarioChange }: Props) {
  const [playing, setPlaying] = useState(false);
  const [cursor, setCursor] = useState<number>(diagram.messages.length); // all visible by default

  // Reset cursor when diagram changes
  useEffect(() => { setCursor(diagram.messages.length); setPlaying(false); }, [diagram.messages.length]);

  // Auto-advance during playback
  useEffect(() => {
    if (!playing) return;
    if (cursor >= diagram.messages.length) {
      setPlaying(false);
      return;
    }
    const t = setTimeout(() => setCursor((c) => c + 1), 1100);
    return () => clearTimeout(t);
  }, [playing, cursor, diagram.messages.length]);

  // ── Layout maths ─────────────────────────────────────────────────
  const lifelineX = useMemo(() => {
    const m = new Map<string, number>();
    diagram.lifelines.forEach((l, i) => {
      m.set(l.id, LIFELINE_W / 2 + i * LIFELINE_W);
    });
    return m;
  }, [diagram.lifelines]);

  const totalW = Math.max(LIFELINE_W * 4, diagram.lifelines.length * LIFELINE_W);
  const totalH = HEADER_H + TOP_PAD + diagram.messages.length * MSG_H + BOTTOM_PAD;

  // Step helpers
  const yForStep = (step: number) => HEADER_H + TOP_PAD + step * MSG_H + MSG_H / 2;

  if (diagram.lifelines.length === 0) {
    return (
      <div className="flex items-center justify-center h-full text-ink-dim text-sm italic">
        No app loaded — open a YAML to see its sequence diagram.
      </div>
    );
  }

  return (
    <div className="relative w-full h-full bg-surface-0 flex flex-col">
      {/* Toolbar bar */}
      <div className="flex items-center gap-2 px-4 h-11 border-b border-border-subtle bg-surface-1 flex-shrink-0">
        <span className="text-[10px] uppercase tracking-wider font-bold text-accent">
          Sequence Diagram
        </span>
        <span className="text-[10px] font-mono text-ink-dim">
          {diagram.lifelines.length} actors · {diagram.messages.length} messages · {diagram.frames.length} frames
        </span>
        {onScenarioChange && (
          <select
            value={scenario ?? "classic"}
            onChange={(e) => onScenarioChange(e.target.value as SequenceScenario)}
            className="ml-2 h-7 px-2 rounded-md bg-surface-2 border border-border-subtle text-[11px] text-ink"
            title="Pick a scenario"
          >
            {SEQUENCE_SCENARIOS.map((s) => (
              <option key={s.id} value={s.id}>{s.label}</option>
            ))}
          </select>
        )}
        <div className="flex-1" />
        <button
          onClick={() => { setCursor(0); setPlaying(true); }}
          className="inline-flex items-center gap-1.5 h-7 px-2.5 rounded-md text-xs text-ink-muted hover:text-ink hover:bg-surface-2"
          title="Replay from start"
        >
          <RotateCcw className="w-3.5 h-3.5" />
          Replay
        </button>
        <button
          onClick={() => setCursor((c) => Math.max(0, c - 1))}
          className="h-7 w-7 inline-flex items-center justify-center rounded-md text-ink-muted hover:text-ink hover:bg-surface-2"
          title="Previous step"
        >
          <SkipBack className="w-3.5 h-3.5" />
        </button>
        <button
          onClick={() => setPlaying((p) => !p)}
          className="h-7 w-7 inline-flex items-center justify-center rounded-md bg-accent/15 text-accent hover:bg-accent/25"
          title={playing ? "Pause" : "Play"}
        >
          {playing ? <Pause className="w-3.5 h-3.5" /> : <Play className="w-3.5 h-3.5" />}
        </button>
        <button
          onClick={() => setCursor((c) => Math.min(diagram.messages.length, c + 1))}
          className="h-7 w-7 inline-flex items-center justify-center rounded-md text-ink-muted hover:text-ink hover:bg-surface-2"
          title="Next step"
        >
          <SkipForward className="w-3.5 h-3.5" />
        </button>
        <span className="text-[10px] font-mono text-ink-muted w-16 text-right">
          {cursor} / {diagram.messages.length}
        </span>
        {onClose && (
          <button
            onClick={onClose}
            className="ml-2 h-7 w-7 inline-flex items-center justify-center rounded-md text-ink-muted hover:text-ink hover:bg-surface-2"
            title="Close"
          >
            <X className="w-3.5 h-3.5" />
          </button>
        )}
      </div>

      {/* Diagram area */}
      <div className="flex-1 min-h-0 overflow-auto bg-surface-0">
        <svg
          width={totalW}
          height={totalH}
          className="block"
          style={{ background: "transparent" }}
        >
          <defs>
            {/* Sync arrowhead (filled triangle) */}
            <marker
              id="seq-arrow-sync"
              viewBox="-10 -10 20 20"
              refX="0" refY="0"
              markerWidth={ARROW_HEAD} markerHeight={ARROW_HEAD}
              orient="auto-start-reverse"
            >
              <polygon points="-8,-6 0,0 -8,6" fill="rgb(148, 163, 184)" />
            </marker>
            {/* Async arrowhead (open) */}
            <marker
              id="seq-arrow-async"
              viewBox="-10 -10 20 20"
              refX="0" refY="0"
              markerWidth={ARROW_HEAD} markerHeight={ARROW_HEAD}
              orient="auto-start-reverse"
            >
              <polyline points="-8,-6 0,0 -8,6" fill="none" stroke="rgb(148, 163, 184)" strokeWidth="1.5" />
            </marker>
            {/* Return arrowhead (smaller, dashed) */}
            <marker
              id="seq-arrow-return"
              viewBox="-10 -10 20 20"
              refX="0" refY="0"
              markerWidth={ARROW_HEAD} markerHeight={ARROW_HEAD}
              orient="auto-start-reverse"
            >
              <polyline points="-8,-6 0,0 -8,6" fill="none" stroke="rgb(125, 211, 252)" strokeWidth="1.5" />
            </marker>
          </defs>

          {/* Lifeline columns */}
          {diagram.lifelines.map((l) => {
            const x = lifelineX.get(l.id) ?? 0;
            const tint = KIND_TINT[l.kind];
            const isSelectedFromCanvas = selectedNodeId === l.id;
            return (
              <g key={l.id} onClick={() => onSelectNode?.(l.id)} className="cursor-pointer">
                {/* Header card */}
                <rect
                  x={x - 78}
                  y={12}
                  width={156}
                  height={66}
                  rx={10}
                  fill="rgb(14, 22, 36)"
                  stroke={isSelectedFromCanvas ? tint.bg : `${tint.ring}55`}
                  strokeWidth={isSelectedFromCanvas ? 2 : 1.5}
                />
                <rect
                  x={x - 78}
                  y={12}
                  width={156}
                  height={4}
                  rx={2}
                  fill={tint.bg}
                />
                <text
                  x={x}
                  y={36}
                  textAnchor="middle"
                  className="text-[11px] font-semibold"
                  fill={tint.fg}
                  style={{ fontSize: 12, fontWeight: 600 }}
                >
                  {l.label}
                </text>
                <text
                  x={x}
                  y={54}
                  textAnchor="middle"
                  fill="rgb(148, 163, 184)"
                  style={{ fontSize: 10 }}
                >
                  {l.kind}
                </text>
                {l.sublabel && (
                  <text
                    x={x}
                    y={68}
                    textAnchor="middle"
                    fill="rgb(125, 211, 252)"
                    style={{ fontSize: 9, fontFamily: "monospace" }}
                  >
                    {l.sublabel.slice(0, 22)}
                  </text>
                )}
                {/* Vertical lifeline (dashed) */}
                <line
                  x1={x}
                  y1={HEADER_H}
                  x2={x}
                  y2={totalH - 12}
                  stroke="rgb(45, 60, 80)"
                  strokeWidth={1}
                  strokeDasharray="4 4"
                />
              </g>
            );
          })}

          {/* Frames (drawn FIRST so messages overlay them) */}
          {diagram.frames.map((f, i) => {
            const idsInFrame = new Set<string>();
            for (let s = f.startStep; s <= f.endStep; s++) {
              const m = diagram.messages[s];
              if (m) {
                idsInFrame.add(m.from);
                idsInFrame.add(m.to);
              }
            }
            const xs = [...idsInFrame].map((id) => lifelineX.get(id) ?? 0);
            if (xs.length === 0) return null;
            const xMin = Math.min(...xs) - FRAME_PAD_X;
            const xMax = Math.max(...xs) + FRAME_PAD_X;
            const yMin = yForStep(f.startStep) - MSG_H / 2 - FRAME_PAD_Y;
            const yMax = yForStep(f.endStep) + MSG_H / 2 + FRAME_PAD_Y;
            const tint = FRAME_TINT[f.type] ?? FRAME_TINT.ref;
            return (
              <g key={`frame-${i}`}>
                <rect
                  x={xMin}
                  y={yMin}
                  width={xMax - xMin}
                  height={yMax - yMin}
                  rx={4}
                  fill="none"
                  stroke={tint}
                  strokeOpacity={0.6}
                  strokeWidth={1}
                  strokeDasharray="6 3"
                />
                {/* Label tab top-left */}
                <rect
                  x={xMin}
                  y={yMin}
                  width={Math.max(60, f.label.length * 6 + 28)}
                  height={FRAME_LABEL_H}
                  fill={tint}
                  fillOpacity={0.85}
                />
                <text
                  x={xMin + 6}
                  y={yMin + 13}
                  fill="rgb(14, 22, 36)"
                  style={{ fontSize: 10, fontWeight: 700, fontFamily: "monospace" }}
                >
                  {f.type}
                </text>
                <text
                  x={xMin + 36}
                  y={yMin + 13}
                  fill="rgb(14, 22, 36)"
                  style={{ fontSize: 10, fontWeight: 500 }}
                >
                  [{f.label}]
                </text>
                {/* Branch separators (alt) */}
                {f.branches?.map((br, bi) => {
                  const yBr = yForStep(br.startStep) - MSG_H / 2 - 4;
                  return (
                    <g key={`br-${i}-${bi}`}>
                      <line
                        x1={xMin}
                        y1={yBr}
                        x2={xMax}
                        y2={yBr}
                        stroke={tint}
                        strokeOpacity={0.4}
                        strokeWidth={0.8}
                        strokeDasharray="3 3"
                      />
                      <text
                        x={xMin + 8}
                        y={yBr - 2}
                        fill={tint}
                        style={{ fontSize: 9, fontStyle: "italic", fontFamily: "monospace" }}
                      >
                        [{br.guard}]
                      </text>
                    </g>
                  );
                })}
              </g>
            );
          })}

          {/* Messages */}
          {diagram.messages.map((m) => {
            const sx = lifelineX.get(m.from) ?? 0;
            const tx = lifelineX.get(m.to) ?? 0;
            const y = yForStep(m.step);
            const dimmed = m.step >= cursor;
            const opacity = dimmed ? 0.18 : 1;
            const dashed = m.kind === "return";
            const stroke = dashed ? "rgb(125, 211, 252)" : "rgb(148, 163, 184)";

            if (m.kind === "self") {
              const off = 32;
              return (
                <g key={`msg-${m.step}`} opacity={opacity}>
                  <path
                    d={`M ${sx} ${y - 8} L ${sx + off} ${y - 8} L ${sx + off} ${y + 8} L ${sx} ${y + 8}`}
                    fill="none"
                    stroke={stroke}
                    strokeWidth={1.5}
                    strokeDasharray={dashed ? "4 3" : undefined}
                    markerEnd="url(#seq-arrow-sync)"
                  />
                  <text x={sx + off + 6} y={y + 4} fill="rgb(203, 213, 225)" style={{ fontSize: 11 }}>
                    {m.label}
                  </text>
                </g>
              );
            }

            const dir = tx > sx ? 1 : -1;
            const labelX = (sx + tx) / 2;
            return (
              <g
                key={`msg-${m.step}`}
                opacity={opacity}
                className="cursor-pointer"
                onClick={() => m.linkedNodeId && onSelectNode?.(m.linkedNodeId)}
              >
                <line
                  x1={sx}
                  y1={y}
                  x2={tx}
                  y2={y}
                  stroke={stroke}
                  strokeWidth={1.5}
                  strokeDasharray={dashed ? "5 3" : undefined}
                  markerEnd={
                    m.kind === "async" ? "url(#seq-arrow-async)"
                    : m.kind === "return" ? "url(#seq-arrow-return)"
                    : "url(#seq-arrow-sync)"
                  }
                />
                {/* Step number bubble */}
                <circle
                  cx={dir > 0 ? sx + 14 : sx - 14}
                  cy={y - 8}
                  r={9}
                  fill="rgb(14, 22, 36)"
                  stroke={stroke}
                  strokeWidth={1}
                />
                <text
                  x={dir > 0 ? sx + 14 : sx - 14}
                  y={y - 5}
                  textAnchor="middle"
                  fill={stroke}
                  style={{ fontSize: 9, fontWeight: 700, fontFamily: "monospace" }}
                >
                  {m.step + 1}
                </text>
                {/* Label */}
                <rect
                  x={labelX - Math.min(110, m.label.length * 3.5)}
                  y={y - 22}
                  width={Math.min(220, m.label.length * 7)}
                  height={16}
                  rx={3}
                  fill="rgb(14, 22, 36)"
                  fillOpacity={0.92}
                />
                <text
                  x={labelX}
                  y={y - 10}
                  textAnchor="middle"
                  fill={dashed ? "rgb(125, 211, 252)" : "rgb(203, 213, 225)"}
                  style={{ fontSize: 11, fontFamily: dashed ? "monospace" : "inherit" }}
                >
                  {m.label}
                </text>
                {m.detail && (
                  <title>{m.detail}</title>
                )}
              </g>
            );
          })}

          {/* Cursor line — current playback position */}
          {cursor < diagram.messages.length && (
            <line
              x1={0}
              y1={yForStep(cursor) - MSG_H / 2 - 2}
              x2={totalW}
              y2={yForStep(cursor) - MSG_H / 2 - 2}
              stroke="rgb(56, 189, 248)"
              strokeWidth={1.5}
              strokeDasharray="2 4"
              opacity={0.5}
            />
          )}
        </svg>
      </div>
    </div>
  );
}
