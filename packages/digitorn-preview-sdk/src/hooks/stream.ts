import { useDigiPreview } from "../DigiPreview.js";
import type {
  ContentBlock,
  ThinkingBlock,
  TextBlock,
  ToolUseBlock,
  CitationBlock,
  ChatAssistantMessage,
} from "../types.js";

// ── Public API ────────────────────────────────────────────────────────

export interface UseStreamApi {
  /** True while a turn is in flight. ``blocks`` belongs to that turn
   *  while ``true``; once the turn settles it freezes onto the tail
   *  ``ChatAssistantMessage`` and ``streaming`` flips to false. */
  streaming: boolean;
  /** All content blocks the assistant has produced so far in the
   *  CURRENT turn, in chronological order. Use this to render rich
   *  typed UIs (collapsible thinking, inline tool widgets, citations).
   *  When idle the array is empty. */
  blocks: ContentBlock[];
  /** Convenience filter: just the thinking blocks. */
  thinking: ThinkingBlock[];
  /** Convenience filter: just the text blocks (the user-facing reply). */
  text: TextBlock[];
  /** Convenience filter: just the tool-use blocks (with their
   *  current ``status: running | done | error``). */
  toolUses: ToolUseBlock[];
  /** Convenience filter: just the citation blocks. */
  citations: CitationBlock[];
  /** Concatenated text content of every ``text`` block - same value
   *  as ``useAgentStream()``. Mirrored here so a single hook covers
   *  the typed view + the plain-text view. */
  textContent: string;
}

function _filterBlocks(blocks: ContentBlock[]): {
  thinking: ThinkingBlock[];
  text: TextBlock[];
  toolUses: ToolUseBlock[];
  citations: CitationBlock[];
} {
  const thinking: ThinkingBlock[] = [];
  const text: TextBlock[] = [];
  const toolUses: ToolUseBlock[] = [];
  const citations: CitationBlock[] = [];
  for (const b of blocks) {
    switch (b.type) {
      case "thinking": thinking.push(b); break;
      case "text": text.push(b); break;
      case "tool_use": toolUses.push(b); break;
      case "citation": citations.push(b); break;
    }
  }
  return { thinking, text, toolUses, citations };
}

/**
 * Live, typed view of the assistant's output for the CURRENT turn.
 *
 * The daemon's response stream interleaves several distinct types of
 * output (chain-of-thought, final answer text, tool calls + results,
 * citations). This hook exposes them as an ordered, chronological
 * array of ``ContentBlock`` so chat UIs can render each kind
 * differently:
 *
 * - ``ThinkingBlock``  → collapsible "Reasoning..." panel
 * - ``TextBlock``      → main chat bubble
 * - ``ToolUseBlock``   → inline "🔧 Calling X" widget with live status
 * - ``CitationBlock``  → clickable source reference
 *
 * ```tsx
 * function StructuredAssistant() {
 *   const { blocks, streaming } = useStream();
 *   return (
 *     <div>
 *       {blocks.map((b, i) => {
 *         if (b.type === "thinking") return <Thinking key={i} {...b} />;
 *         if (b.type === "text") return <Markdown key={i}>{b.content}</Markdown>;
 *         if (b.type === "tool_use") return <ToolWidget key={i} {...b} />;
 *         if (b.type === "citation") return <Cite key={i} {...b} />;
 *       })}
 *       {streaming && <Cursor />}
 *     </div>
 *   );
 * }
 * ```
 *
 * Idle case: ``blocks`` is an empty array, ``streaming`` is false.
 * For the FROZEN history of past turns, read
 * ``useChat().messages[i].blocks`` instead - those are the same
 * ``ContentBlock`` arrays, just settled.
 */
export function useStream(): UseStreamApi {
  const ctx = useDigiPreview();
  const tail = ctx.chatMessages[ctx.chatMessages.length - 1];
  const isStreamingAssistant = (
    tail !== undefined
    && tail.role === "assistant"
    && Boolean(tail.streaming)
  );
  const blocks = isStreamingAssistant
    ? (tail as ChatAssistantMessage).blocks
    : [];
  const filtered = _filterBlocks(blocks);
  return {
    streaming: isStreamingAssistant,
    blocks,
    ...filtered,
    textContent: ctx.agentStream,
  };
}
