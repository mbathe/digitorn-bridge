/**
 * Animated Story-mode walkthrough.
 *
 * When `playing` is true, the runner steps through `steps` on a fixed
 * cadence. At each step it:
 *  1. Returns the set of "in-focus" node IDs (everyone else dims out).
 *  2. Returns the list of "active" edges (animated arrow pulse).
 *  3. Renders a centered caption banner explaining the step.
 *
 * Pure UI orchestrator — the parent merges the focus/edges sets into
 * the canvas state on each tick.
 */
import { useEffect, useState } from "react";
import { Pause, Play, SkipForward, X } from "lucide-react";
import clsx from "clsx";
import type { StoryStep } from "../lib/story-script";

interface Props {
  steps: StoryStep[];
  playing: boolean;
  onPlay: () => void;
  onPause: () => void;
  onClose: () => void;
  onStep: (active: { nodeIds: Set<string>; edgePairs: Array<[string, string]> }) => void;
  /** Time per step in ms. Defaults to 2200ms — enough to read each caption. */
  stepMs?: number;
}

export default function StoryRunner({
  steps, playing, onPlay, onPause, onClose, onStep, stepMs = 2200,
}: Props) {
  const [idx, setIdx] = useState(0);

  // Reset to step 0 whenever the script changes (different YAML).
  useEffect(() => { setIdx(0); }, [steps.length]);

  // Auto-advance on a timer when playing.
  useEffect(() => {
    if (!playing || steps.length === 0) return;
    const t = setTimeout(() => {
      setIdx((i) => (i + 1) % steps.length);
    }, stepMs);
    return () => clearTimeout(t);
  }, [playing, idx, steps.length, stepMs]);

  // Push the current step to the parent so the canvas updates.
  useEffect(() => {
    if (steps.length === 0) {
      onStep({ nodeIds: new Set(), edgePairs: [] });
      return;
    }
    const step = steps[idx];
    onStep({
      nodeIds: new Set(step.nodes),
      edgePairs: step.edges.map((e) => [e.source, e.target] as [string, string]),
    });
  }, [idx, steps, onStep]);

  if (steps.length === 0) {
    return (
      <div className="absolute top-3 left-1/2 -translate-x-1/2 z-30 px-4 py-2 rounded-lg bg-surface-1/95 border border-border-subtle backdrop-blur-md shadow-lg text-xs text-ink-muted">
        No story available — define an entry agent first.
        <button onClick={onClose} className="ml-3 text-ink-dim hover:text-ink"><X className="inline w-3 h-3" /></button>
      </div>
    );
  }

  const step = steps[idx];
  return (
    <div className="absolute top-3 left-1/2 -translate-x-1/2 z-30 w-[min(720px,90%)] flex flex-col gap-2 pointer-events-auto">
      <div className="px-4 py-3 rounded-xl bg-surface-1/95 border border-accent/40 backdrop-blur-md shadow-lg">
        <div className="flex items-center gap-3">
          <span className="text-[10px] uppercase tracking-wider font-bold text-accent">Story</span>
          <span className="text-[10px] font-mono text-ink-dim">
            {idx + 1} / {steps.length}
          </span>
          <div className="flex-1 h-1 rounded-full bg-surface-3 overflow-hidden">
            <div
              className="h-full bg-accent transition-all"
              style={{ width: `${((idx + 1) / steps.length) * 100}%` }}
            />
          </div>
          <button onClick={() => playing ? onPause() : onPlay()} className="text-ink-muted hover:text-ink" title={playing ? "Pause" : "Play"}>
            {playing ? <Pause className="w-3.5 h-3.5" /> : <Play className="w-3.5 h-3.5" />}
          </button>
          <button onClick={() => setIdx((i) => (i + 1) % steps.length)} className="text-ink-muted hover:text-ink" title="Next">
            <SkipForward className="w-3.5 h-3.5" />
          </button>
          <button onClick={onClose} className="text-ink-muted hover:text-ink" title="Exit story">
            <X className="w-3.5 h-3.5" />
          </button>
        </div>
        <div className={clsx("mt-2 text-sm text-ink leading-relaxed")}>
          {step.caption}
        </div>
      </div>
    </div>
  );
}
