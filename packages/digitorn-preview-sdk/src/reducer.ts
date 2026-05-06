import type {
  DigiPreviewContextValue,
  PreviewEvent,
  PreviewSnapshot,
  ToolCall,
  ApprovalRequest,
  ResourceMap,
  ChatMessage,
  ChatUserMessage,
  ChatAssistantMessage,
  ContentBlock,
  TextBlock,
  ThinkingBlock,
  ToolUseBlock,
} from "./types.js";

// ── Block-stream helpers ──────────────────────────────────────────────
//
// Every helper returns a NEW ``ChatAssistantMessage`` with an updated
// ``blocks`` array. We never mutate in place so the React tree picks up
// the change. The streaming assistant message lives at the tail of
// ``chatMessages`` while ``streaming: true``; once ``turn_complete``
// fires we flip the flag and freeze the blocks.

function _ensureStreamingAssistant(
  messages: ChatMessage[],
): { messages: ChatMessage[]; tail: ChatAssistantMessage } {
  const last = messages[messages.length - 1];
  if (last && last.role === "assistant" && last.streaming) {
    return { messages, tail: last };
  }
  const fresh: ChatAssistantMessage = {
    role: "assistant",
    content: "",
    timestamp: Date.now(),
    streaming: true,
    tool_calls: [],
    blocks: [],
  };
  return { messages: [...messages, fresh], tail: fresh };
}

function _replaceTail(
  messages: ChatMessage[], next: ChatAssistantMessage,
): ChatMessage[] {
  return [...messages.slice(0, -1), next];
}

function _concatTextContent(blocks: ContentBlock[]): string {
  let out = "";
  for (const b of blocks) if (b.type === "text") out += b.content;
  return out;
}

function _appendTextDelta(
  tail: ChatAssistantMessage, delta: string,
): ChatAssistantMessage {
  const last = tail.blocks[tail.blocks.length - 1];
  let blocks: ContentBlock[];
  if (last && last.type === "text" && last.streaming) {
    const upd: TextBlock = { ...last, content: last.content + delta };
    blocks = [...tail.blocks.slice(0, -1), upd];
  } else {
    const fresh: TextBlock = {
      type: "text", content: delta, streaming: true,
      timestamp: Date.now(),
    };
    blocks = [...tail.blocks, fresh];
  }
  return { ...tail, blocks, content: _concatTextContent(blocks) };
}

function _appendThinkingDelta(
  tail: ChatAssistantMessage, delta: string, tokens?: number,
): ChatAssistantMessage {
  const last = tail.blocks[tail.blocks.length - 1];
  let blocks: ContentBlock[];
  if (last && last.type === "thinking" && last.streaming) {
    const upd: ThinkingBlock = {
      ...last,
      content: last.content + delta,
      tokens: tokens ?? last.tokens,
    };
    blocks = [...tail.blocks.slice(0, -1), upd];
  } else {
    const fresh: ThinkingBlock = {
      type: "thinking", content: delta, streaming: true,
      tokens, timestamp: Date.now(),
    };
    blocks = [...tail.blocks, fresh];
  }
  return { ...tail, blocks };
}

function _startThinking(tail: ChatAssistantMessage): ChatAssistantMessage {
  // ``thinking_started`` arrives before any deltas. Push an empty
  // thinking block so the UI can flip to "Reasoning..." instantly.
  // Idempotent - if the tail already opens with a streaming thinking
  // block we keep it.
  const last = tail.blocks[tail.blocks.length - 1];
  if (last && last.type === "thinking" && last.streaming) return tail;
  const fresh: ThinkingBlock = {
    type: "thinking", content: "", streaming: true, timestamp: Date.now(),
  };
  return { ...tail, blocks: [...tail.blocks, fresh] };
}

function _appendToolUse(
  tail: ChatAssistantMessage,
  tool: string, params: Record<string, unknown>,
): ChatAssistantMessage {
  const fresh: ToolUseBlock = {
    type: "tool_use",
    tool, params, status: "running",
    timestamp: Date.now(),
  };
  return { ...tail, blocks: [...tail.blocks, fresh] };
}

function _resolveToolUse(
  tail: ChatAssistantMessage, tool: string, result: unknown,
): ChatAssistantMessage {
  // Update the latest matching ``running`` tool_use block. Walk
  // backwards so concurrent calls of the same tool don't cross-resolve.
  for (let i = tail.blocks.length - 1; i >= 0; i -= 1) {
    const b = tail.blocks[i];
    if (b.type === "tool_use" && b.tool === tool && b.status === "running") {
      const isError = (
        typeof result === "object" && result !== null
        && "error" in (result as Record<string, unknown>)
      );
      const upd: ToolUseBlock = {
        ...b, result, status: isError ? "error" : "done",
      };
      return {
        ...tail,
        blocks: [...tail.blocks.slice(0, i), upd, ...tail.blocks.slice(i + 1)],
      };
    }
  }
  return tail;
}

