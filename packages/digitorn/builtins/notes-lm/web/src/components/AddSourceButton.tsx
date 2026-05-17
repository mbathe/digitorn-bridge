import { useEffect, useRef, useState } from "react";
import {
  requestToast,
  useChat,
  useWorkspaceFiles,
} from "@digitorn/preview-sdk";

type Tab = "text" | "url" | "file";

interface Props {
  onAdded?: (path: string) => void;
}

function slugify(input: string): string {
  const base = input
    .toLowerCase()
    .normalize("NFKD")
    .replace(/[̀-ͯ]/g, "")
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, 60);
  return base || `source-${Date.now().toString(36)}`;
}

function nowIso(): string {
  return new Date().toISOString();
}

function buildFrontmatter(meta: Record<string, string>): string {
  const lines = ["---"];
  for (const [k, v] of Object.entries(meta)) {
    if (v) lines.push(`${k}: ${v.replace(/\n/g, " ")}`);
  }
  lines.push("---", "");
  return lines.join("\n");
}

export function AddSourceButton({ onAdded }: Props) {
  const [open, setOpen] = useState(false);
  const [tab, setTab] = useState<Tab>("text");
  const wrapperRef = useRef<HTMLDivElement | null>(null);
  const popoverRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    if (!open) return;
    function onDocClick(e: MouseEvent) {
      const target = e.target as Node;
      if (wrapperRef.current?.contains(target)) return;
      if (popoverRef.current?.contains(target)) return;
      setOpen(false);
    }
    function onKey(e: KeyboardEvent) {
      if (e.key === "Escape") setOpen(false);
    }
    document.addEventListener("mousedown", onDocClick);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDocClick);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  return (
    <div className="add-source-wrapper" ref={wrapperRef}>
      <button
        type="button"
        className="add-source-trigger"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        aria-haspopup="dialog"
        title="Add a source"
      >
        <PlusIcon />
        <span className="add-source-trigger-label">Add</span>
      </button>

      {open && (
        <>
          <div
            className="add-source-backdrop"
            onClick={() => setOpen(false)}
          />
          <div
            ref={popoverRef}
            className="add-source-popover"
            role="dialog"
            aria-label="Add a source"
          >
          <div className="add-source-tabs" role="tablist">
            <TabButton active={tab === "text"} onClick={() => setTab("text")}>
              Paste text
            </TabButton>
            <TabButton active={tab === "file"} onClick={() => setTab("file")}>
              Upload file
            </TabButton>
            <TabButton active={tab === "url"} onClick={() => setTab("url")}>
              URL
            </TabButton>
          </div>

          <div className="add-source-body">
            {tab === "text" && (
              <TextTab
                close={() => setOpen(false)}
                onAdded={onAdded}
              />
            )}
            {tab === "file" && (
              <FileTab
                close={() => setOpen(false)}
                onAdded={onAdded}
              />
            )}
            {tab === "url" && (
              <UrlTab
                close={() => setOpen(false)}
              />
            )}
          </div>
        </div>
        </>
      )}
    </div>
  );
}

function TabButton({
  active,
  onClick,
  children,
}: {
  active: boolean;
  onClick: () => void;
  children: React.ReactNode;
}) {
  return (
    <button
      type="button"
      role="tab"
      aria-selected={active}
      className="add-source-tab"
      onClick={onClick}
    >
      {children}
    </button>
  );
}

// ── Tab: Paste text ─────────────────────────────────────────────────────

function TextTab({
  close,
  onAdded,
}: {
  close: () => void;
  onAdded?: (path: string) => void;
}) {
  const [title, setTitle] = useState("");
  const [body, setBody] = useState("");
  const fs = useWorkspaceFiles();

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    if (!body.trim()) return;
    const slug = slugify(title.trim() || body.slice(0, 40));
    const path = `attachments/${slug}.md`;
    const frontmatter = buildFrontmatter({
      title: title.trim() || slug,
      added_at: nowIso(),
    });
    const content = `${frontmatter}# ${title.trim() || slug}\n\n${body.trim()}\n`;
    try {
      await fs.writeFile(path, content, { autoApprove: true });
      requestToast(`Saved ${path}`, "success");
      onAdded?.(path);
      close();
    } catch (err) {
      requestToast(
        `Failed to save: ${err instanceof Error ? err.message : String(err)}`,
        "error",
      );
    }
  }

  return (
    <form onSubmit={submit} className="add-source-form">
      <label className="add-source-label">
        Title <span className="add-source-hint">(optional)</span>
      </label>
      <input
        type="text"
        value={title}
        onChange={(e) => setTitle(e.target.value)}
        placeholder="Notes from 2026 product meeting"
        className="add-source-input"
        autoFocus
      />
      <label className="add-source-label">Content</label>
      <textarea
        value={body}
        onChange={(e) => setBody(e.target.value)}
        placeholder="Paste any text. Markdown is supported."
        className="add-source-textarea"
        rows={8}
        required
      />
      <div className="add-source-actions">
        <button type="button" onClick={close} className="add-source-cancel">
          Cancel
        </button>
        <button
          type="submit"
          className="add-source-submit"
          disabled={fs.busy || !body.trim()}
        >
          {fs.busy ? "Saving..." : "Save source"}
        </button>
      </div>
    </form>
  );
}

