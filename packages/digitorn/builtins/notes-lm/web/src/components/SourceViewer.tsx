import { useEffect, useMemo, useRef } from "react";

interface Props {
  content: string;
  focusLines?: [number, number];
}

export function SourceViewer({ content, focusLines }: Props) {
  const ref = useRef<HTMLDivElement | null>(null);

  const lines = useMemo(() => content.split(/\r?\n/), [content]);

  useEffect(() => {
    if (!focusLines || !ref.current) return;
    const [start] = focusLines;
    const el = ref.current.querySelector<HTMLElement>(
      `[data-ln="${start}"]`,
    );
    el?.scrollIntoView({ behavior: "smooth", block: "center" });
  }, [focusLines, content]);

  return (
    <div className="source-viewer" ref={ref}>
      {lines.map((text, i) => {
        const n = i + 1;
        const inFocus =
          focusLines !== undefined && n >= focusLines[0] && n <= focusLines[1];
        return (
          <div
            key={n}
            data-ln={n}
            className={`line ${inFocus ? "highlight" : ""}`}
          >
            <div className="ln">{n}</div>
            <div className="lc">{text || " "}</div>
          </div>
        );
      })}
    </div>
  );
}
