/**
 * Host ↔ iframe communication protocol.
 *
 * The "host" is whatever surface embeds the preview iframe (the
 * Digitorn web client, the Flutter desktop client, eventually a
 * VS Code extension panel etc). Apps don't need to know which host
 * is on the other end - they speak the same protocol.
 *
 * Two channels:
 *
 * 1. **URL query params** — set ONCE at iframe load time. Read via
 *    ``readSession()`` and ``readHostTheme()``. Examples:
 *      session_id, token, theme, accent, locale.
 *
 * 2. **postMessage** — bidirectional, real-time. Used for events
 *    that can change after load (theme toggle, abort signal, app
 *    requesting the host to focus a file in the parent editor, etc).
 *
 * All messages are typed and namespaced under ``digi:*`` so the
 * iframe can ignore stray messages from analytics scripts or other
 * embedded widgets.
 */

// ── Host theme (read from URL, mutated by host postMessages) ──────────

export type ThemeMode = "dark" | "light" | "auto";

export interface HostTheme {
  /** Light/dark/auto. ``auto`` = follow OS. */
  mode: ThemeMode;
  /** Brand accent color (hex). Null = use the SDK default. */
  accent: string | null;
  /** BCP-47 language tag (``en``, ``fr``, ``en-US``). */
  locale: string;
}

export const DEFAULT_HOST_THEME: HostTheme = {
  mode: "dark",
  accent: null,
  locale: "en",
};

// ── Message types (sealed, typed, namespaced) ─────────────────────────

/**
 * Messages the HOST sends INTO the iframe.
 *
 * All payloads are JSON-serialisable. The ``type`` discriminator is
 * always namespaced under ``digi:`` so the iframe can filter out
 * stray ``window.message`` events from third-party scripts.
 */
export type ClientBoundMessage =
  | {
      type: "digi:theme-change";
      theme: HostTheme;
    }
  | {
      type: "digi:locale-change";
      locale: string;
    }
  | {
      type: "digi:abort";
      /** When set, the iframe should stop rendering and surface the reason. */
      reason?: string;
    }
  | {
      type: "digi:resize";
      width: number;
      height: number;
    }
  | {
      /**
       * The host is about to elevate or shrink the iframe element
       * (e.g. moving from in-flow gallery slot to ``position: fixed``
       * full-canvas mode). The iframe content (gallery, etc.) follows
       * the iframe element, so its viewport position would change
       * visibly. The SDK counter-shifts its in-flow content by
       * ``offset`` pixels of ``padding-top`` so the gallery stays at
       * the same viewport y while the iframe element moves underneath.
       *
       * Pair: emit with ``offset > 0`` BEFORE elevating, emit with
       * ``offset: 0`` BEFORE shrinking back.
       */
      type: "digi:layout-shift";
      offset: number;
    };

/**
 * Messages the IFRAME sends BACK to the host. The host typically uses
 * these to drive cross-cutting UI (open a file in the parent editor,
 * show a toast, etc).
 */
export type HostBoundMessage =
  | {
      /** Sent once when the iframe content is ready to receive
       *  ``ClientBoundMessage`` events. The host should hold any
       *  pending pushes until this arrives to avoid races. */
      type: "digi:ready";
    }
  | {
      type: "digi:request-open-file";
      path: string;
      line?: number;
      column?: number;
    }
  | {
      type: "digi:request-focus-line";
      path: string;
      line: number;
      column?: number;
    }
  | {
      type: "digi:request-toast";
      message: string;
      level?: "info" | "success" | "warning" | "error";
    }
  | {
      type: "digi:request-navigate";
      /** Internal route inside the host (e.g. session, settings). */
      route: string;
    }
  | {
      /**
       * The iframe's empty-state UI received a template confirmation
       * (user clicked "Use this template"). The host is expected to
       * (1) seed the workspace files via its standard write API and
       * (2) dispatch ``prompt`` as the first user message so the
       * agent's first turn lands. Carrying the payload here lets the
       * iframe stay session-agnostic — sessions are owned by the host.
       */
      type: "digi:template-pick";
      /** Stable id so the host can correlate logs / analytics. */
      template_id: string;
      /** First-turn prompt. Empty string means "no prompt, just seed". */
      prompt: string;
      /** Map of workspace path → raw source. May be empty. */
      seed_files: Record<string, string>;
    }
  | {
      /**
       * The iframe's content height changed — the host should
       * resize the frame to fit so users get the page-level scroll
       * instead of an internal scrollbar. Emit on mount and on every
       * ``ResizeObserver`` tick on ``document.documentElement``.
       *
       * Cross-origin safe: works when the iframe and host live on
       * different origins (Next.js → daemon proxy redirect) where
       * direct ``contentDocument.scrollHeight`` reads are blocked.
       */
      type: "digi:content-resize";
      /** Pixel height the iframe needs to render its content with no scroll. */
      height: number;
    }
  | {
      /**
       * The iframe is opening a full-canvas modal (e.g. the template
       * detail view). The host should elevate the iframe to ``fixed``
       * positioning, ``inset: 0``, top z-index — so the modal covers
       * the host's composer / hero / sidebar instead of being clipped
       * inside the iframe's normal layout slot.
       *
       * Pair with ``digi:modal-close`` on dismiss to restore the
       * iframe's natural flow position.
       */
      type: "digi:modal-open";
    }
  | {
      /** Counterpart to ``digi:modal-open`` — restore normal layout. */
      type: "digi:modal-close";
    };