// ── Tab: Upload file ────────────────────────────────────────────────────

function FileTab({
  close,
  onAdded,
}: {
  close: () => void;
  onAdded?: (path: string) => void;
}) {
  const fs = useWorkspaceFiles();
  const inputRef = useRef<HTMLInputElement | null>(null);
  const [dragOver, setDragOver] = useState(false);

  async function ingest(file: File) {
    try {
      const { path } = await fs.ingestFile(file);
      requestToast(`Saved ${path}`, "success");
      onAdded?.(path);
      close();
    } catch (err) {
      requestToast(
        `Failed: ${err instanceof Error ? err.message : String(err)}`,
        "error",
      );
    }
  }

  const busy = fs.busy;

  function onFiles(files: FileList | null) {
    if (!files || files.length === 0) return;
    void ingest(files[0]);
  }

  return (
    <div className="add-source-form">
      <div
        className={`add-source-drop ${dragOver ? "drag-over" : ""} ${busy ? "busy" : ""}`}
        onDragOver={(e) => {
          if (busy) return;
          e.preventDefault();
          setDragOver(true);
        }}
        onDragLeave={() => setDragOver(false)}
        onDrop={(e) => {
          if (busy) return;
          e.preventDefault();
          setDragOver(false);
          onFiles(e.dataTransfer?.files ?? null);
        }}
        onClick={() => !busy && inputRef.current?.click()}
        role="button"
        tabIndex={0}
        aria-disabled={busy}
        onKeyDown={(e) => {
          if (busy) return;
          if (e.key === "Enter" || e.key === " ") inputRef.current?.click();
        }}
      >
        <UploadIcon />
        <div className="add-source-drop-title">
          {busy ? "Extracting text..." : "Drop a file here, or click to browse"}
        </div>
        <div className="add-source-drop-sub">
          Any file format works. PDFs, DOCX, spreadsheets and slides are
          auto-extracted to text server-side. Plain text and markdown
          land as-is.
        </div>
        <input
          ref={inputRef}
          type="file"
          style={{ display: "none" }}
          onChange={(e) => onFiles(e.target.files)}
          disabled={busy}
        />
      </div>
      <div className="add-source-actions">
        <button
          type="button"
          onClick={close}
          className="add-source-cancel"
          disabled={busy}
        >
          {busy ? "Working..." : "Cancel"}
        </button>
      </div>
    </div>
  );
}

// ── Tab: URL ─────────────────────────────────────────────────────────────

function UrlTab({ close }: { close: () => void }) {
  const chat = useChat();
  const [url, setUrl] = useState("");

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    const trimmed = url.trim();
    if (!trimmed) return;
    try {
      await chat.send(`Ingest this source: ${trimmed}`);
      requestToast("Fetching the URL...", "info");
      close();
    } catch (err) {
      requestToast(
        `Failed to send: ${err instanceof Error ? err.message : String(err)}`,
        "error",
      );
    }
  }

  return (
    <form onSubmit={submit} className="add-source-form">
      <label className="add-source-label">URL</label>
      <input
        type="url"
        value={url}
        onChange={(e) => setUrl(e.target.value)}
        placeholder="https://example.com/article"
        className="add-source-input"
        autoFocus
        required
      />
      <p className="add-source-explainer">
        The agent fetches the page, extracts readable text, and saves it
        as a source. Watch the chat for progress and any errors. You
        can keep typing other questions while this runs.
      </p>
      <div className="add-source-actions">
        <button type="button" onClick={close} className="add-source-cancel">
          Cancel
        </button>
        <button
          type="submit"
          className="add-source-submit"
          disabled={chat.busy || !url.trim()}
        >
          {chat.busy ? "Sending..." : "Fetch"}
        </button>
      </div>
    </form>
  );
}

// ── Icons ────────────────────────────────────────────────────────────────

function PlusIcon() {
  return (
    <svg
      width="14"
      height="14"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
    >
      <path d="M12 5v14M5 12h14" />
    </svg>
  );
}

function UploadIcon() {
  return (
    <svg
      width="28"
      height="28"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.5"
      strokeLinecap="round"
      strokeLinejoin="round"
    >
      <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4" />
      <path d="M17 8l-5-5-5 5" />
      <path d="M12 3v12" />
    </svg>
  );
}
