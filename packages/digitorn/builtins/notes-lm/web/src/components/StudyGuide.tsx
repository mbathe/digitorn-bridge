import { useMemo, useState } from "react";
import { Markdown } from "./Markdown";

interface Props {
  source: string;
  onCitationClick?: (path: string, start: number, end: number) => void;
}

type Tab = "concepts" | "faq" | "quiz" | "raw";

interface Section {
  tab: Tab;
  label: string;
  body: string;
}

const HEADINGS: Record<Tab, RegExp[]> = {
  concepts: [/^##\s+(key\s+)?concepts?\b/im],
  faq: [/^##\s+(faq|frequently)/im],
  quiz: [/^##\s+(quiz|questions?)\b/im],
  raw: [],
};

function sliceSection(md: string, ix: number): string {
  // Take from `ix` until the next `## ` heading or EOF.
  const after = md.slice(ix);
  const nextH2 = /^##\s+/m;
  nextH2.lastIndex = 0;
  // Skip the heading line we matched on, then find the next one.
  const rest = after.slice(after.indexOf("\n") + 1);
  const m = nextH2.exec(rest);
  return m ? after.slice(0, after.indexOf("\n") + 1 + m.index) : after;
}

function splitSections(md: string): Section[] {
  const out: Section[] = [];
  for (const tab of ["concepts", "faq", "quiz"] as Tab[]) {
    for (const re of HEADINGS[tab]) {
      const m = re.exec(md);
      if (m) {
        out.push({
          tab,
          label: capitalize(tab === "faq" ? "FAQ" : tab),
          body: sliceSection(md, m.index).trim(),
        });
        break;
      }
    }
  }
  return out;
}

function capitalize(s: string): string {
  return s.charAt(0).toUpperCase() + s.slice(1);
}

export function StudyGuide({ source, onCitationClick }: Props) {
  const sections = useMemo(() => splitSections(source), [source]);
  const [active, setActive] = useState<Tab>(
    sections[0]?.tab ?? "raw",
  );

  if (sections.length === 0) {
    return <Markdown source={source} onCitationClick={onCitationClick} />;
  }

  const current =
    active === "raw"
      ? { body: source, label: "Full" }
      : sections.find((s) => s.tab === active) ?? sections[0];

  return (
    <div>
      <div className="tabs" role="tablist">
        {sections.map((s) => (
          <button
            key={s.tab}
            type="button"
            role="tab"
            aria-selected={active === s.tab}
            onClick={() => setActive(s.tab)}
          >
            {s.label}
          </button>
        ))}
        <button
          type="button"
          role="tab"
          aria-selected={active === "raw"}
          onClick={() => setActive("raw")}
        >
          Full
        </button>
      </div>
      <Markdown source={current.body} onCitationClick={onCitationClick} />
    </div>
  );
}
