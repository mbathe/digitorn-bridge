import type { WorkspaceFile } from "@digitorn/preview-sdk";
import { requestOpenFile, requestToast } from "@digitorn/preview-sdk";
import type { Selection } from "../App";
import { artefactById } from "../lib/artefacts";
import { Markdown } from "./Markdown";
import { SourceViewer } from "./SourceViewer";
import { MindMap } from "./MindMap";
import { Timeline } from "./Timeline";
import { StudyGuide } from "./StudyGuide";
import { AudioOverview } from "./AudioOverview";
import { FormViewer } from "./FormViewer";

interface Props {
  files: Map<string, WorkspaceFile>;
  selection: Selection;
  onSelectFile: (path: string, focusLines?: [number, number]) => void;
}

function isMarkdown(path: string, file: WorkspaceFile | undefined): boolean {
  const mime = (file as { mime?: string } | undefined)?.mime ?? "";
  const lower = path.toLowerCase();
  return (
    lower.endsWith(".md") ||
    lower.endsWith(".markdown") ||
    mime === "text/markdown"
  );
}

function isForm(path: string): boolean {
  return path.startsWith("forms/") && path.toLowerCase().endsWith(".json");
}

function isResponse(path: string): boolean {
  return path.startsWith("responses/") && path.toLowerCase().endsWith(".json");
}

export function Viewer({ files, selection, onSelectFile }: Props) {
  if (selection.kind === "welcome") {
    return <Welcome hasFiles={files.size > 0} />;
  }

  if (selection.kind === "artefact") {
    const def = artefactById(selection.id);
    if (!def) return <Welcome hasFiles={files.size > 0} />;
    const file = files.get(def.path);
    if (!file) {
      return (
        <div className="viewer">
          <ViewerHeader title={def.title} subtitle={def.path} />
          <div className="viewer-body">
            <div className="empty">
              Not generated yet. Ask the agent for "{def.title.toLowerCase()}".
            </div>
          </div>
        </div>
      );
    }

    const onCitation = (path: string, start: number, end: number) => {
      onSelectFile(path, [start, end]);
      // Best-effort cross-pane signal to the host's workspace IDE.
      requestOpenFile(path, start);
    };

    return (
      <div className="viewer">
        <ViewerHeader
          title={def.title}
          subtitle={def.path}
          actions={
            <>
              <button
                type="button"
                onClick={() => copyMarkdown(file.content, def.title)}
              >
                Copy
              </button>
              <button
                type="button"
                onClick={() => downloadFile(def.path, file.content)}
              >
                Download
              </button>
            </>
          }
        />
        <div className="viewer-body">
          {def.id === "mindmap" && (
            <MindMap source={file.content} onCitationClick={onCitation} />
          )}
          {def.id === "timeline" && (
            <Timeline source={file.content} onCitationClick={onCitation} />
          )}
          {def.id === "study_guide" && (
            <StudyGuide source={file.content} onCitationClick={onCitation} />
          )}
          {def.id === "audio_overview" && (
            <AudioOverview source={file.content} files={files} />
          )}
          {def.id === "briefing" && (
            <Markdown source={file.content} onCitationClick={onCitation} />
          )}
        </div>
      </div>
    );
  }

  const file = files.get(selection.path);
  if (!file) {
    return (
      <div className="viewer">
        <ViewerHeader title="File missing" subtitle={selection.path} />
        <div className="viewer-body">
          <div className="empty">
            The source was removed. Pick another from the sidebar.
          </div>
        </div>
      </div>
    );
  }

  const path = selection.path;
  const showAsForm = isForm(path);
  const showAsResponse = isResponse(path);

  return (
    <div className="viewer">
      <ViewerHeader
        title={displayName(path)}
        subtitle={`${path} · ${file.lines ?? 0} lines`}
        actions={
          <button
            type="button"
            onClick={() => copyMarkdown(file.content, displayName(path))}
          >
            Copy
          </button>
        }
      />
      <div className="viewer-body">
        {showAsForm ? (
          <FormViewer path={path} source={file.content} />
        ) : showAsResponse ? (
          <ResponseViewer source={file.content} />
        ) : isMarkdown(path, file) ? (
          <Markdown source={file.content} />
        ) : (
          <SourceViewer
            content={file.content}
            focusLines={selection.focusLines}
          />
        )}
      </div>
    </div>
  );
}

