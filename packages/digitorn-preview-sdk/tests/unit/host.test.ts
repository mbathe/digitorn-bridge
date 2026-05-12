import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  DEFAULT_HOST_THEME,
  notifyReady,
  onHostMessage,
  readHostTheme,
  requestFocusLine,
  requestOpenFile,
  requestToast,
  sendToHost,
} from "../../src/host.js";

/**
 * Host postMessage / theme protocol regression suite.
 *
 * Two surfaces:
 *   - URL-driven theme read at boot (``readHostTheme``)
 *   - bidirectional postMessage (``sendToHost`` / ``onHostMessage``)
 *
 * Tests live entirely in happy-dom — no parent frame, no Flutter
 * runtime — so we drive both with explicit fakes.
 */

// ── URL theme ────────────────────────────────────────────────────────────

describe("readHostTheme", () => {
  afterEach(() => {
    // Reset to bare path so the next test starts query-clean. Use a
    // relative URL — happy-dom rejects cross-origin pushState.
    window.history.replaceState({}, "", "/");
  });

  it("returns the default theme when no query params are present", () => {
    expect(readHostTheme()).toEqual(DEFAULT_HOST_THEME);
  });

  it("parses valid theme + accent + locale", () => {
    window.history.replaceState({}, "", "/?theme=light&accent=%23ff00aa&locale=fr-FR");
    expect(readHostTheme()).toEqual({
      mode: "light",
      accent: "#ff00aa",
      locale: "fr-FR",
    });
  });

  it("falls back to dark for an unknown theme value", () => {
    window.history.replaceState({}, "", "/?theme=neon");
    expect(readHostTheme().mode).toBe("dark");
  });

  it("rejects an accent that isn't a hex literal", () => {
    window.history.replaceState({}, "", "/?accent=red");
    expect(readHostTheme().accent).toBeNull();
  });

  it("accepts the three-digit hex form (#fff)", () => {
    window.history.replaceState({}, "", "/?accent=%23fff");
    expect(readHostTheme().accent).toBe("#fff");
  });
});

// ── sendToHost ───────────────────────────────────────────────────────────

describe("sendToHost", () => {
  let originalParentDescriptor: PropertyDescriptor | undefined;
  let postMessageSpy: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    originalParentDescriptor = Object.getOwnPropertyDescriptor(window, "parent");
    postMessageSpy = vi.fn();
    Object.defineProperty(window, "parent", {
      configurable: true,
      get: () => ({ postMessage: postMessageSpy }),
    });
  });

  afterEach(() => {
    if (originalParentDescriptor) {
      Object.defineProperty(window, "parent", originalParentDescriptor);
    } else {
      delete (window as any).parent;
    }
    delete (window as any).DigiHost;
  });

  it("forwards to window.parent.postMessage with origin '*'", () => {
    sendToHost({ type: "digi:modal-open" });
    expect(postMessageSpy).toHaveBeenCalledOnce();
    expect(postMessageSpy).toHaveBeenCalledWith({ type: "digi:modal-open" }, "*");
  });

  it("prefers Flutter DigiHost channel when present (JSON-serialised)", () => {
    const flutterSpy = vi.fn();
    (window as any).DigiHost = { postMessage: flutterSpy };
    sendToHost({ type: "digi:content-resize", height: 480 });
    expect(flutterSpy).toHaveBeenCalledOnce();
    expect(flutterSpy).toHaveBeenCalledWith(JSON.stringify({ type: "digi:content-resize", height: 480 }));
    expect(postMessageSpy).not.toHaveBeenCalled();
  });

  it("falls back to parent.postMessage when Flutter channel is missing", () => {
    delete (window as any).DigiHost;
    sendToHost({ type: "digi:ready" });
    expect(postMessageSpy).toHaveBeenCalledWith({ type: "digi:ready" }, "*");
  });

  it("is a no-op when parent === window (standalone dev mode)", () => {
    Object.defineProperty(window, "parent", { configurable: true, get: () => window });
    expect(() => sendToHost({ type: "digi:ready" })).not.toThrow();
    // (postMessageSpy not on parent here — assertion is just "no throw")
  });
});

// ── convenience helpers ──────────────────────────────────────────────────

describe("convenience helpers", () => {
  let postMessageSpy: ReturnType<typeof vi.fn>;
  let originalParentDescriptor: PropertyDescriptor | undefined;

  beforeEach(() => {
    originalParentDescriptor = Object.getOwnPropertyDescriptor(window, "parent");
    postMessageSpy = vi.fn();
    Object.defineProperty(window, "parent", {
      configurable: true,
      get: () => ({ postMessage: postMessageSpy }),
    });
  });

  afterEach(() => {
    if (originalParentDescriptor) {
      Object.defineProperty(window, "parent", originalParentDescriptor);
    }
  });

  it("requestOpenFile passes path + optional line/column", () => {
    requestOpenFile("src/x.ts", 12, 4);
    expect(postMessageSpy).toHaveBeenCalledWith(
      { type: "digi:request-open-file", path: "src/x.ts", line: 12, column: 4 },
      "*",
    );
  });

  it("requestFocusLine emits the right type", () => {
    requestFocusLine("a.ts", 1);
    expect(postMessageSpy).toHaveBeenCalledWith(
      { type: "digi:request-focus-line", path: "a.ts", line: 1, column: undefined },
      "*",
    );
  });

  it("requestToast defaults level to 'info'", () => {
    requestToast("hi");
    expect(postMessageSpy).toHaveBeenCalledWith(
      { type: "digi:request-toast", message: "hi", level: "info" },
      "*",
    );
  });

  it("notifyReady emits digi:ready", () => {
    notifyReady();
    expect(postMessageSpy).toHaveBeenCalledWith({ type: "digi:ready" }, "*");
  });
});

// ── onHostMessage ────────────────────────────────────────────────────────

describe("onHostMessage", () => {
  function fireMessage(data: unknown) {
    window.dispatchEvent(new MessageEvent("message", { data }));
  }

  it("delivers matching digi:* messages", () => {
    const handler = vi.fn();
    const off = onHostMessage("digi:theme-change", handler);
    fireMessage({ type: "digi:theme-change", theme: { mode: "light", accent: null, locale: "en" } });
    expect(handler).toHaveBeenCalledOnce();
    off();
  });

  it("ignores non-digi messages (analytics scripts etc.)", () => {
    const handler = vi.fn();
    onHostMessage("digi:abort", handler);
    fireMessage({ type: "react-devtools-something" });
    fireMessage({ type: "gtag-event", action: "click" });
    expect(handler).not.toHaveBeenCalled();
  });

  it("ignores digi:* messages of a different type", () => {
    const themeHandler = vi.fn();
    const abortHandler = vi.fn();
    onHostMessage("digi:theme-change", themeHandler);
    onHostMessage("digi:abort", abortHandler);
    fireMessage({ type: "digi:abort", reason: "user" });
    expect(themeHandler).not.toHaveBeenCalled();
    expect(abortHandler).toHaveBeenCalledOnce();
  });

  it("ignores non-object payloads (defensive)", () => {
    const handler = vi.fn();
    onHostMessage("digi:abort", handler);
    fireMessage(null);
    fireMessage("hello");
    fireMessage(42);
    expect(handler).not.toHaveBeenCalled();
  });

  it("returns an unsubscribe that stops further delivery", () => {
    const handler = vi.fn();
    const off = onHostMessage("digi:abort", handler);
    off();
    fireMessage({ type: "digi:abort" });
    expect(handler).not.toHaveBeenCalled();
  });
});
