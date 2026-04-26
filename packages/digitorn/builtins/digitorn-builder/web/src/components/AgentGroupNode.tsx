import { memo } from "react";
import { Users } from "lucide-react";
import clsx from "clsx";
import type { NodeData } from "../lib/yaml-to-graph";

interface Props {
  data: NodeData;
  selected?: boolean;
}

/**
 * Renders the translucent container that wraps a team of related agents
 * (entry agent + its spawned specialists). Children are positioned by
 * dagre's compound layout pass — this component only paints the chrome.
 */
function AgentGroupNode({ data, selected }: Props) {
  return (
    <div
      className={clsx(
        "relative w-full h-full",
        "rounded-2xl",
        "transition-colors duration-200",
        selected
          ? "ring-2 ring-kind-agent ring-offset-2 ring-offset-surface-0"
          : "",
      )}
      style={{
        background:
          "linear-gradient(180deg, rgb(192 132 252 / 0.06) 0%, rgb(168 85 247 / 0.04) 100%)",
        border: "1px dashed rgb(var(--kind-agent) / 0.45)",
        boxShadow: "inset 0 1px 0 rgb(255 255 255 / 0.03)",
      }}
    >
      {/* Header strip — sits above the children, above the dashed border */}
      <div
        className="absolute -top-[14px] left-4 flex items-center gap-2 px-2.5 py-1 rounded-md text-[10px] font-medium uppercase tracking-wider bg-surface-1 border border-border-subtle"
        style={{ color: "rgb(var(--kind-agent))" }}
      >
        <Users className="w-3 h-3" strokeWidth={2.25} />
        <span>{data.label}</span>
        {data.subtitle && (
          <span className="text-ink-dim normal-case font-mono text-[10px]">
            · {data.subtitle}
          </span>
        )}
      </div>
    </div>
  );
}

export default memo(AgentGroupNode);
