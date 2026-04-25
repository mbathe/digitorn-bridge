import { useRef, useState } from "react";
import {
  useWorkspaceSnapshot,
  useWorkspacePersistence,
} from "@digitorn/preview-sdk";

/**
 * Header-bar tools for saving / forking / importing the current session's
 * workspace. Exercises the WSP05/06/07 endpoints end-to-end through the SDK.
 */
export default function WorkspaceMenu() {
  const ws = useWorkspaceSnapshot();
  const { hasPendingWrites, lastSavedAt } = useWorkspacePersistence();
  const [message, setMessage] = useState<string | null>(null);
  const fileInput = useRef<HTMLInputElement>(null);

  const flash = (msg: string) => {
    setMessage(msg);
    window.setTimeout(() => setMessage(null), 2500);
  };

  const onSave = async () => {
    try {
      await ws.downloadSnapshot();
      flash("Saved ⇣");
    } catch (e) {
      flash(`Save failed: ${(e as Error).message}`);
    }
  };

  const onFork = async () => {
    try {
      const r = await ws.forkSession({ title: "Fork" });
      flash(`Forked → ${r.session_id.slice(0, 8)} (${r.files} files)`);
    } catch (e) {
      flash(`Fork failed: ${(e as Error).message}`);
    }
  };

  const onImportClick = () => fileInput.current?.click();

  const onImportFile = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const f = e.target.files?.[0];
    if (!f) return;
    try {
      const r = await ws.importFromFile(f, { replace: true });
      flash(`Imported ${r.files} files`);
    } catch (err) {
      flash(`Import failed: ${(err as Error).message}`);
    } finally {
      e.target.value = "";
    }
  };

  const savingIndicator = (() => {
    if (hasPendingWrites) return "Saving…";
    if (lastSavedAt) {
      const d = new Date(lastSavedAt);
      const hh = String(d.getHours()).padStart(2, "0");
      const mm = String(d.getMinutes()).padStart(2, "0");
      return `Saved ✓ ${hh}:${mm}`;
    }
    return "";
  })();

  return (
    <div style={styles.wrap}>
      <span
        style={{
          ...styles.badge,
          color: hasPendingWrites ? "#fbbf24" : "#64748b",
        }}
      >
        {savingIndicator}
      </span>
      <button style={styles.btn} onClick={onSave} disabled={ws.busy}>
        Save a copy
      </button>
      <button style={styles.btn} onClick={onFork} disabled={ws.busy}>
        Fork
      </button>
      <button style={styles.btn} onClick={onImportClick} disabled={ws.busy}>
        Import…
      </button>
      <input
        ref={fileInput}
        type="file"
        accept="application/json"
        style={{ display: "none" }}
        onChange={onImportFile}
      />
      {message && <span style={styles.toast}>{message}</span>}
      {ws.error && !message && (
        <span style={{ ...styles.toast, color: "#ef4444" }}>
          {ws.error.message.slice(0, 80)}
        </span>
      )}
    </div>
  );
}

const styles: Record<string, React.CSSProperties> = {
  wrap: {
    display: "flex",
    alignItems: "center",
    gap: 8,
  },
  badge: {
    fontFamily: "monospace",
    fontSize: 10,
    minWidth: 80,
    textAlign: "right",
  },
  btn: {
    background: "#1e293b",
    color: "#e2e8f0",
    border: "1px solid #334155",
    borderRadius: 4,
    padding: "4px 10px",
    fontSize: 11,
    cursor: "pointer",
  },
  toast: {
    fontFamily: "monospace",
    fontSize: 10,
    color: "#22c55e",
    marginLeft: 8,
  },
};
