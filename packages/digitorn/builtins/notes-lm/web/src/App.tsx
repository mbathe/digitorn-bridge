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
          (p) => p.startsWith("attachments/") || p.startsWith("forms/"),
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
  // attachments/. Single bucket regardless of how the file was added:
  // iframe "+" button, chat composer paperclip, or agent-side URL
  // fetch all converge here.
  const { addHint } = usePendingHints();
  useResourceLifecycle({
    channel: "files",
    match: "attachments/",
    fireForInitial: false,
    onCreate: (e) => {
      const name = e.id.slice("attachments/".length);
      requestToast(`Source added: ${name}`, "success");
      addHint(
        `The user just added a new source: \`${e.id}\`. ` +
        `Read it with \`WsRead("${e.id}")\` BEFORE answering the next ` +
        `question. It contains the content the user is about to ask about.`,
      );
    },
  });

  // Forms — when the agent writes a new schema, toast + auto-select so
  // the user sees the rendered form immediately.
  useResourceLifecycle({
    channel: "files",
    match: "forms/",
    fireForInitial: false,
    onCreate: (e) => {
      if (!e.id.toLowerCase().endsWith(".json")) return;
      const name = e.id.slice("forms/".length);
      requestToast(`Form ready: ${name}`, "success");
      if (selectionRef.current.kind === "welcome") {
        setSelection({ kind: "file", path: e.id });
      }
    },
  });

  // Form submissions — the FormViewer itself pushes a hint via
  // ``usePendingHints``, but ALSO surface a toast here so the user
  // sees their submission saved + the agent's next-turn awareness in
  // sync.
  useResourceLifecycle({
    channel: "files",
    match: "responses/",
    fireForInitial: false,
    onCreate: (e) => {
      const name = e.id.slice("responses/".length);
      requestToast(`Response saved: ${name}`, "info");
    },
  });

  const [sourcesCollapsed, setSourcesCollapsed] = useState(false);
  const [studioCollapsed, setStudioCollapsed] = useState(false);
  // Mobile overlay state: "none" | "sources" | "studio". When set, the
  // corresponding pane slides in over the viewer. Tap-outside / Escape
  // / route-change closes it.
  const [mobileDrawer, setMobileDrawer] = useState<"none" | "sources" | "studio">("none");

  // Detect mobile so we can short-circuit the desktop "collapsed" state.
  // Below 880px both panes are off-canvas drawers — the strip renderer
  // has no role to play.
  const [isMobile, setIsMobile] = useState(() =>
    typeof window !== "undefined" &&
    window.matchMedia("(max-width: 880px)").matches,
  );
  useEffect(() => {
    if (typeof window === "undefined") return;
    const mq = window.matchMedia("(max-width: 880px)");
    const update = () => setIsMobile(mq.matches);
    update();
    mq.addEventListener("change", update);
    return () => mq.removeEventListener("change", update);
  }, []);

  // On mobile the desktop "collapsed" toggles are irrelevant - force
  // both panes to render so the drawer can reveal them.
  const effectiveSourcesCollapsed = isMobile ? false : sourcesCollapsed;
  const effectiveStudioCollapsed = isMobile ? false : studioCollapsed;

  // Close mobile drawer on Escape and when viewport grows past mobile.
  useEffect(() => {
    if (mobileDrawer === "none") return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setMobileDrawer("none");
    };
    const mq = window.matchMedia("(min-width: 881px)");
    const onMq = () => {
      if (mq.matches) setMobileDrawer("none");
    };
    window.addEventListener("keydown", onKey);
    mq.addEventListener("change", onMq);
    return () => {
      window.removeEventListener("keydown", onKey);
      mq.removeEventListener("change", onMq);
    };
  }, [mobileDrawer]);

  // Auto-close the drawer once the user picks something from it - the
  // intent is fulfilled, free the viewport.
  const closeDrawer = () => setMobileDrawer("none");

  const shellClass =
    "app-shell" +
    (effectiveSourcesCollapsed ? " sources-collapsed" : "") +
    (effectiveStudioCollapsed ? " studio-collapsed" : "") +
    (mobileDrawer !== "none" ? " mobile-drawer-open" : "") +
    (mobileDrawer === "sources" ? " mobile-sources-open" : "") +
    (mobileDrawer === "studio" ? " mobile-studio-open" : "");

  return (
    <div className={shellClass}>
      <MobileTopBar
        sourceCount={sourceFiles.length}
        artefactCount={fileCount}
        connected={connected}
        onOpenSources={() => setMobileDrawer("sources")}
        onOpenStudio={() => setMobileDrawer("studio")}
      />
      {mobileDrawer !== "none" && (
        <div
          className="mobile-backdrop"
          onClick={closeDrawer}
          aria-hidden="true"
        />
      )}
      {effectiveSourcesCollapsed ? (
        <CollapsedStrip
          label="Sources"
          count={sourceFiles.length}
          onExpand={() => setSourcesCollapsed(false)}
          side="left"
        />
      ) : (
        <aside className="pane pane-sources">
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
            <div className="pane-header-actions">
              <AddSourceButton
                onAdded={(path) => setSelection({ kind: "file", path })}
              />
              <CollapseButton
                side="left"
                onClick={() => setSourcesCollapsed(true)}
                label="Hide sources"
              />
              <DrawerCloseButton onClose={closeDrawer} label="Close sources" />
            </div>
          </header>
          <div className="pane-body">
            <SourceList
              files={files}
              selection={selection}
              onSelect={(path) => {
                setSelection({ kind: "file", path });
                closeDrawer();
              }}
            />
          </div>
        </aside>
      )}

      <main className="pane pane-viewer">
        <Viewer
          files={files}
          selection={selection}
          onSelectFile={(path, focusLines) =>
            setSelection({ kind: "file", path, focusLines })
          }
        />
      </main>

      {effectiveStudioCollapsed ? (
        <CollapsedStrip
          label="Studio"
          count={fileCount}
          onExpand={() => setStudioCollapsed(false)}
          side="right"
        />
      ) : (
        <aside className="pane pane-studio">
          <header className="pane-header">
            <div className="pane-header-title">
              <h2>Studio</h2>
              <span className="count">{fileCount} files</span>
            </div>
            <div className="pane-header-actions">
              <CollapseButton
                side="right"
                onClick={() => setStudioCollapsed(true)}
                label="Hide studio"
              />
              <DrawerCloseButton onClose={closeDrawer} label="Close studio" />
            </div>
          </header>
          <div className="pane-body">
            <Studio
              files={files}
              selection={selection}
              onSelect={(id) => {
                setSelection({ kind: "artefact", id });
                closeDrawer();
              }}
            />
          </div>
        </aside>
      )}
    </div>
  );
}