// ── URL query reader ───────────────────────────────────────────────────

export function readHostTheme(): HostTheme {
  if (typeof window === "undefined") return { ...DEFAULT_HOST_THEME };
  const params = new URLSearchParams(window.location.search);
  const parentParams = _safeReadParentSearch();

  const theme = (params.get("theme") ?? parentParams?.get("theme") ?? "dark") as ThemeMode;
  const accent = params.get("accent") ?? parentParams?.get("accent");
  const locale = params.get("locale") ?? parentParams?.get("locale") ?? "en";

  // Validate ``mode`` against the union; fall back to dark for typos.
  const mode: ThemeMode =
    theme === "light" || theme === "dark" || theme === "auto" ? theme : "dark";

  return {
    mode,
    accent: accent && /^#[0-9a-fA-F]{3,8}$/.test(accent) ? accent : null,
    locale,
  };
}

function _safeReadParentSearch(): URLSearchParams | null {
  try {
    if (window.parent && window.parent !== window) {
      return new URLSearchParams(window.parent.location.search);
    }
  } catch {
    // Cross-origin parent - access blocked, swallow.
  }
  return null;
}

// ── postMessage primitives ────────────────────────────────────────────

const _DIGI_PREFIX = "digi:";

/**
 * Send a message FROM the iframe TO the host (parent window). No-op
 * when there's no parent (running standalone for development).
 *
 * Two bridges, tried in order:
 *
 * 1. **Flutter native channel** — when the SDK runs inside a Flutter
 *    WebView (no iframe nesting, ``window.parent === window``), the
 *    Flutter ``PreviewIframe`` adds a ``DigiHost`` JavaScript channel.
 *    Sending via that channel reaches the Dart side directly.
 *
 * 2. **Browser iframe parent** — the standard postMessage flow used
 *    when the SDK runs inside a real ``<iframe>`` embedded by the
 *    Digitorn web client. Origin is ``"*"`` because the host's origin
 *    is unknown to the iframe; safe because:
 *      - Messages contain no secrets (token is already in URL).
 *      - The host filters by ``digi:`` prefix before acting.
 *      - The iframe is sandboxed by the host's frame attributes.
 */
export function sendToHost(message: HostBoundMessage): void {
  if (typeof window === "undefined") return;

  // Flutter WebView native channel - Dart side adds this on init.
  const flutterChannel = (window as unknown as {
    DigiHost?: { postMessage?: (data: string) => void };
  }).DigiHost;
  if (flutterChannel && typeof flutterChannel.postMessage === "function") {
    try {
      flutterChannel.postMessage(JSON.stringify(message));
      return;
    } catch {
      // Fall through to the parent-frame path below if JSON fails
      // (shouldn't happen for typed HostBoundMessage payloads, but
      // defensive in case a custom message slips through).
    }
  }

  // Browser iframe parent.
  const parent = window.parent;
  if (!parent || parent === window) return;
  try {
    parent.postMessage(message, "*");
  } catch {
    // Defensive: serialisation errors etc.
  }
}

/**
 * Subscribe to messages FROM the host TO the iframe. Filters by the
 * ``digi:`` prefix automatically. The handler receives the typed
 * payload only when the type matches what was requested.
 *
 * Returns an unsubscribe function.
 */
export function onHostMessage<T extends ClientBoundMessage["type"]>(
  type: T,
  handler: (msg: Extract<ClientBoundMessage, { type: T }>) => void,
): () => void {
  if (typeof window === "undefined") return () => {};
  const listener = (ev: MessageEvent) => {
    const data = ev.data;
    if (
      !data ||
      typeof data !== "object" ||
      typeof data.type !== "string" ||
      !data.type.startsWith(_DIGI_PREFIX)
    ) {
      return;
    }
    if (data.type === type) {
      handler(data as Extract<ClientBoundMessage, { type: T }>);
    }
  };
  window.addEventListener("message", listener);
  return () => window.removeEventListener("message", listener);
}

// ── Convenience helpers (most common iframe → host requests) ──────────

/** Ask the host to open ``path`` in its editor (Monaco / etc.). */
export function requestOpenFile(path: string, line?: number, column?: number): void {
  sendToHost({ type: "digi:request-open-file", path, line, column });
}

/** Ask the host to scroll its editor to the given file location. */
export function requestFocusLine(path: string, line: number, column?: number): void {
  sendToHost({ type: "digi:request-focus-line", path, line, column });
}

/** Ask the host to surface a toast / snackbar to the user. */
export function requestToast(
  message: string,
  level: "info" | "success" | "warning" | "error" = "info",
): void {
  sendToHost({ type: "digi:request-toast", message, level });
}

/** Tell the host the iframe is mounted and ready to receive pushes.
 *  Recommended: call once at the top of your app's effect chain. */
export function notifyReady(): void {
  sendToHost({ type: "digi:ready" });
}
