import { useEffect, useMemo, useState } from "react";
import type { WorkspaceFile } from "@digitorn/preview-sdk";
import { Markdown } from "./Markdown";

interface Props {
  source: string;
  files: Map<string, WorkspaceFile>;
}

interface Turn {
  index: number;
  path: string;
  speaker: string;
  file: WorkspaceFile;
}

interface ScriptTurn {
  speaker: string;
  text: string;
}

const TURN_RE = /^audio_overview\/turn_(\d{3})\.mp3$/;

function base64ToBlobUrl(b64: string, mime: string): string {
  const clean = b64.replace(/^data:[^;]+;base64,/, "");
  const bytes = atob(clean);
  const buf = new Uint8Array(bytes.length);
  for (let i = 0; i < bytes.length; i++) buf[i] = bytes.charCodeAt(i);
  return URL.createObjectURL(new Blob([buf], { type: mime }));
}

export function AudioOverview({ source, files }: Props) {
  const [scriptByIndex, setScriptByIndex] = useState<ScriptTurn[]>([]);

  useEffect(() => {
    const raw = files.get("audio_overview/script.json")?.content;
    if (!raw) {
      setScriptByIndex([]);
      return;
    }
    try {
      const parsed = JSON.parse(raw) as unknown;
      if (Array.isArray(parsed)) {
        setScriptByIndex(
          parsed.filter(
            (t): t is ScriptTurn =>
              typeof t === "object" &&
              t !== null &&
              typeof (t as ScriptTurn).text === "string",
          ),
        );
      }
    } catch {
      setScriptByIndex([]);
    }
  }, [files]);

  const turns = useMemo(() => {
    const out: Turn[] = [];
    for (const [path, file] of files) {
      const m = TURN_RE.exec(path);
      if (!m) continue;
      const index = Number(m[1]);
      const scripted = scriptByIndex[index - 1];
      out.push({
        index,
        path,
        file,
        speaker: scripted?.speaker ?? (index % 2 ? "host_a" : "host_b"),
      });
    }
    return out.sort((a, b) => a.index - b.index);
  }, [files, scriptByIndex]);

  const [urls, setUrls] = useState<Record<string, string>>({});
  useEffect(() => {
    const next: Record<string, string> = {};
    for (const t of turns) {
      if (urls[t.path] && t.file.content) continue;
      try {
        next[t.path] = base64ToBlobUrl(t.file.content, "audio/mpeg");
      } catch {
        // Content not base64 — skip; fallback message kicks in below.
      }
    }
    if (Object.keys(next).length > 0) {
      setUrls((prev) => ({ ...prev, ...next }));
    }
    return () => {
      // We intentionally don't revoke on every render; the iframe is
      // short-lived enough that leaking a few blob URLs per session is
      // cheaper than reconstructing them every time the file map
      // refreshes. The browser GC reclaims them on unload.
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [turns]);

  if (turns.length === 0) {
    return (
      <>
        <Markdown source={source} />
        <div
          style={{
            marginTop: 18,
            padding: 14,
            background: "var(--surface)",
            border: "1px dashed var(--border)",
            borderRadius: 8,
            fontSize: 12.5,
            color: "var(--text-muted)",
          }}
        >
          No audio segments yet. The agent will save MP3 turns under
          <code style={{ marginLeft: 4 }}>audio_overview/</code> once
          synthesis completes.
        </div>
      </>
    );
  }

  return (
    <div>
      <div style={{ marginBottom: 18 }}>
        <Markdown source={summaryHeader(source, turns.length)} />
      </div>
      {turns.map((t) => {
        const url = urls[t.path];
        const scripted = scriptByIndex[t.index - 1];
        return (
          <div key={t.path}>
            <div className="audio-row">
              <span className="turn-label">
                #{String(t.index).padStart(3, "0")} · {t.speaker}
              </span>
              {url ? (
                <audio controls preload="none" src={url} />
              ) : (
                <span style={{ fontSize: 12, color: "var(--warn)" }}>
                  Encoding pending
                </span>
              )}
            </div>
            {scripted && (
              <div
                style={{
                  margin: "0 12px 14px",
                  padding: "8px 12px",
                  borderLeft: "2px solid var(--border)",
                  color: "var(--text-muted)",
                  fontSize: 13,
                  fontStyle: "italic",
                  lineHeight: 1.55,
                }}
              >
                {scripted.text}
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
}

function summaryHeader(source: string, n: number): string {
  const firstLine = source.split("\n")[0] ?? "# Audio Overview";
  const head = firstLine.startsWith("#") ? firstLine : "# Audio Overview";
  return `${head}\n\n*${n} segments — press play on any segment to listen.*`;
}
