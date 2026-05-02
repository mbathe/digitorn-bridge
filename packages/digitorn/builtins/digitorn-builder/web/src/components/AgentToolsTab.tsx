import { Wrench } from "lucide-react";
import clsx from "clsx";
import type { AgentBehaviorProfile } from "../lib/summarize-agent";
import { humanizeCondition, humanizeAction, humanizeEvent } from "../lib/humanize-hook";
import type { ParsedYaml } from "../lib/yaml-to-graph";

interface Props {
  profile: AgentBehaviorProfile;
  /** Whole parsed doc — used to compute per-action wiring (which
   *  hooks fire on each tool, which actions need approval). Passing
   *  null disables the per-action annotations. */
  doc?: ParsedYaml | null;
}

/** For a given module + action, find which hooks intercept it and
 *  whether it needs HITL approval. Computed from doc.runtime.hooks
 *  + doc.tools?.capabilities.approve. */
function lookupActionWiring(
  doc: ParsedYaml | null | undefined,
  mod: string,
  action: string,
): { hookIds: string[]; gateBlocks: string[]; needsApproval: boolean } {
  const out = { hookIds: [] as string[], gateBlocks: [] as string[], needsApproval: false };
  if (!doc) return out;
  const fqn = `${mod}.${action}`;
  // Approval check
  const approves = (doc.tools?.capabilities?.approve ?? []) as Array<{ module?: string; actions?: string[] }>;
  for (const a of approves) {
    if (a.module === mod && (a.actions ?? []).includes(action)) {
      out.needsApproval = true;
      break;
    }
  }
  // Hook intercepts — same shape extraction as extra-nodes.ts
  const hooks = (doc.runtime?.hooks ?? []) as Array<Record<string, unknown>>;
  for (const h of hooks) {
    const cond = h.condition as { equals?: string; tool_name?: string | string[]; in?: string[]; type?: string } | undefined;
    const tools: string[] = [];
    if (typeof cond?.equals === "string") tools.push(cond.equals);
    if (typeof cond?.tool_name === "string") tools.push(cond.tool_name);
    if (Array.isArray(cond?.tool_name)) tools.push(...(cond.tool_name as string[]));
    if (Array.isArray(cond?.in)) tools.push(...(cond.in as string[]));
    if (!tools.includes(fqn)) continue;
    const id = (h.id as string | undefined) ?? "(unnamed)";
    out.hookIds.push(id);
    const actType = (h.action as { type?: string } | undefined)?.type;
    if (actType === "gate") out.gateBlocks.push(id);
  }
  return out;
}

const MODULE_TONE: Record<string, string> = {
  filesystem: "text-kind-module bg-kind-module/10 border-kind-module/30",
  shell: "text-status-warn bg-status-warn/10 border-status-warn/30",
  web: "text-kind-trigger bg-kind-trigger/10 border-kind-trigger/30",
  http: "text-kind-trigger bg-kind-trigger/10 border-kind-trigger/30",
  memory: "text-kind-memory bg-kind-memory/10 border-kind-memory/30",
  agent_spawn: "text-kind-agent bg-kind-agent/10 border-kind-agent/30",
};

export default function AgentToolsTab({ profile, doc }: Props) {
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
              {m.actions.map((a) => {
                const wiring = lookupActionWiring(doc, m.module, a);
                const blocked = wiring.gateBlocks.length > 0;
                const tooltipParts: string[] = [`${m.module}.${a}`];
                if (wiring.needsApproval) tooltipParts.push("• needs human approval");
                if (wiring.hookIds.length > 0) tooltipParts.push(`• fires: ${wiring.hookIds.join(", ")}`);
                if (blocked) tooltipParts.push(`• BLOCKED by: ${wiring.gateBlocks.join(", ")}`);
                return (
                  <div
                    key={a}
                    className={clsx(
                      "flex items-center gap-1.5 px-2 py-1 rounded-md border text-[11px] font-mono",
                      blocked
                        ? "bg-status-error/10 border-status-error/40 text-status-error line-through"
                        : "bg-surface-2 border-border-subtle text-ink-muted",
                    )}
                    title={tooltipParts.join("\n")}
                  >
                    <span className={clsx(
                      "w-1 h-1 rounded-full flex-shrink-0",
                      blocked ? "bg-status-error" : "bg-status-running",
                    )} />
                    <span className="truncate flex-1">{a}</span>
                    {wiring.needsApproval && (
                      <span className="text-[9px] text-status-warn" title="Requires human approval">🔒</span>
                    )}
                    {wiring.hookIds.length > 0 && !blocked && (
                      <span className="text-[9px] text-kind-hook" title={`Fires: ${wiring.hookIds.join(", ")}`}>
                        ↺{wiring.hookIds.length}
                      </span>
                    )}
                  </div>
                );
              })}
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
            {profile.affectingHooks.map((h) => {
              // The condition + action come from summarize-agent as raw
              // YAML sub-trees serialised; parse + humanize on display.
              const condRaw = h.condition;
              const actRaw = h.action;
              let condParsed: unknown = condRaw;
              let actParsed: unknown = actRaw;
              if (typeof condRaw === "string") {
                try { condParsed = JSON.parse(condRaw); } catch { /* leave as string */ }
              }
              if (typeof actRaw === "string") {
                try { actParsed = JSON.parse(actRaw); } catch { /* leave as string */ }
              }
              const condText = humanizeCondition(condParsed);
              const act = humanizeAction(actParsed);
              return (
                <div
                  key={h.id}
                  className="px-2.5 py-1.5 rounded-lg bg-surface-2 border-l-2 border-l-kind-hook border border-border-subtle"
                >
                  <div className="flex items-center gap-2 text-[11px]">
                    <span className="font-mono text-kind-hook font-semibold">{h.id}</span>
                    <span className="text-ink-dim">on</span>
                    <span className="font-mono text-ink">{humanizeEvent(h.on)}</span>
                  </div>
                  <div className="text-[10px] text-ink-muted mt-1 leading-snug">
                    <span className="text-ink-dim">when</span>{" "}
                    <span className="font-mono">{condText}</span>{" → "}
                    <span className="text-kind-hook font-mono">{act.label}</span>
                  </div>
                  {act.detail && (
                    <div className="text-[10px] text-ink-dim italic mt-0.5">{act.detail}</div>
                  )}
                </div>
              );
            })}
          </div>
        </div>
      )}
    </div>
  );
}
