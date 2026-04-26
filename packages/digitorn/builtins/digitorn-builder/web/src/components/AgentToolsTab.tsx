import { Wrench } from "lucide-react";
import clsx from "clsx";
import type { AgentBehaviorProfile } from "../lib/summarize-agent";

interface Props {
  profile: AgentBehaviorProfile;
}

const MODULE_TONE: Record<string, string> = {
  filesystem: "text-kind-module bg-kind-module/10 border-kind-module/30",
  shell: "text-status-warn bg-status-warn/10 border-status-warn/30",
  web: "text-kind-trigger bg-kind-trigger/10 border-kind-trigger/30",
  http: "text-kind-trigger bg-kind-trigger/10 border-kind-trigger/30",
  memory: "text-kind-memory bg-kind-memory/10 border-kind-memory/30",
  agent_spawn: "text-kind-agent bg-kind-agent/10 border-kind-agent/30",
};

export default function AgentToolsTab({ profile }: Props) {
  const total = profile.toolCount;
  if (profile.toolInventory.length === 0) {
    return (
      <div className="p-4 text-xs text-ink-dim italic">
        This agent has no tools available — pure reasoning, no external actions.
      </div>
    );
  }
  return (
    <div className="p-4 space-y-4">
      <div className="text-[10px] uppercase tracking-wider text-ink-dim font-medium flex items-center gap-1.5">
        <Wrench className="w-3 h-3" />
        Total: {total} tool{total > 1 ? "s" : ""} across {profile.toolInventory.length} module{profile.toolInventory.length > 1 ? "s" : ""}
      </div>
      {profile.toolInventory.map((m) => (
        <div key={m.module}>
          <div className={clsx(
            "inline-flex items-center gap-1.5 px-2 py-0.5 rounded-md text-[11px] font-mono font-semibold border",
            MODULE_TONE[m.module] ?? "text-ink-muted bg-surface-2 border-border-subtle",
          )}>
            {m.module}
            <span className="text-[9px] opacity-70">
              {m.actions.length > 0 ? `${m.actions.length} actions` : "all actions"}
            </span>
          </div>
          {m.actions.length > 0 ? (
            <div className="mt-1.5 grid grid-cols-2 gap-1">
              {m.actions.map((a) => (
                <div
                  key={a}
                  className="flex items-center gap-1.5 px-2 py-1 rounded-md bg-surface-2 border border-border-subtle text-[11px] font-mono text-ink-muted"
                >
                  <span className="w-1 h-1 rounded-full bg-status-running flex-shrink-0" />
                  <span className="truncate">{m.module}.{a}</span>
                </div>
              ))}
            </div>
          ) : (
            <div className="mt-1.5 text-[10px] text-ink-dim italic px-2">All module actions inherited.</div>
          )}
        </div>
      ))}

      {profile.affectingHooks.length > 0 && (
        <div className="pt-3 border-t border-border-subtle">
          <div className="text-[10px] uppercase tracking-wider text-ink-dim font-medium mb-2">
            Hooks that may fire ({profile.affectingHooks.length})
          </div>
          <div className="space-y-1.5">
            {profile.affectingHooks.map((h) => (
              <div
                key={h.id}
                className="px-2.5 py-1.5 rounded-lg bg-surface-2 border-l-2 border-l-kind-hook border border-border-subtle"
              >
                <div className="flex items-center gap-2 text-[11px]">
                  <span className="font-mono text-kind-hook font-semibold">{h.id}</span>
                  <span className="text-ink-dim">on</span>
                  <span className="font-mono text-ink">{h.on}</span>
                </div>
                <div className="text-[10px] font-mono text-ink-muted mt-0.5">
                  if {h.condition || "always"} → <span className="text-kind-hook">{h.action}</span>
                </div>
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}
