import { useReactFlow } from "reactflow";
import { Wrench } from "lucide-react";
import clsx from "clsx";

interface Props {
  agentId: string;
  toolName: string;
  args?: unknown;
}

/**
 * Floating bubble pinned to the active agent node. Renders the in-flight
 * tool name + a 1-line preview of args. Position is computed live from
 * the ReactFlow viewport so the bubble follows pan/zoom.
 */
export default function ToolCallBubble({ agentId, toolName, args }: Props) {
  const rf = useReactFlow();
  const node = rf.getNode(`agent-${agentId}`);
  if (!node?.position) return null;

  // Position : 12 px above the node's top edge, horizontally centered.
  const left = node.position.x + (node.width ?? 240) / 2;
  const top = node.position.y - 8;

  const argPreview = previewArgs(args);

  return (
    <div
      className={clsx(
        "react-flow__panel pointer-events-none",
        "absolute z-50 -translate-x-1/2 -translate-y-full",
      )}
      style={{ left, top, position: "absolute" }}
    >
      <div className="flex items-center gap-2 px-2.5 py-1.5 rounded-lg bg-status-running/95 text-white shadow-node-active backdrop-blur-sm border border-status-running text-xs animate-pulse">
        <Wrench className="w-3.5 h-3.5" strokeWidth={2.5} />
        <span className="font-mono font-semibold">{toolName}</span>
        {argPreview && (
          <span className="font-mono text-white/70 max-w-[260px] truncate">
            ({argPreview})
          </span>
        )}
      </div>
    </div>
  );
}

function previewArgs(args: unknown): string {
  if (!args) return "";
  if (typeof args !== "object") return String(args).slice(0, 80);
  try {
    const obj = args as Record<string, unknown>;
    const parts: string[] = [];
    for (const [k, v] of Object.entries(obj)) {
      const sv = typeof v === "string" ? v : JSON.stringify(v);
      parts.push(`${k}=${sv?.slice(0, 30)}`);
      if (parts.join(", ").length > 80) break;
    }
    return parts.join(", ");
  } catch {
    return "";
  }
}
