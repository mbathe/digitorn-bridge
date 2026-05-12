import { afterEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, render } from "@testing-library/react";
import { ReactPreview, TemplatePreview } from "../../src/templates/TemplatePreview.js";

/**
 * Dispatcher + skeleton overlay tests.
 *
 * The previous Playwright suite asserted "the iframe ends up showing
 * content" but never checked the skeleton itself. These tests inspect
 * the DOM directly: the shimmer bar must be present at mount and gone
 * after the iframe fires ``onLoad``.
 */

afterEach(() => {
  cleanup();
});

describe("TemplatePreview kind dispatcher", () => {
  it("warns and falls back to React when no renderer is registered", () => {
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    try {
      const { container } = render(
        <TemplatePreview
          kind="latex-not-installed"
          seed={{ bundleUrl: "about:blank" }}
        />,
      );
      // Fallback to React renders the FastPath shell (the bundleUrl
      // path) — so we still see an iframe in the DOM rather than nothing.
      expect(container.querySelector("iframe")).not.toBeNull();
      expect(warn).toHaveBeenCalledOnce();
      const msg = String(warn.mock.calls[0][0]);
      expect(msg).toMatch(/latex-not-installed/);
      expect(msg).toMatch(/registerPreviewKind/);
    } finally {
      warn.mockRestore();
    }
  });

  it("does NOT warn when the kind has a registered renderer (built-in 'html')", () => {
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    try {
      render(
        <TemplatePreview
          kind="html"
          seed={{ files: { "index.html": "<p>hi</p>" } }}
        />,
      );
      expect(warn).not.toHaveBeenCalled();
    } finally {
      warn.mockRestore();
    }
  });
});

describe("ReactPreview FastPath skeleton overlay", () => {
  it("renders the shimmer overlay before the iframe loads", () => {
    const { container } = render(
      <ReactPreview seed={{ bundleUrl: "about:blank" }} />,
    );
    const shimmer = container.querySelector(".digi-preview-shimmer-bar");
    expect(shimmer).not.toBeNull();
  });

  it("removes the shimmer overlay after the iframe fires onLoad", async () => {
    const { container } = render(
      <ReactPreview seed={{ bundleUrl: "about:blank" }} />,
    );
    expect(container.querySelector(".digi-preview-shimmer-bar")).not.toBeNull();

    const iframe = container.querySelector("iframe");
    expect(iframe).not.toBeNull();

    // Fire onLoad synchronously — the FastPath listens for it via the
    // iframe element's React event. Dispatching a ``load`` DOM event
    // doesn't call React's onLoad; we trigger the handler the React
    // way via ``act`` + the React DevTools approach is overkill — use
    // a synthetic event dispatch instead.
    await act(async () => {
      iframe!.dispatchEvent(new Event("load"));
    });

    // React's onLoad listener fires from the synthetic event system,
    // which only attaches when bubbling. happy-dom's ``Event`` is
    // delivered via the underlying DOM listener — React attaches its
    // listener as a delegated bubbling handler on the root, so the
    // event must bubble.
    await act(async () => {
      iframe!.dispatchEvent(new Event("load", { bubbles: true }));
    });

    expect(container.querySelector(".digi-preview-shimmer-bar")).toBeNull();
  });

  it("forces the skeleton off after 2.5s even when onLoad never fires", async () => {
    vi.useFakeTimers();
    try {
      const { container } = render(
        <ReactPreview seed={{ bundleUrl: "about:blank" }} />,
      );
      expect(container.querySelector(".digi-preview-shimmer-bar")).not.toBeNull();

      await act(async () => {
        vi.advanceTimersByTime(2_600);
      });

      expect(container.querySelector(".digi-preview-shimmer-bar")).toBeNull();
    } finally {
      vi.useRealTimers();
    }
  });
});
