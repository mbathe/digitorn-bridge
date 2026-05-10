/**
 * Live in-browser preview of a template seed.
 *
 * Pass a ``TemplateSeed`` (or just ``files`` + ``entry``) and the
 * component bundles the files via esbuild-wasm and renders the
 * result in a sandboxed iframe. The bundler is keyed off the
 * stringified file list so re-renders without source changes don't
 * trigger redundant rebuilds.
 *
 * When the template carries a ``previewUrl`` instead of a seed, use
 * a plain ``<iframe src={previewUrl}>`` from the consuming app —
 * this component is for the ``seed`` path only. (We deliberately
 * don't multiplex the two: the modal does, since it has the
 * template-level decision logic.)
 */

import {
  createElement,
  useEffect,
  useMemo,
  useRef,
  useState,
  type CSSProperties,
  type ReactNode,
} from "react";

import { bundleFiles } from "./bundler.js";
import { TemplateSandbox } from "./Sandbox.js";
import type { TemplateBundleError, TemplateBundleStatus, TemplateSeed } from "./types.js";

export interface TemplatePreviewProps {
  seed: TemplateSeed;
  /** Optional callback fired on every status transition. */
  onStatus?: (status: TemplateBundleStatus) => void;
  /** Wrapper style. */
  style?: CSSProperties;
  /** Wrapper className. */
  className?: string;
  /** Render slot for the loading state (default: nothing). */
  loadingSlot?: ReactNode;
  /** Render slot for the error state (default: a minimal red banner). */
  errorSlot?: (errors: TemplateBundleError[]) => ReactNode;
}

const _DEFAULT_ENTRY = "src/main.tsx";

export function TemplatePreview({
  seed,
  onStatus,
  style,
  className,
  loadingSlot,
  errorSlot,
}: TemplatePreviewProps) {
  // Fast path: when the Vite plugin pre-bundled the seed, just iframe
  // the resulting page directly. Skips esbuild-wasm entirely.
  if (seed.bundleUrl) {
    const wrapperStyle: CSSProperties = {
      width: "100%",
      height: "100%",
      background: "white",
      ...style,
    };
    return createElement(
      "div",
      { className, style: wrapperStyle },
      createElement("iframe", {
        src: seed.bundleUrl,
        title: "preview",
        // Same flags the runtime path uses on the inner sandbox so
        // both surfaces have identical isolation semantics.
        sandbox:
          "allow-scripts allow-same-origin allow-modals allow-forms allow-popups",
        style: {
          width: "100%",
          height: "100%",
          border: "none",
          background: "white",
        },
      }),
    );
  }

  const fileMap = useMemo(() => {
    const m = new Map<string, string>();
    for (const [k, v] of Object.entries(seed.files || {})) {
      if (typeof v === "string") m.set(k, v);
    }
    return m;
  }, [seed.files]);

  const entry: string = seed.entry || _DEFAULT_ENTRY;

  const [status, setStatus] = useState<TemplateBundleStatus>({ status: "idle" });
  const lastUrlRef = useRef<string | null>(null);

  useEffect(() => {
    if (fileMap.size === 0) return;
    let cancelled = false;
    setStatus({ status: "bundling" });
    onStatus?.({ status: "bundling" });

    // Tiny debounce so an app pushing many sequential file edits
    // doesn't trigger a build per keystroke. 100 ms is small enough
    // to feel instant, large enough to coalesce rapid bursts.
    const t = setTimeout(async () => {
      const result = await bundleFiles(fileMap, entry);
      if (cancelled) {
        if (result.ok) URL.revokeObjectURL(result.url);
        return;
      }
      if (result.ok) {
        // Revoke the previous bundle URL once the new one is ready
        // and committed. Doing it inside the callback (vs in cleanup)
        // avoids the race where a fast re-bundle revokes a URL the
        // iframe is still loading.
        if (lastUrlRef.current) {
          URL.revokeObjectURL(lastUrlRef.current);
        }
        lastUrlRef.current = result.url;
        const next: TemplateBundleStatus = {
          status: "ready",
          bundleUrl: result.url,
          bytes: result.size,
          durationMs: result.durationMs,
        };
        setStatus(next);
        onStatus?.(next);
      } else {
        const next: TemplateBundleStatus = {
          status: "error",
          errors: result.errors,
        };
        setStatus(next);
        onStatus?.(next);
      }
    }, 100);

    return () => {
      cancelled = true;
      clearTimeout(t);
    };
  }, [fileMap, entry, onStatus]);

  // Final cleanup on unmount: revoke the last URL.
  useEffect(() => {
    return () => {
      if (lastUrlRef.current) {
        URL.revokeObjectURL(lastUrlRef.current);
        lastUrlRef.current = null;
      }
    };
  }, []);

  const wrapperStyle: CSSProperties = {
    width: "100%",
    height: "100%",
    background: "white",
    ...style,
  };

  if (status.status === "error") {
    if (errorSlot) {
      return createElement(
        "div",
        { className, style: wrapperStyle },
        errorSlot(status.errors),
      );
    }
    return createElement(
      "div",
      {
        className,
        style: { ...wrapperStyle, padding: 16, fontFamily: "ui-monospace, monospace", fontSize: 11, color: "#900", background: "#fee", whiteSpace: "pre-wrap", overflow: "auto" },
      },
      status.errors.map((er, i) =>
        createElement(
          "div",
          { key: i, style: { marginBottom: 8 } },
          createElement(
            "div",
            { style: { fontWeight: 700 } },
            `${er.file}:${er.line}:${er.column}`,
          ),
          createElement("div", null, er.message),
        ),
      ),
    );
  }

  if (status.status !== "ready") {
    return createElement(
      "div",
      { className, style: wrapperStyle },
      loadingSlot ?? null,
    );
  }

  return createElement(
    "div",
    { className, style: wrapperStyle },
    createElement(TemplateSandbox, { bundleUrl: status.bundleUrl }),
  );
}
