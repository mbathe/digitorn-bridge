/**
 * Read-only code viewer with line numbers + path header. Drop-in
 * companion to `<WorkspaceFileTree>` for any app showing the agent's
 * code while it edits.
 *
 * Deliberately lean: no syntax highlighting (apps that want Monaco or
 * Shiki wrap this primitive or replace it). Ships premium typography
 * (JetBrains Mono), gutter line numbers, status badge in the header.
 *
 * ```tsx
 * const file = useFile(selected);
 * <WorkspaceCodeViewer
 *   path={selected}
 *   content={file?.content ?? null}
 *   status={file?.status}
 * />
 * ```
 */

import { createElement, useMemo, type CSSProperties } from "react";

export interface WorkspaceCodeViewerTokens {
  background: string;
  surface: string;
  border: string;
  textBright: string;
  textMuted: string;
  textDim: string;
  added: string;
  modified: string;
  deleted: string;
}

const _DEFAULT_TOKENS: WorkspaceCodeViewerTokens = {
  background: "var(--bg, #0b0e13)",
  surface: "var(--surface, #11151b)",
  border: "var(--border, rgba(255,255,255,0.06))",
  textBright: "var(--text-bright, #f1f3f6)",
  textMuted: "var(--text-muted, #9099a8)",
  textDim: "var(--text-dim, #5d6573)",
  added: "var(--green, #34d399)",
  modified: "var(--amber, #fbbf24)",
  deleted: "var(--red, #f87171)",
};

export type WorkspaceFileStatus = "added" | "modified" | "deleted" | undefined;

export interface WorkspaceCodeViewerProps {
  /** Path of the file being viewed. `null` shows the empty state. */
  path: string | null;
  /** File content. `null` shows the empty state. */
  content: string | null;
  /** Optional status — drives the badge in the header. */
  status?: WorkspaceFileStatus;
  /** Optional change summary shown next to the status badge. */
  insertions?: number;
  /** Optional change summary shown next to the status badge. */
  deletions?: number;
  /** Empty-state message. */
  emptyLabel?: string;
  /** Wrapper style override. */
  style?: CSSProperties;
  /** Wrapper className. */
  className?: string;
  /** Token overrides. */
  tokens?: WorkspaceCodeViewerTokens;
}

export function WorkspaceCodeViewer({
  path,
  content,
  status,
  insertions,
  deletions,
  emptyLabel = "Select a file to inspect.",
  style,
  className,
  tokens = _DEFAULT_TOKENS,
}: WorkspaceCodeViewerProps) {
  const lines = useMemo(() => (content ? content.split("\n") : []), [content]);
  const gutterWidth = useMemo(
    () => Math.max(2, String(lines.length).length) + 1,
    [lines.length],
  );

  const wrap: CSSProperties = {
    flex: 1,
    display: "flex",
    flexDirection: "column",
    background: tokens.background,
    overflow: "hidden",
    height: "100%",
    fontFamily: "var(--font-sans, 'IBM Plex Sans', system-ui, sans-serif)",
    ...style,
  };

  if (!path || content === null) {
    return createElement(
      "div",
      {
        className,
        style: {
          ...wrap,
          alignItems: "center",
          justifyContent: "center",
          color: tokens.textDim,
          fontSize: 13,
        },
      },
      emptyLabel,
    );
  }

  const header: CSSProperties = {
    display: "flex",
    alignItems: "center",
    gap: 12,
    padding: "10px 16px",
    background: tokens.surface,
    borderBottom: `1px solid ${tokens.border}`,
    fontSize: 12.5,
    color: tokens.textMuted,
    fontFamily: "ui-monospace, 'JetBrains Mono', monospace",
    flex: "0 0 auto",
  };

  const statusColor =
    status === "added"
      ? tokens.added
      : status === "modified"
      ? tokens.modified
      : status === "deleted"
      ? tokens.deleted
      : null;

  const statusBadge = statusColor
    ? createElement(
        "span",
        {
          style: {
            display: "inline-flex",
            alignItems: "center",
            gap: 5,
            padding: "2px 8px",
            borderRadius: 999,
            fontSize: 10.5,
            fontWeight: 500,
            textTransform: "uppercase" as const,
            letterSpacing: "0.04em",
            background: `color-mix(in oklab, ${statusColor} 15%, transparent)`,
            color: statusColor,
            fontFamily: "var(--font-sans, 'IBM Plex Sans', system-ui, sans-serif)",
          },
        },
        createElement("span", {
          style: { width: 5, height: 5, borderRadius: 999, background: statusColor },
        }),
        status,
      )
    : null;

  const diffStat =
    insertions != null || deletions != null
      ? createElement(
          "span",
          {
            style: {
              fontSize: 11,
              color: tokens.textDim,
              fontFamily: "ui-monospace, 'JetBrains Mono', monospace",
              marginLeft: "auto",
            },
          },
          insertions ? createElement("span", { style: { color: tokens.added } }, `+${insertions}`) : null,
          insertions != null && deletions != null
            ? createElement("span", { style: { margin: "0 4px" } }, " ")
            : null,
          deletions ? createElement("span", { style: { color: tokens.deleted } }, `-${deletions}`) : null,
        )
      : null;

  const codeShell: CSSProperties = {
    flex: 1,
    minHeight: 0,
    overflow: "auto",
    background: tokens.background,
    fontFamily: "ui-monospace, 'JetBrains Mono', 'Fira Code', monospace",
    fontSize: 12.5,
    lineHeight: 1.55,
    color: tokens.textBright,
  };

  const codeTable: CSSProperties = {
    display: "grid",
    gridTemplateColumns: `${gutterWidth}ch 1fr`,
    minWidth: "100%",
  };

  const gutterCell: CSSProperties = {
    color: tokens.textDim,
    background: tokens.background,
    textAlign: "right" as const,
    padding: "0 12px 0 16px",
    userSelect: "none" as const,
    fontVariantNumeric: "tabular-nums" as const,
    borderRight: `1px solid ${tokens.border}`,
  };

  const lineCell: CSSProperties = {
    padding: "0 16px",
    whiteSpace: "pre" as const,
  };

  return createElement(
    "section",
    { className, style: wrap },
    createElement(
      "header",
      { style: header },
      createElement("span", { style: { color: tokens.textBright } }, path),
      statusBadge,
      diffStat,
    ),
    createElement(
      "div",
      { style: codeShell },
      createElement(
        "div",
        { style: codeTable },
        lines.map((line, i) => [
          createElement(
            "div",
            { key: `g${i}`, style: gutterCell },
            String(i + 1),
          ),
          createElement(
            "div",
            { key: `l${i}`, style: lineCell },
            line.length === 0 ? " " : line,
          ),
        ]),
      ),
    ),
  );
}