/** Pretty-prints a form response JSON (`responses/<slug>-<iso>.json`)
 *  with the submitted values laid out as a definition list rather
 *  than raw JSON. Falls back to JSON when parsing fails. */
function ResponseViewer({ source }: { source: string }) {
  try {
    const parsed = JSON.parse(source) as {
      form_id?: string;
      form_path?: string;
      submitted_at?: string;
      values?: Record<string, unknown>;
    };
    const entries = Object.entries(parsed.values || {});
    return (
      <div className="response-viewer">
        <header className="response-meta">
          <div>
            <span className="response-meta-label">Form:</span>{" "}
            <code>{parsed.form_id || parsed.form_path || "(unknown)"}</code>
          </div>
          {parsed.submitted_at && (
            <div>
              <span className="response-meta-label">Submitted:</span>{" "}
              {new Date(parsed.submitted_at).toLocaleString()}
            </div>
          )}
        </header>
        <dl className="response-values">
          {entries.map(([k, v]) => (
            <div key={k} className="response-row">
              <dt>{k}</dt>
              <dd>{formatResponseValue(v)}</dd>
            </div>
          ))}
        </dl>
      </div>
    );
  } catch {
    return <SourceViewer content={source} />;
  }
}

function formatResponseValue(v: unknown): React.ReactNode {
  if (v === null || v === undefined || v === "") {
    return <span className="response-empty">—</span>;
  }
  if (typeof v === "boolean") return v ? "yes" : "no";
  if (Array.isArray(v)) {
    if (v.length === 0) return <span className="response-empty">empty</span>;
    if (v.every((x) => typeof x !== "object")) {
      return v.join(", ");
    }
    // Array of records (a repeating group).
    return (
      <ol className="response-list">
        {v.map((entry, i) => (
          <li key={i}>
            <dl className="response-values nested">
              {Object.entries(entry as Record<string, unknown>).map(([k, sv]) => (
                <div key={k} className="response-row">
                  <dt>{k}</dt>
                  <dd>{formatResponseValue(sv)}</dd>
                </div>
              ))}
            </dl>
          </li>
        ))}
      </ol>
    );
  }
  if (typeof v === "object") {
    return <pre className="response-json">{JSON.stringify(v, null, 2)}</pre>;
  }
  return String(v);
}

function Welcome({ hasFiles }: { hasFiles: boolean }) {
  return (
    <div className="welcome">
      <div className="welcome-card">
        <div className="welcome-logo">N</div>
        <h1>Notes LM</h1>
        <p>
          Drop a source in the chat (URL, file, or pasted text) — the agent
          curates them in your workspace and grounds every answer with
          verbatim line-range citations.
        </p>
        {!hasFiles && (
          <p style={{ fontSize: 12.5, color: "var(--text-faint)" }}>
            Once a source lands, generated artefacts (briefing, mind map,
            timeline, study guide, audio overview) appear in the Studio
            panel on the right.
          </p>
        )}
        <p style={{ fontSize: 12, color: "var(--text-faint)" }}>
          Pick one of the suggested prompts in the chat or ask "write a
          briefing", "build a mind map", "extract a timeline".
        </p>
      </div>
    </div>
  );
}

function ViewerHeader({
  title,
  subtitle,
  actions,
}: {
  title: string;
  subtitle?: string;
  actions?: React.ReactNode;
}) {
  return (
    <header className="viewer-header">
      <div className="viewer-title">
        <h1>{title}</h1>
        {subtitle && <span className="path">{subtitle}</span>}
      </div>
      {actions && <div className="viewer-actions">{actions}</div>}
    </header>
  );
}

function displayName(path: string): string {
  const slash = path.lastIndexOf("/");
  return slash < 0 ? path : path.slice(slash + 1);
}

async function copyMarkdown(content: string, label: string): Promise<void> {
  try {
    await navigator.clipboard.writeText(content);
    requestToast(`Copied ${label}`, "success");
  } catch {
    requestToast("Copy blocked by browser permissions", "error");
  }
}

function downloadFile(path: string, content: string): void {
  const name = path.includes("/") ? path.slice(path.lastIndexOf("/") + 1) : path;
  const blob = new Blob([content], { type: "text/markdown;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = name;
  a.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
