import { useMemo } from "react";
import { Markdown } from "./Markdown";

interface Props {
  source: string;
  onCitationClick?: (path: string, start: number, end: number) => void;
}

interface Event {
  date: string;
  text: string;
}

// We accept three common shapes the agent might produce:
//   - "1922-03-04 — text"   (bullet list)
//   - "- 1922: text"        (bullet list, colon)
//   - "### 1922\n\ntext"    (heading-per-event)
const BULLET_RE = /^[-*]\s+(\d{4}(?:-\d{2}(?:-\d{2})?)?|\d{4}s?)\s*[:—-]\s*(.+)$/;

function extractEvents(md: string): Event[] {
  const events: Event[] = [];
  for (const line of md.split(/\r?\n/)) {
    const m = BULLET_RE.exec(line.trim());
    if (m) events.push({ date: m[1], text: m[2] });
  }
  return events;
}

export function Timeline({ source, onCitationClick }: Props) {
  const events = useMemo(() => extractEvents(source), [source]);

  if (events.length < 2) {
    return <Markdown source={source} onCitationClick={onCitationClick} />;
  }

  return (
    <div>
      <div className="timeline">
        {events.map((ev, i) => (
          <div key={i} className="timeline-event">
            <div className="date">{ev.date}</div>
            <div className="desc">{ev.text}</div>
          </div>
        ))}
      </div>
    </div>
  );
}
