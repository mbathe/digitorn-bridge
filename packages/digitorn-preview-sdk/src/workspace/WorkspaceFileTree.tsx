/**
 * Live file tree for any app that lets users browse a workspace
 * powered by the SDK's `useFiles()` hook (lovable, builder, etc).
 *
 * Renders a Map<path, WorkspaceFile> as a collapsible folder/file
 * tree with:
 *   - Premium typography (Plex Sans for paths, mono for sizes).
 *   - Status indicators (added / modified / deleted dots).
 *   - Click → selects + fires `onSelect`.
 *   - CSS-var tokens so the consumer's theme drives the palette.
 *
 * Consumer plugs it in:
 *
 * ```tsx
 * import { useFiles } from "@digitorn/preview-sdk";
 * import { WorkspaceFileTree } from "@digitorn/preview-sdk";
 *
 * const files = useFiles();
 * const [selected, setSelected] = useState<string | null>(null);
 * return <WorkspaceFileTree files={files} selected={selected} onSelect={setSelected} />;
 * ```
 */

import { createElement, useMemo, useState, type CSSProperties } from "react";

import type { WorkspaceFile } from "../types.js";

export interface WorkspaceFileTreeTokens {
  /** Surface behind the tree (default `var(--surface, #11151b)`). */
  background: string;
  /** Hairline dividers + borders. */
  border: string;
  /** Bright text — selected path, file name. */
  textBright: string;
  /** Muted text — folder names, sizes, status. */
  textMuted: string;
  /** Dim text — empty states, labels. */
  textDim: string;
  /** Accent — selected item indicator. */
  accent: string;
  /** Status colors. */
  added: string;
  modified: string;
  deleted: string;
}

const _DEFAULT_TOKENS: WorkspaceFileTreeTokens = {
  background: "var(--surface, #11151b)",
  border: "var(--border, rgba(255,255,255,0.06))",
  textBright: "var(--text-bright, #f1f3f6)",
  textMuted: "var(--text-muted, #9099a8)",
  textDim: "var(--text-dim, #5d6573)",
  accent: "var(--accent-primary, #22d3ee)",
  added: "var(--green, #34d399)",
  modified: "var(--amber, #fbbf24)",
  deleted: "var(--red, #f87171)",
};

export interface WorkspaceFileTreeProps {
  /** Map produced by `useFiles()` — keyed by workspace-relative path. */
  files: Map<string, WorkspaceFile>;
  /** Currently focused path, drives the highlight + accent border. */
  selected: string | null;
  /** Fired on item click. */
  onSelect: (path: string) => void;
  /** Optional title shown above the tree. Default `"Files"`. */
  label?: string;
  /** Wrapper style override. */
  style?: CSSProperties;
  /** Wrapper className. */
  className?: string;
  /** Color tokens (CSS vars by default — override per-app theme). */
  tokens?: WorkspaceFileTreeTokens;
}

interface _TreeNode {
  name: string;
  fullPath: string;
  isFolder: boolean;
  children: _TreeNode[];
  file?: WorkspaceFile;
}

function _buildTree(
  files: Map<string, WorkspaceFile>,
): _TreeNode[] {
  const root: _TreeNode = { name: "", fullPath: "", isFolder: true, children: [] };
  const sorted = Array.from(files.entries()).sort(([a], [b]) => a.localeCompare(b));
  for (const [path, file] of sorted) {
    const parts = path.split("/").filter(Boolean);
    let cur = root;
    parts.forEach((part, i) => {
      const isLast = i === parts.length - 1;
      const segment = parts.slice(0, i + 1).join("/");
      let next = cur.children.find((c) => c.name === part);
      if (!next) {
        next = {
          name: part,
          fullPath: segment,
          isFolder: !isLast,
          children: [],
          ...(isLast ? { file } : {}),
        };
        cur.children.push(next);
      }
      cur = next;
    });
  }
  // Folders first, then files, alphabetical within each group.
  const sortNode = (node: _TreeNode) => {
    node.children.sort((a, b) => {
      if (a.isFolder !== b.isFolder) return a.isFolder ? -1 : 1;
      return a.name.localeCompare(b.name);
    });
    node.children.forEach(sortNode);
  };
  sortNode(root);
  return root.children;
}

