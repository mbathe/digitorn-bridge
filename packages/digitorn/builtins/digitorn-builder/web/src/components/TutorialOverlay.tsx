/**
 * Guided tutorial overlay — "Build a chatbot in 3 minutes".
 *
 * 6-step walkthrough that drives the user through the minimum
 * pieces needed to make an app run. Each step:
 *  - Highlights the relevant region of the UI (palette card, inspector
 *    section, etc.) via a `targetSelector`.
 *  - Shows a short caption explaining WHAT and WHY.
 *  - Advances on user click.
 *
 * The tutorial is purely educational — it does NOT mutate the YAML
 * automatically. It just points where to click.
 */
import { useEffect, useState } from "react";
import { ChevronRight, X, BookOpen, ChevronLeft } from "lucide-react";
import clsx from "clsx";

interface Step {
  title: string;
  body: string;
  /** CSS selector for an element to highlight, or null for centered. */
  highlight?: string;
}

const STEPS: Step[] = [
  {
    title: "Welcome to the Digitorn builder",
    body: "We'll build a working chatbot in 6 steps. The Palette on the left lets you add components; the canvas shows the flow; the Inspector on the right edits each component. Let's start.",
  },
  {
    title: "1. Add an Agent",
    body: "An Agent is the brain — it reads the user's message and decides what to do. Open the palette, click the + on \"Agent\", and an empty agent appears in the canvas.",
    highlight: "[draggable='true']",
  },
  {
    title: "2. Configure the brain",
    body: "Click your new Agent in the canvas. The Inspector opens with a Configuration tab. Set provider (anthropic / openai / deepseek…), model (claude-sonnet-4-6 for instance), and write a system_prompt that tells the agent what it does.",
  },
  {
    title: "3. Give it tools",
    body: "Add a Module from the palette (filesystem, shell, web, memory…). Then add a Capability grant: choose the module + the actions the agent is allowed to call (e.g. filesystem: [read, grep]). The agent now has access at runtime.",
  },
  {
    title: "4. Protect risky actions",
    body: "If you grant something dangerous (shell.bash, filesystem.write), add an Approval gate. The user has to click approve in the chat before the action runs. The agent can also call ask_user to pause and ask a question first.",
  },
  {
    title: "5. Add a slash command (optional)",
    body: "Skills are pre-written procedures the user invokes with /command. Drop a Skill from the palette, set the command and the .md file path. Great for /commit, /review, /deploy…",
  },
  {
    title: "6. Save and test",
    body: "Click \"Save .yaml\" in the inspector to download your app. Or press \"Test\" in the toolbar to dispatch a test prompt and watch the sequence diagram run live.",
  },
];

interface Props {
  open: boolean;
  onClose: () => void;
}

export default function TutorialOverlay({ open, onClose }: Props) {
  const [step, setStep] = useState(0);
  useEffect(() => { if (open) setStep(0); }, [open]);
  // Close on ESC
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") onClose(); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose]);

  if (!open) return null;
  const cur = STEPS[step];
  const last = step === STEPS.length - 1;

  return (
    <div className="fixed inset-0 z-50 pointer-events-none">
      {/* Dim backdrop */}
      <div className="absolute inset-0 bg-black/30 pointer-events-auto" onClick={onClose} />
      {/* Card */}
      <div className="absolute left-1/2 bottom-12 -translate-x-1/2 w-[min(560px,90vw)] pointer-events-auto">
        <div className="rounded-xl border border-accent/40 bg-surface-1 shadow-2xl overflow-hidden">
          <div className="flex items-center gap-2 px-4 h-9 bg-accent/10 border-b border-accent/30">
            <BookOpen className="w-3.5 h-3.5 text-accent" />
            <span className="text-[10px] uppercase tracking-wider font-bold text-accent">
              Tutorial
            </span>
            <span className="text-[10px] font-mono text-ink-dim">
              {step + 1} / {STEPS.length}
            </span>
            <div className="flex-1 h-1 rounded-full bg-surface-3 overflow-hidden ml-2">
              <div className="h-full bg-accent transition-all" style={{ width: `${((step + 1) / STEPS.length) * 100}%` }} />
            </div>
            <button
              onClick={onClose}
              className="text-ink-muted hover:text-ink"
              title="Close"
            >
              <X className="w-3.5 h-3.5" />
            </button>
          </div>
          <div className="p-5 space-y-2.5">
            <h3 className="text-sm font-bold text-ink">{cur.title}</h3>
            <p className="text-xs text-ink-muted leading-relaxed">
              {cur.body}
            </p>
          </div>
          <div className="flex items-center gap-2 px-4 py-2.5 border-t border-border-subtle bg-surface-2/40">
            <button
              onClick={() => setStep((s) => Math.max(0, s - 1))}
              disabled={step === 0}
              className={clsx(
                "inline-flex items-center gap-1 h-7 px-2.5 rounded-md text-xs",
                step === 0 ? "text-ink-dim" : "text-ink-muted hover:text-ink hover:bg-surface-2",
              )}
            >
              <ChevronLeft className="w-3 h-3" />
              Back
            </button>
            <div className="flex-1" />
            {last ? (
              <button
                onClick={onClose}
                className="inline-flex items-center gap-1 h-7 px-3 rounded-md text-xs bg-accent text-surface-0 font-semibold hover:bg-accent/90"
              >
                Got it!
              </button>
            ) : (
              <button
                onClick={() => setStep((s) => s + 1)}
                className="inline-flex items-center gap-1 h-7 px-3 rounded-md text-xs bg-accent/15 text-accent hover:bg-accent/25"
              >
                Next
                <ChevronRight className="w-3 h-3" />
              </button>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}
