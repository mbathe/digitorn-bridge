import { memo } from "react";
import { Box } from "lucide-react";
import clsx from "clsx";
import type { NodeData } from "../lib/yaml-to-graph";

interface Props {
  data: NodeData;
  selected?: boolean;
}

/**
 * Container that wraps the MCP servers declared under
 * `modules.mcp.config.servers.*`. Acts the same way as the agent-team
 * group node — paints the chrome + header label, while the children
 * are positioned by the lane layout step.
 */
function MCPGroupNode({ data, selected }: Props) {
  return (
    <div
      className={clsx(
        "relative w-full h-full rounded-2xl transition-colors duration-200",
        selected ? "ring-2 ring-kind-module ring-offset-2 ring-offset-surface-0" : "",
      )}
      style={{
        background:
          "linear-gradient(180deg, rgb(125 211 252 / 0.06) 0%, rgb(56 189 248 / 0.04) 100%)",
        border: "1px dashed rgb(125 211 252 / 0.45)",
        boxShadow: "inset 0 1px 0 rgb(255 255 255 / 0.03)",
      }}
    >
      <div
        className="absolute -top-[14px] left-4 flex items-center gap-2 px-2.5 py-1 rounded-md text-[10px] font-medium uppercase tracking-wider bg-surface-1 border border-border-subtle"
        style={{ color: "rgb(125, 211, 252)" }}
      >
        <Box className="w-3 h-3" strokeWidth={2.25} />
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

export default memo(MCPGroupNode);
