/**
 * Visual editor for the ``features:`` block (UI feature toggles).
 *
 * Schema: ``AppDefinition.features: dict[str, bool]``. Documented in
 * ``docs/app-language/44-client-manifest.md``. The Flutter client
 * defaults every key to ``true`` when unset, so an empty dict means
 * "everything visible". Setting a key to ``false`` hides that surface.
 *
 * Rather than a flat dict editor, we group the 12 known keys by which
 * surface they affect: chat (compose row + message body), side panels,
 * status bar. Unknown keys are rendered at the bottom under "Other"
 * so user-defined custom flags still round-trip.
 */
import { useMemo } from "react";
import {
  Mic, Paperclip, Wrench, Type, ChevronRight, Bot, Activity,
  ListTodo, Brain, Gauge, Hash, Eye, EyeOff, Layers, MessageSquare,
} from "lucide-react";
import clsx from "clsx";

interface Props {
  raw: Record<string, boolean>;
  basePath: string;
  onEdit: (absolutePath: string, value: unknown) => void;
  onDelete: (absolutePath: string) => void;
}

interface FeatureSpec {
  key: string;
  label: string;
  hint: string;
  icon: typeof Mic;
}

interface FeatureGroup {
  label: string;
  icon: typeof MessageSquare;
  hint: string;
  features: FeatureSpec[];
}

const FEATURE_GROUPS: readonly FeatureGroup[] = [
  {
    label: "Chat surface",
    icon: MessageSquare,
    hint: "Controls the compose row and per-message rendering.",
    features: [
      { key: "voice", label: "Voice input", hint: "Microphone button in the compose row.", icon: Mic },
      { key: "attachments", label: "Attachments", hint: "Paperclip / file upload menu.", icon: Paperclip },
      { key: "slash_commands", label: "Slash commands", hint: "/ palette for skill commands.", icon: ChevronRight },
      { key: "markdown", label: "Markdown rendering", hint: "Rich text. False = plain text.", icon: Type },
      { key: "message_actions", label: "Message actions", hint: "Copy / retry / copy-markdown buttons.", icon: ListTodo },
    ],
  },
  {
    label: "Side panels",
    icon: Layers,
    hint: "Hideable drawers anchored to the chat layout.",
    features: [
      { key: "tools_panel", label: "Tools panel", hint: "Tools button + drawer.", icon: Wrench },
      { key: "snippets", label: "Snippets", hint: "Snippets menu.", icon: Hash },
      { key: "tasks_panel", label: "Tasks panel", hint: "Background tasks drawer.", icon: Bot },
      { key: "memory_panel", label: "Memory panel", hint: "Memory drawer (goal + remembered facts).", icon: Brain },
    ],
  },
  {
    label: "Status indicators",
    icon: Activity,
    hint: "Visual cues about the current session and context.",
    features: [
      { key: "context_ring", label: "Context ring", hint: "Pressure gauge - color tracks compaction headroom.", icon: Gauge },
      { key: "status_pills", label: "Status pills", hint: "Live / Reconnecting / Interrupted badges.", icon: Activity },
      { key: "token_badges", label: "Token badges", hint: "Per-message token footer.", icon: Hash },
    ],
  },
] as const;

const KNOWN_KEYS = new Set<string>(
  FEATURE_GROUPS.flatMap((g) => g.features.map((f) => f.key)),
);

export default function FeaturesToggleGrid({ raw, basePath, onEdit, onDelete }: Props) {
  const safeRaw = raw ?? {};
  const customKeys = useMemo(
    () => Object.keys(safeRaw).filter((k) => !KNOWN_KEYS.has(k)),
    [safeRaw],
  );

  const setFeature = (key: string, visible: boolean) => {
    if (visible) {
      // visible = default; remove the explicit `false` so the YAML
      // stays minimal.
      if (key in safeRaw) onDelete(`${basePath}.${key}`);
    } else {
      onEdit(`${basePath}.${key}`, false);
    }
  };

  const isVisible = (key: string): boolean => {
    if (!(key in safeRaw)) return true; // default
    return safeRaw[key] !== false;
  };

  const isExplicit = (key: string): boolean => key in safeRaw;

  return (
    <div className="space-y-4">
      <div className="rounded-md bg-surface-2 border border-border-subtle p-2.5 text-[11px] text-ink-muted">
        Every feature defaults to <span className="font-mono text-ink">visible</span>.
        Click to <span className="font-mono">hide</span>; click again to revert to default.
      </div>

      {FEATURE_GROUPS.map((group) => {
        const GroupIcon = group.icon;
        return (
          <div key={group.label} className="space-y-1.5">
            <div className="flex items-center gap-1.5">
              <GroupIcon className="w-3.5 h-3.5 text-accent" />
              <span className="text-[11px] font-medium text-ink">{group.label}</span>
            </div>
            <div className="grid grid-cols-2 gap-1">
              {group.features.map((f) => {
                const Icon = f.icon;
                const visible = isVisible(f.key);
                const explicit = isExplicit(f.key);
                return (
                  <button
                    key={f.key}
                    onClick={() => setFeature(f.key, !visible)}
                    title={`${f.hint}${explicit ? " (explicit override)" : " (default)"}`}
                    className={clsx(
                      "flex items-center gap-2 px-2 py-1.5 rounded-md text-[11px] text-left transition-colors group",
                      visible
                        ? "bg-surface-2 text-ink hover:bg-surface-3"
                        : "bg-status-error/10 text-status-error/90 ring-1 ring-status-error/20 hover:bg-status-error/15",
                    )}
                  >
                    {visible ? (
                      <Eye className="w-3 h-3 flex-shrink-0 text-accent" />
                    ) : (
                      <EyeOff className="w-3 h-3 flex-shrink-0" />
                    )}
                    <Icon className={clsx(
                      "w-3 h-3 flex-shrink-0",
                      visible ? "text-ink-muted" : "text-status-error/60",
                    )} />
                    <span className="font-medium truncate flex-1">{f.label}</span>
                    {explicit && (
                      <span className="text-[9px] uppercase tracking-wider text-ink-dim font-mono">
                        set
                      </span>
                    )}
                  </button>
                );
              })}
            </div>
          </div>
        );
      })}

      {customKeys.length > 0 && (
        <div className="space-y-1.5 pt-2 border-t border-border-subtle">
          <div className="flex items-center gap-1.5">
            <Hash className="w-3.5 h-3.5 text-ink-dim" />
            <span className="text-[11px] font-medium text-ink-muted">Other (custom)</span>
          </div>
          <div className="grid grid-cols-2 gap-1">
            {customKeys.map((k) => {
              const visible = safeRaw[k] !== false;
              return (
                <button
                  key={k}
                  onClick={() => onEdit(`${basePath}.${k}`, !visible)}
                  className={clsx(
                    "flex items-center gap-2 px-2 py-1.5 rounded-md text-[11px] text-left transition-colors",
                    visible
                      ? "bg-surface-2 text-ink hover:bg-surface-3"
                      : "bg-status-error/10 text-status-error/90 ring-1 ring-status-error/20",
                  )}
                >
                  {visible ? (
                    <Eye className="w-3 h-3 flex-shrink-0 text-accent" />
                  ) : (
                    <EyeOff className="w-3 h-3 flex-shrink-0" />
                  )}
                  <span className="font-mono truncate flex-1">{k}</span>
                </button>
              );
            })}
          </div>
        </div>
      )}
    </div>
  );
}