const _FOLDER_ICON = (open: boolean) =>
  createElement(
    "svg",
    {
      "aria-hidden": true,
      width: 13,
      height: 13,
      viewBox: "0 0 16 16",
      fill: "none",
      style: { flex: "0 0 auto", opacity: 0.7 },
    },
    createElement("path", {
      d: open
        ? "M2 5.5A1.5 1.5 0 0 1 3.5 4h3l1.5 1.5h5A1.5 1.5 0 0 1 14.5 7v.25l-1.5 4.5A1.5 1.5 0 0 1 11.6 13H3.4A1.5 1.5 0 0 1 2 11.5v-6Z"
        : "M2 5.5A1.5 1.5 0 0 1 3.5 4h3l1.5 1.5h5A1.5 1.5 0 0 1 14.5 7v4.5A1.5 1.5 0 0 1 13 13H3.5A1.5 1.5 0 0 1 2 11.5v-6Z",
      stroke: "currentColor",
      strokeWidth: 1.2,
      strokeLinejoin: "round",
    }),
  );

const _CHEVRON = (open: boolean) =>
  createElement(
    "svg",
    {
      "aria-hidden": true,
      width: 9,
      height: 9,
      viewBox: "0 0 9 9",
      fill: "none",
      style: {
        flex: "0 0 auto",
        opacity: 0.55,
        transform: open ? "rotate(90deg)" : "rotate(0deg)",
        transition: "transform 120ms ease",
      },
    },
    createElement("path", {
      d: "M2.5 1.5L6 4.5L2.5 7.5",
      stroke: "currentColor",
      strokeWidth: 1.3,
      strokeLinecap: "round",
      strokeLinejoin: "round",
      fill: "none",
    }),
  );

function _fileIcon(language: string | undefined): string {
  switch (language) {
    case "tsx":
    case "jsx":
      return "⟨⟩";
    case "ts":
    case "js":
      return "𝙹𝚂";
    case "css":
    case "scss":
      return "▦";
    case "html":
      return "◇";
    case "json":
      return "{}";
    case "md":
    case "mdx":
      return "¶";
    case "py":
      return "py";
    case "go":
      return "go";
    case "rs":
      return "rs";
    case "yaml":
    case "yml":
      return "≣";
    default:
      return "·";
  }
}

export function WorkspaceFileTree({
  files,
  selected,
  onSelect,
  label = "Files",
  style,
  className,
  tokens = _DEFAULT_TOKENS,
}: WorkspaceFileTreeProps) {
  const tree = useMemo(() => _buildTree(files), [files]);
  const [openFolders, setOpenFolders] = useState<Set<string>>(
    () => new Set(_initialOpenFolders(files)),
  );

  function toggleFolder(path: string) {
    setOpenFolders((prev) => {
      const next = new Set(prev);
      if (next.has(path)) next.delete(path);
      else next.add(path);
      return next;
    });
  }

  const wrap: CSSProperties = {
    display: "flex",
    flexDirection: "column",
    background: tokens.background,
    borderRight: `1px solid ${tokens.border}`,
    overflow: "hidden",
    height: "100%",
    fontFamily: "var(--font-sans, 'IBM Plex Sans', system-ui, sans-serif)",
    ...style,
  };
  const head: CSSProperties = {
    padding: "10px 14px",
    fontSize: 11,
    fontWeight: 500,
    letterSpacing: "0.10em",
    textTransform: "uppercase" as const,
    color: tokens.textDim,
    borderBottom: `1px solid ${tokens.border}`,
    display: "flex",
    justifyContent: "space-between",
    alignItems: "center",
  };
  const body: CSSProperties = { flex: 1, overflow: "auto", padding: "6px 0" };

  return createElement(
    "aside",
    { className, style: wrap },
    createElement(
      "div",
      { style: head },
      createElement("span", null, label),
      createElement(
        "span",
        { style: { color: tokens.textDim, fontSize: 11 } },
        files.size,
      ),
    ),
    createElement(
      "div",
      { style: body },
      files.size === 0
        ? createElement(
            "div",
            {
              style: {
                padding: "32px 16px",
                color: tokens.textDim,
                fontSize: 12,
                textAlign: "center" as const,
                lineHeight: 1.5,
              },
            },
            "No files yet.",
            createElement("br"),
            "Ask the agent to build something.",
          )
        : tree.map((node) =>
            _renderNode(
              node,
              0,
              selected,
              onSelect,
              openFolders,
              toggleFolder,
              tokens,
            ),
          ),
    ),
  );
}

