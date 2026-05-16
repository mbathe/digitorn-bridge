import { useEffect, useRef, useState } from "react";
import { Markdown } from "./Markdown";

interface Props {
  source: string;
  onCitationClick?: (path: string, start: number, end: number) => void;
}

const MERMAID_RE = /```mermaid\n([\s\S]*?)```/;

export function MindMap({ source, onCitationClick }: Props) {
  const ref = useRef<HTMLDivElement | null>(null);
  const [renderError, setRenderError] = useState<string | null>(null);
  const [renderedFor, setRenderedFor] = useState<string | null>(null);

  const match = MERMAID_RE.exec(source);
  const diagram = match?.[1]?.trim() ?? null;

  useEffect(() => {
    if (!diagram) return;
    let cancelled = false;
    setRenderError(null);
    (async () => {
      try {
        const mermaid = (await import("mermaid")).default;
        const isLight = document.documentElement.dataset.theme === "light";
        mermaid.initialize({
          startOnLoad: false,
          theme: isLight ? "neutral" : "dark",
          securityLevel: "strict",
          fontFamily: "IBM Plex Sans, system-ui, sans-serif",
        });
        const id = `mermaid-${Math.random().toString(36).slice(2, 9)}`;
        const { svg } = await mermaid.render(id, diagram);
        if (cancelled) return;
        if (ref.current) ref.current.innerHTML = svg;
        setRenderedFor(diagram);
      } catch (err) {
        if (cancelled) return;
        setRenderError(err instanceof Error ? err.message : String(err));
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [diagram]);

  if (!diagram) {
    return <Markdown source={source} onCitationClick={onCitationClick} />;
  }

  return (
    <div>
      <div className="mermaid-host" ref={ref}>
        {renderedFor === null && !renderError && (
          <div style={{ padding: 24 }}>
            <div className="spinner" />
          </div>
        )}
      </div>
      {renderError && (
        <pre
          style={{
            background: "var(--code-bg)",
            border: "1px solid var(--border)",
            borderRadius: 8,
            padding: 12,
            fontSize: 12,
            color: "var(--warn)",
            whiteSpace: "pre-wrap",
          }}
        >
          Could not render diagram: {renderError}
          {"\n\n"}
          {diagram}
        </pre>
      )}
      <Markdown
        source={source.replace(MERMAID_RE, "").trim()}
        onCitationClick={onCitationClick}
      />
    </div>
  );
}