function _freezeStreaming(tail: ChatAssistantMessage): ChatAssistantMessage {
  // Flip every ``streaming: true`` block to false. Run on
  // ``turn_complete`` so the UI knows to stop the typing cursor.
  let touched = false;
  const blocks = tail.blocks.map(b => {
    if (b.type === "text" && b.streaming) { touched = true; return { ...b, streaming: false }; }
    if (b.type === "thinking" && b.streaming) { touched = true; return { ...b, streaming: false }; }
    return b;
  });
  return touched
    ? { ...tail, streaming: false, blocks, content: _concatTextContent(blocks) }
    : { ...tail, streaming: false };
}

export const initialState: DigiPreviewContextValue = {
  connected: false,
  resources: new Map(),
  state: {},
  agentStatus: "idle",
  agentStream: "",
  toolCalls: [],
  approvals: [],
  approvalRequest: null,
  chatMessages: [],
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
  | { type: "approval_resolved"; request_id?: string }
  | { type: "approvals_set"; approvals: ApprovalRequest[] }
  | { type: "agent_thinking_started" }
  | { type: "agent_thinking_delta"; delta: string; tokens?: number }
  | {
      type: "chat_user_message";
      content: string;
      images?: string[];
      correlation_id?: string;
      pending?: boolean;
    }
  | {
      type: "chat_tool_message";
      tool_call_id: string;
      tool: string;
      content: string;
    }
  | { type: "chat_messages_set"; messages: ChatMessage[] }
  | { type: "chat_clear" };

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
      // ``events`` and ``seq`` come from the Socket.IO snapshot envelope.
      // The HTTP one-shot route ``GET /sessions/{sid}/preview`` returns a
      // pure {state, resources} shape without those metadata fields, so
      // we fall back to current state's values rather than crash on
      // ``[...undefined]`` / ``undefined assigned to seq``.
      return {
        ...state,
        connected: true,
        state: { ...(s.state || {}) },
        resources,
        events: Array.isArray(s.events) ? [...s.events] : state.events,
        seq: typeof s.seq === "number" ? s.seq : state.seq,
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

    case "agent_token": {
      // Stream a text delta. The reducer handles BOTH the legacy
      // ``agentStream`` accumulator AND the new typed ``blocks`` array
      // on the streaming assistant message. Existing UIs reading
      // ``agentStream`` keep working; new ones map ``blocks`` for
      // structured rendering.
      const nextStream = state.agentStream + action.content;
      const ensured = _ensureStreamingAssistant(state.chatMessages);
      const updated = _appendTextDelta(ensured.tail, action.content);
      return {
        ...state,
        agentStatus: "working",
        agentStream: nextStream,
        chatMessages: _replaceTail(ensured.messages, updated),
      };
    }

    case "agent_thinking":
      // Coarse status flag - the dedicated thinking_started action
      // below opens the typed block. Kept for backward compat with
      // status-only consumers.
      return { ...state, agentStatus: "thinking" };

    case "agent_thinking_started": {
      const ensured = _ensureStreamingAssistant(state.chatMessages);
      const updated = _startThinking(ensured.tail);
      return {
        ...state,
        agentStatus: "thinking",
        chatMessages: _replaceTail(ensured.messages, updated),
      };
    }

    case "agent_thinking_delta": {
      const ensured = _ensureStreamingAssistant(state.chatMessages);
      const updated = _appendThinkingDelta(
        ensured.tail, action.delta, action.tokens,
      );
      return {
        ...state,
        agentStatus: "thinking",
        chatMessages: _replaceTail(ensured.messages, updated),
      };
    }

    case "agent_tool_start": {
      const call: ToolCall = {
        tool: action.tool,
        params: action.params,
        timestamp: Date.now(),
      };
      const toolCalls = [...state.toolCalls, call].slice(-50);
      const ensured = _ensureStreamingAssistant(state.chatMessages);
      const withBlock = _appendToolUse(ensured.tail, action.tool, action.params);
      // Mirror onto legacy ``tool_calls`` array for older UIs.
      const updated: ChatAssistantMessage = {
        ...withBlock,
        tool_calls: [...(withBlock.tool_calls ?? []), call],
      };
      return {
        ...state,
        agentStatus: "working",
        toolCalls,
        chatMessages: _replaceTail(ensured.messages, updated),
      };
    }

    case "agent_tool_done": {
      const toolCalls = state.toolCalls.map((c) =>
        c.tool === action.tool && !c.result
          ? { ...c, result: action.result }
          : c
      );
      const tail = state.chatMessages[state.chatMessages.length - 1];
      let chatMessages = state.chatMessages;
      if (tail && tail.role === "assistant" && tail.streaming) {
        const withBlock = _resolveToolUse(tail, action.tool, action.result);
        const updatedCalls = (withBlock.tool_calls ?? []).map(c =>
          c.tool === action.tool && !c.result
            ? { ...c, result: action.result }
            : c,
        );
        const updated: ChatAssistantMessage = {
          ...withBlock,
          tool_calls: updatedCalls,
        };
        chatMessages = _replaceTail(state.chatMessages, updated);
      }
      return { ...state, toolCalls, chatMessages };
    }

    case "agent_turn_complete": {
      // Freeze the streaming assistant tail (blocks too).
      const tail = state.chatMessages[state.chatMessages.length - 1];
      let chatMessages = state.chatMessages;
      if (tail && tail.role === "assistant" && tail.streaming) {
        const finalised = _freezeStreaming(tail);
        chatMessages = _replaceTail(state.chatMessages, finalised);
      }
      // Flip the most recent pending user message → settled.
      for (let i = chatMessages.length - 1; i >= 0; i -= 1) {
        const m = chatMessages[i];
        if (m.role === "user" && m.pending) {
          const settled: ChatUserMessage = { ...m, pending: false };
          chatMessages = [
            ...chatMessages.slice(0, i),
            settled,
            ...chatMessages.slice(i + 1),
          ];
          break;
        }
      }
      return {
        ...state,
        agentStatus: "idle",
        agentStream: "",
        chatMessages,
      };
    }

    case "agent_abort": {
      // Drop the streaming tail entirely (partial blocks are
      // misleading - the agent never confirmed them).
      const tail = state.chatMessages[state.chatMessages.length - 1];
      let chatMessages = state.chatMessages;
      if (tail && tail.role === "assistant" && tail.streaming) {
        chatMessages = state.chatMessages.slice(0, -1);
      }
      return {
        ...state,
        agentStatus: "idle",
        agentStream: "",
        chatMessages,
      };
    }

    case "approval_request": {
      // Append-or-replace by request_id so duplicate emits (heartbeats,
      // reconnect replays) don't stack copies of the same request.
      const rid = action.request.request_id;
      const others = state.approvals.filter(r => r.request_id !== rid);
      const approvals = [...others, action.request];
      return {
        ...state,
        approvals,
        approvalRequest: approvals[0] ?? null,
      };
    }

    case "approval_resolved": {
      // Without a request_id we conservatively drop the head of the
      // queue (legacy behaviour). With one we surgically remove the
      // matching entry, so a multi-pending UI keeps the rest visible.
      const rid = action.request_id;
      const approvals = rid
        ? state.approvals.filter(r => r.request_id !== rid)
        : state.approvals.slice(1);
      return {
        ...state,
        approvals,
        approvalRequest: approvals[0] ?? null,
      };
    }

    case "approvals_set":
      // Wholesale replacement - used by the HTTP-snapshot warmup so a
      // session that already had pending approvals before the iframe
      // connected sees them immediately.
      return {
        ...state,
        approvals: action.approvals,
        approvalRequest: action.approvals[0] ?? null,
      };

    case "chat_user_message": {
      // De-dup by ``correlation_id``: if a user message with that id
      // is already present (typically the optimistic insert added
      // when ``send()`` was called), update it in place rather than
      // appending a duplicate when the daemon's ``user_message``
      // event lands.
      const cid = action.correlation_id;
      const idx = cid
        ? state.chatMessages.findIndex(
            m => m.role === "user" && m.correlation_id === cid,
          )
        : -1;
      const entry: ChatUserMessage = {
        role: "user",
        content: action.content,
        images: action.images,
        timestamp: Date.now(),
        correlation_id: cid,
        pending: action.pending ?? true,
      };
      const next = idx >= 0
        ? [
            ...state.chatMessages.slice(0, idx),
            entry,
            ...state.chatMessages.slice(idx + 1),
          ]
        : [...state.chatMessages, entry];
      return { ...state, chatMessages: next };
    }

    case "chat_tool_message": {
      const next: ChatMessage = {
        role: "tool",
        tool_call_id: action.tool_call_id,
        tool: action.tool,
        content: action.content,
        timestamp: Date.now(),
      };
      return { ...state, chatMessages: [...state.chatMessages, next] };
    }

    case "chat_messages_set":
      return { ...state, chatMessages: action.messages };

    case "chat_clear":
      return { ...state, chatMessages: [] };

    default:
      return state;
  }
}
