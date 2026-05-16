import { useEffect, useMemo, useRef } from "react";
import { marked } from "marked";
import { highlightCitations, parseCitation } from "../lib/cite";

interface Props {
  source: string;
  onCitationClick?: (path: string, start: number, end: number) => void;
}

marked.setOptions({ gfm: true, breaks: false });

export function Markdown({ source, onCitationClick }: Props) {
  const ref = useRef<HTMLDivElement | null>(null);

  const html = useMemo(() => {
    const raw = marked.parse(source, { async: false }) as string;
    return highlightCitations(raw);
  }, [source]);

  useEffect(() => {
    const root = ref.current;
    if (!root) return;
    if (!onCitationClick) return;

    function handleClick(e: MouseEvent) {
      const target = (e.target as HTMLElement).closest(
        "button.citation",
      ) as HTMLButtonElement | null;
      if (!target) return;
      const raw = target.getAttribute("data-cite");
      if (!raw) return;
      try {
        const data = JSON.parse(decodeURIComponent(raw)) as ReturnType<typeof parseCitation>;
        if (!data) return;
        e.preventDefault();
        onCitationClick!(data.path, data.start, data.end);
      } catch {
        /* malformed citation, ignore */
      }
    }
    root.addEventListener("click", handleClick);
    return () => root.removeEventListener("click", handleClick);
  }, [onCitationClick, html]);

  return (
    <div
      ref={ref}
      className="markdown"
      dangerouslySetInnerHTML={{ __html: html }}
    />
  );
}
