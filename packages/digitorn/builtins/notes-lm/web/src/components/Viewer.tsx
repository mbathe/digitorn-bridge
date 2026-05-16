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

interface Props {
  files: Map<string, WorkspaceFile>;
  selection: Selection;
  onSelectFile: (path: string, focusLines?: [number, number]) => void;
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

  return (
    <div className="viewer">
      <ViewerHeader
        title={displayName(selection.path)}
        subtitle={`${selection.path} · ${file.lines ?? 0} lines`}
        actions={
          <button
            type="button"
            onClick={() => copyMarkdown(file.content, displayName(selection.path))}
          >
            Copy
          </button>
        }
      />
      <div className="viewer-body">
        <SourceViewer
          content={file.content}
          focusLines={selection.focusLines}
        />
      </div>
    </div>
  );
}

function Welcome({ hasFiles }: { hasFiles: boolean }) {
  return (
    <div className="welcome">
      <div className="welcome-card">
        <div className="welcome-logo">N</div>
        <h1>Notes LM</h1>
        <p>
          Drop a source in the chat (URL, file, or pasted text) — the agent
          saves it under <code>sources/</code> and grounds every answer with
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

