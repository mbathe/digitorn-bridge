import { useEffect, useMemo, useRef, useState } from "react";
import {
  requestToast,
  useConnection,
  useFiles,
  useHostTheme,
  usePendingHints,
  useResourceLifecycle,
} from "@digitorn/preview-sdk";
import { AddSourceButton } from "./components/AddSourceButton";
import { SourceList } from "./components/SourceList";
import { Studio } from "./components/Studio";
import { Viewer } from "./components/Viewer";
import { ARTEFACTS, artefactById } from "./lib/artefacts";

export type Selection =
  | { kind: "welcome" }
  | { kind: "file"; path: string; focusLines?: [number, number] }
  | { kind: "artefact"; id: string };

const ARTEFACT_PATHS = new Set(ARTEFACTS.map((a) => a.path));

function pathToArtefactId(path: string): string | null {
  for (const art of ARTEFACTS) if (art.path === path) return art.id;
  return null;
}

export function App() {
  const connected = useConnection();
  const files = useFiles();
  const theme = useHostTheme();
  const [selection, setSelection] = useState<Selection>({ kind: "welcome" });
  // Selection ref so lifecycle callbacks can read the current selection
  // without re-binding (callbacks live in the SDK's internal ref).
  const selectionRef = useRef(selection);
  selectionRef.current = selection;

  useEffect(() => {
    const root = document.documentElement;
    const resolved =
      theme.mode === "auto"
        ? window.matchMedia("(prefers-color-scheme: light)").matches
          ? "light"
          : "dark"
        : theme.mode;
    root.dataset.theme = resolved;
  }, [theme.mode]);

  const fileCount = files.size;
  const sourceFiles = useMemo(
    () =>
      Array.from(files.keys())
        .filter(
          (p) => p.startsWith("sources/") || p.startsWith("attachments/"),
        )
        .sort(),
    [files],
  );

  // Auto-jump to a fresh artefact when the agent generates one and the
  // user is still on the welcome screen. Doesn't fire for files present
  // at mount — those are pre-existing.
  useResourceLifecycle({
    channel: "files",
    match: (id) => ARTEFACT_PATHS.has(id),
    fireForInitial: false,
    onCreate: (e) => {
      const id = pathToArtefactId(e.id);
      if (!id) return;
      const art = artefactById(id);
      if (art) requestToast(`${art.title} ready`, "success");
      if (selectionRef.current.kind === "welcome") {
        setSelection({ kind: "artefact", id });
      }
    },
  });

  // First-load: if the session already has an artefact, jump straight
  // to it (skip the welcome screen). Runs once per file Map change but
  // short-circuits as soon as selection moves off welcome.
  useEffect(() => {
    if (selection.kind !== "welcome") return;
    for (const art of ARTEFACTS) {
      if (files.has(art.path)) {
        setSelection({ kind: "artefact", id: art.id });
        return;
      }
    }
  }, [files, selection.kind]);

  // Toast + system-prompt hint every time a new source lands under
  // sources/. The hint flows into the next user turn via the SDK's
  // useChat -> system_addendum path, so the LLM is aware that the
  // user just curated something new and can prioritise reading it.
  const { addHint } = usePendingHints();
  useResourceLifecycle({
    channel: "files",
    match: "sources/",
    fireForInitial: false,
    onCreate: (e) => {
      const name = e.id.slice("sources/".length);
      requestToast(`Source added: ${name}`, "info");
      addHint(
        `The user just added a new source via the iframe sidebar: ` +
        `\`${e.id}\`. Discover the full corpus with WsGlob and read this ` +
        `file with WsRead when answering the next question.`,
      );
    },
  });

  // Mirror the hint path for attachments uploaded via the chat composer
  // paperclip. They land under attachments/<name> via the daemon's
  // register_attachment, the LLM has no other signal that something
  // arrived. fireForInitial: false so existing attachments from prior
  // turns don't re-trigger on every iframe mount.
  useResourceLifecycle({
    channel: "files",
    match: "attachments/",
    fireForInitial: false,
    onCreate: (e) => {
      const name = e.id.slice("attachments/".length);
      requestToast(`Attachment added: ${name}`, "info");
      addHint(
        `The user just uploaded a file via the chat composer paperclip: ` +
        `\`${e.id}\`. The extracted text is in the workspace. Read it with ` +
        `WsRead before answering the next question if it's relevant.`,
      );
    },
  });

  return (
    <div className="app-shell">
      <aside className="pane">
        <header className="pane-header">
          <div className="pane-header-title">
            <h2>Sources</h2>
            <span className="count" aria-label="connection status">
              <span
                className={`status-dot ${connected ? "connected" : ""}`}
                title={connected ? "Live" : "Disconnected"}
              />{" "}
              {sourceFiles.length}
            </span>
          </div>
          <AddSourceButton
            onAdded={(path) => setSelection({ kind: "file", path })}
          />
        </header>
        <div className="pane-body">
          <SourceList
            files={files}
            selection={selection}
            onSelect={(path) => setSelection({ kind: "file", path })}
          />
        </div>
      </aside>

      <main className="pane">
        <Viewer
          files={files}
          selection={selection}
          onSelectFile={(path, focusLines) =>
            setSelection({ kind: "file", path, focusLines })
          }
        />
      </main>

      <aside className="pane">
        <header className="pane-header">
          <h2>Studio</h2>
          <span className="count">{fileCount} files</span>
        </header>
        <div className="pane-body">
          <Studio
            files={files}
            selection={selection}
            onSelect={(id) => setSelection({ kind: "artefact", id })}
          />
        </div>
      </aside>
    </div>
  );
}
