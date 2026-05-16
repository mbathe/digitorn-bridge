import { useEffect, useMemo, useRef } from "react";

interface Props {
  content: string;
  focusLines?: [number, number];
}

// Reject content as "not human-readable" when >5% of the first 4 KB is
// non-printable (NUL, BEL, control chars, etc.). Matches what most diff
// tools call "binary": git uses 1 in 8000 NULs as the cutoff, but for
// text-vs-binary in a UI viewer the threshold can be looser.
function isBinaryLike(content: string): boolean {
  if (!content) return false;
  const sample = content.slice(0, 4096);
  let nonPrintable = 0;
  for (let i = 0; i < sample.length; i++) {
    const code = sample.charCodeAt(i);
    if (code === 9 || code === 10 || code === 13) continue; // tab, LF, CR
    if (code < 32 || code === 127) nonPrintable++;
  }
  return nonPrintable / sample.length > 0.05;
}

function formatBytes(n: number): string {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / (1024 * 1024)).toFixed(1)} MB`;
}

export function SourceViewer({ content, focusLines }: Props) {
  const ref = useRef<HTMLDivElement | null>(null);

  const binary = useMemo(() => isBinaryLike(content), [content]);
  const lines = useMemo(
    () => (binary ? [] : content.split(/\r?\n/)),
    [content, binary],
  );

  useEffect(() => {
    if (!focusLines || !ref.current) return;
    const [start] = focusLines;
    const el = ref.current.querySelector<HTMLElement>(
      `[data-ln="${start}"]`,
    );
    el?.scrollIntoView({ behavior: "smooth", block: "center" });
  }, [focusLines, content]);

  if (binary) {
    return (
      <div className="binary-placeholder">
        <div className="binary-placeholder-title">Binary content</div>
        <div className="binary-placeholder-desc">
          This file is {formatBytes(content.length)} of non-text data
          and can't be displayed as lines. For PDFs, DOCX, and similar
          formats, drop them into the <strong>chat composer</strong>{" "}
          (paperclip icon) and the daemon will extract their text into
          the <code>attachments/</code> group of this sidebar.
        </div>
      </div>
    );
  }

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
