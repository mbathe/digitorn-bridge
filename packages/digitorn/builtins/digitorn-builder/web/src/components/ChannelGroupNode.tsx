import { memo } from "react";
import { Mail } from "lucide-react";
import clsx from "clsx";
import type { NodeData } from "../lib/yaml-to-graph";

interface Props {
  data: NodeData;
  selected?: boolean;
}

/**
 * Container that wraps a channel + its sub-providers (e.g. an
 * `email` channel can have smtp + sendgrid + mailgun providers,
 * picked by routing_key or round-robin).
 *
 * Same pattern as MCPGroupNode and AgentGroupNode — chrome only,
 * children positioned by lane-layout.
 */
function ChannelGroupNode({ data, selected }: Props) {
  return (
    <div
      className={clsx(
        "relative w-full h-full rounded-2xl transition-colors duration-200",
        selected ? "ring-2 ring-kind-channel ring-offset-2 ring-offset-surface-0" : "",
      )}
      style={{
        background:
          "linear-gradient(180deg, rgb(251 146 60 / 0.06) 0%, rgb(234 88 12 / 0.04) 100%)",
        border: "1px dashed rgb(251 146 60 / 0.45)",
        boxShadow: "inset 0 1px 0 rgb(255 255 255 / 0.03)",
      }}
    >
      <div
        className="absolute -top-[14px] left-4 flex items-center gap-2 px-2.5 py-1 rounded-md text-[10px] font-medium uppercase tracking-wider bg-surface-1 border border-border-subtle"
        style={{ color: "rgb(251, 146, 60)" }}
      >
        <Mail className="w-3 h-3" strokeWidth={2.25} />
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

export default memo(ChannelGroupNode);
