import type {
  DigiPreviewContextValue,
  PreviewEvent,
  PreviewSnapshot,
  ToolCall,
  ApprovalRequest,
  ResourceMap,
} from "./types.js";

export const initialState: DigiPreviewContextValue = {
  connected: false,
  resources: new Map(),
  state: {},
  agentStatus: "idle",
  agentStream: "",
  toolCalls: [],
  approvalRequest: null,
  events: [],
  seq: 0,
};

export type ReducerAction =
  | { type: "connected" }
  | { type: "disconnected" }
  | { type: "snapshot"; payload: PreviewSnapshot }
  | { type: "preview_delta"; payload: PreviewEvent }
  | { type: "agent_token"; content: string }
  | { type: "agent_thinking" }
  | { type: "agent_tool_start"; tool: string; params: Record<string, unknown> }
  | { type: "agent_tool_done"; tool: string; result: Record<string, unknown> }
  | { type: "agent_turn_complete" }
  | { type: "agent_abort" }
  | { type: "approval_request"; request: ApprovalRequest }
  | { type: "approval_resolved" };

function cloneResources(
  src: Map<string, ResourceMap>,
  channelToCopy?: string,
): Map<string, ResourceMap> {
  const out = new Map<string, ResourceMap>();
  for (const [name, items] of src) {
    out.set(name, name === channelToCopy ? new Map(items) : items);
  }
  return out;
}

function getOrInitChannel(
  resources: Map<string, ResourceMap>,
  name: string,
): { resources: Map<string, ResourceMap>; channel: ResourceMap } {
  const out = cloneResources(resources, name);
  let ch = out.get(name);
  if (!ch) {
    ch = new Map();
    out.set(name, ch);
  }
  return { resources: out, channel: ch };
}

export function reducer(
  state: DigiPreviewContextValue,
  action: ReducerAction,
): DigiPreviewContextValue {
  switch (action.type) {

    case "connected":
      return { ...state, connected: true };

    case "disconnected":
      return { ...state, connected: false };

    // ── Preview events ──────────────────────────────────────────────

    case "snapshot": {
      const s = action.payload;
      const resources = new Map<string, ResourceMap>();
      for (const [name, items] of Object.entries(s.resources || {})) {
        const ch: ResourceMap = new Map();
        for (const [id, payload] of Object.entries(items)) ch.set(id, payload);
        resources.set(name, ch);
      }
      if (!resources.has("nodes") && Array.isArray(s.nodes)) {
        const ch: ResourceMap = new Map();
        for (const n of s.nodes) ch.set(n.id, n as unknown as Record<string, unknown>);
        resources.set("nodes", ch);
      }
      if (!resources.has("edges") && Array.isArray(s.edges)) {
        const ch: ResourceMap = new Map();
        for (const e of s.edges) ch.set(e.id, e as unknown as Record<string, unknown>);
        resources.set("edges", ch);
      }
      return {
        ...state,
        connected: true,
        state: { ...s.state },
        resources,
        events: [...s.events],
        seq: s.seq,
      };
    }

    case "preview_delta": {
      const d = action.payload;
      if (d.seq <= state.seq) return state;

      let stateMap = state.state;
      let resources = state.resources;
      const events = [...state.events, d];
      if (events.length > 500) events.splice(0, events.length - 500);

      switch (d.event_type) {
        case "state_changed": {
          const { key, value } = d.data as { key: string; value: unknown };
          stateMap = { ...stateMap, [key]: value };
          break;
        }
        case "state_patched": {
          const { patch } = d.data as { patch: Record<string, unknown> };
          stateMap = { ...stateMap, ...patch };
          break;
        }
        case "cleared":
          stateMap = {};
          resources = new Map();
          break;
        case "channel_cleared": {
          const { channel } = d.data as { channel: string };
          const out = getOrInitChannel(resources, channel);
          out.channel.clear();
          resources = out.resources;
          break;
        }
        case "resource_set":
        case "resource_patched": {
          const { channel, id, payload } = d.data as {
            channel: string; id: string; payload: Record<string, unknown>;
          };
          const out = getOrInitChannel(resources, channel);
          out.channel.set(id, payload);
          resources = out.resources;
          break;
        }
        case "resource_deleted": {
          const { channel, id } = d.data as { channel: string; id: string };
          if (resources.get(channel)?.has(id)) {
            const out = getOrInitChannel(resources, channel);
            out.channel.delete(id);
            resources = out.resources;
          }
          break;
        }
        case "resource_bulk_set": {
          const { channel, items, replace } = d.data as {
            channel: string;
            items: Record<string, Record<string, unknown>>;
            replace?: boolean;
          };
          const out = getOrInitChannel(resources, channel);
          if (replace) out.channel.clear();
          for (const [id, payload] of Object.entries(items)) out.channel.set(id, payload);
          resources = out.resources;
          break;
        }
      }
      return { ...state, state: stateMap, resources, events, seq: d.seq };
    }

    // ── Agent events ────────────────────────────────────────────────

    case "agent_token":
      return {
        ...state,
        agentStatus: "working",
        agentStream: state.agentStream + action.content,
      };

    case "agent_thinking":
      return { ...state, agentStatus: "thinking" };

    case "agent_tool_start": {
      const call: ToolCall = {
        tool: action.tool,
        params: action.params,
        timestamp: Date.now(),
      };
      const toolCalls = [...state.toolCalls, call].slice(-50);
      return { ...state, agentStatus: "working", toolCalls };
    }

    case "agent_tool_done": {
      const toolCalls = state.toolCalls.map((c) =>
        c.tool === action.tool && !c.result
          ? { ...c, result: action.result }
          : c
      );
      return { ...state, toolCalls };
    }

    case "agent_turn_complete":
      return { ...state, agentStatus: "idle", agentStream: "" };

    case "agent_abort":
      return { ...state, agentStatus: "idle", agentStream: "" };

    case "approval_request":
      return { ...state, approvalRequest: action.request };

    case "approval_resolved":
      return { ...state, approvalRequest: null };

    default:
      return state;
  }
}