function MobileTopBar({
  sourceCount,
  artefactCount,
  connected,
  onOpenSources,
  onOpenStudio,
}: {
  sourceCount: number;
  artefactCount: number;
  connected: boolean;
  onOpenSources: () => void;
  onOpenStudio: () => void;
}) {
  return (
    <header className="mobile-topbar" role="toolbar" aria-label="Notes LM navigation">
      <button
        type="button"
        className="mobile-icon-btn"
        onClick={onOpenSources}
        aria-label="Open sources"
      >
        <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
          <line x1="3" y1="6" x2="21" y2="6" />
          <line x1="3" y1="12" x2="21" y2="12" />
          <line x1="3" y1="18" x2="21" y2="18" />
        </svg>
        {sourceCount > 0 && <span className="mobile-badge">{sourceCount}</span>}
      </button>
      <div className="mobile-title">
        <span
          className={`status-dot ${connected ? "connected" : ""}`}
          title={connected ? "Live" : "Disconnected"}
          aria-hidden="true"
        />
        <span>Notes LM</span>
      </div>
      <button
        type="button"
        className="mobile-icon-btn"
        onClick={onOpenStudio}
        aria-label="Open studio"
      >
        <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
          <rect x="3" y="3" width="7" height="7" rx="1.5" />
          <rect x="14" y="3" width="7" height="7" rx="1.5" />
          <rect x="3" y="14" width="7" height="7" rx="1.5" />
          <rect x="14" y="14" width="7" height="7" rx="1.5" />
        </svg>
        {artefactCount > 0 && <span className="mobile-badge">{artefactCount}</span>}
      </button>
    </header>
  );
}

function CollapseButton({
  side,
  onClick,
  label,
}: {
  side: "left" | "right";
  onClick: () => void;
  label: string;
}) {
  // Chevron points TOWARD the edge the pane collapses to.
  const symbol = side === "left" ? "‹" : "›";
  return (
    <button
      type="button"
      className="pane-collapse-btn"
      onClick={onClick}
      title={label}
      aria-label={label}
    >
      {symbol}
    </button>
  );
}

function DrawerCloseButton({
  onClose,
  label,
}: {
  onClose: () => void;
  label: string;
}) {
  // Only shown on mobile via CSS - on desktop the chevron CollapseButton
  // is the canonical way to hide a pane (and the desktop layout has
  // no drawer to close anyway).
  return (
    <button
      type="button"
      className="drawer-close-btn"
      onClick={onClose}
      aria-label={label}
      title={label}
    >
      <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
        <line x1="18" y1="6" x2="6" y2="18" />
        <line x1="6" y1="6" x2="18" y2="18" />
      </svg>
    </button>
  );
}

function CollapsedStrip({
  label,
  count,
  onExpand,
  side,
}: {
  label: string;
  count: number;
  onExpand: () => void;
  side: "left" | "right";
}) {
  // Chevron points AWAY from the edge (toward the center) to signal
  // expansion. Mirror of the collapse direction.
  const symbol = side === "left" ? "›" : "‹";
  return (
    <button
      type="button"
      className={`pane-collapsed-strip strip-${side}`}
      onClick={onExpand}
      title={`Expand ${label}`}
      aria-label={`Expand ${label}`}
    >
      <span className="strip-chevron">{symbol}</span>
      <span className="strip-label">{label}</span>
      <span className="strip-count">{count}</span>
    </button>
  );
}