function _renderNode(
  node: _TreeNode,
  depth: number,
  selected: string | null,
  onSelect: (path: string) => void,
  openFolders: Set<string>,
  toggleFolder: (path: string) => void,
  tokens: WorkspaceFileTreeTokens,
): React.ReactNode {
  if (node.isFolder) {
    const open = openFolders.has(node.fullPath);
    return createElement(
      "div",
      { key: node.fullPath },
      createElement(
        "div",
        {
          onClick: () => toggleFolder(node.fullPath),
          style: {
            display: "flex",
            alignItems: "center",
            gap: 6,
            padding: `4px 12px 4px ${12 + depth * 14}px`,
            fontSize: 12.5,
            color: tokens.textMuted,
            cursor: "pointer",
            userSelect: "none" as const,
          },
        },
        _CHEVRON(open),
        createElement("span", { style: { color: tokens.textDim, display: "inline-flex" } }, _FOLDER_ICON(open)),
        createElement("span", { style: { fontWeight: 450 } }, node.name),
      ),
      open
        ? node.children.map((child) =>
            _renderNode(child, depth + 1, selected, onSelect, openFolders, toggleFolder, tokens),
          )
        : null,
    );
  }

  const isSelected = selected === node.fullPath;
  const status = node.file?.status;
  const statusColor =
    status === "added"
      ? tokens.added
      : status === "modified"
      ? tokens.modified
      : status === "deleted"
      ? tokens.deleted
      : null;

  return createElement(
    "button",
    {
      key: node.fullPath,
      type: "button",
      onClick: () => onSelect(node.fullPath),
      style: {
        all: "unset",
        boxSizing: "border-box" as const,
        width: "100%",
        display: "flex",
        alignItems: "center",
        gap: 8,
        padding: `5px 12px 5px ${12 + depth * 14 + 9 /* chevron width */}px`,
        background: isSelected ? "rgba(34, 211, 238, 0.08)" : "transparent",
        color: isSelected ? tokens.textBright : tokens.textMuted,
        cursor: "pointer",
        fontSize: 12.5,
        fontWeight: isSelected ? 500 : 400,
        position: "relative" as const,
      },
    },
    isSelected
      ? createElement("span", {
          style: {
            position: "absolute" as const,
            left: 0,
            top: 6,
            bottom: 6,
            width: 2,
            background: tokens.accent,
            borderRadius: "0 2px 2px 0",
          },
        })
      : null,
    createElement(
      "span",
      {
        style: {
          fontFamily: "ui-monospace, 'JetBrains Mono', monospace",
          fontSize: 10,
          color: tokens.textDim,
          width: 16,
          textAlign: "center" as const,
          flex: "0 0 auto",
        },
      },
      _fileIcon(node.file?.language),
    ),
    createElement(
      "span",
      {
        style: {
          flex: 1,
          overflow: "hidden",
          textOverflow: "ellipsis",
          whiteSpace: "nowrap" as const,
          textAlign: "left" as const,
          letterSpacing: "-0.005em",
        },
      },
      node.name,
    ),
    statusColor
      ? createElement("span", {
          "aria-label": status,
          style: {
            width: 6,
            height: 6,
            borderRadius: 999,
            background: statusColor,
            flex: "0 0 auto",
          },
        })
      : null,
    node.file && !statusColor
      ? createElement(
          "span",
          {
            style: {
              fontSize: 10,
              color: tokens.textDim,
              fontFamily: "ui-monospace, monospace",
              flex: "0 0 auto",
            },
          },
          `${(node.file.size / 1024).toFixed(1)}k`,
        )
      : null,
  );
}

// Auto-open top-level folders + folders that contain a freshly
// added/modified file, so the user sees the agent's work without
// having to click around.
function _initialOpenFolders(
  files: Map<string, WorkspaceFile>,
): string[] {
  const out = new Set<string>();
  const topDirs = new Set<string>();
  for (const [path, file] of files) {
    const parts = path.split("/").filter(Boolean);
    if (parts.length > 1) topDirs.add(parts[0]);
    if (file.status === "added" || file.status === "modified") {
      let acc = "";
      for (let i = 0; i < parts.length - 1; i++) {
        acc = acc ? `${acc}/${parts[i]}` : parts[i];
        out.add(acc);
      }
    }
  }
  for (const d of topDirs) out.add(d);
  return Array.from(out);
}
