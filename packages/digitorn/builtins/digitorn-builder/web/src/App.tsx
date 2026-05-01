import { useCallback, useEffect, useMemo, useState, useRef } from "react";
import ReactFlow, {
  Background,
  BackgroundVariant,
  Controls,
  MiniMap,
  useEdgesState,
  useNodesState,
  useReactFlow,
  ReactFlowProvider,
  type Node as RFNode,
  type Edge as RFEdge,
} from "reactflow";
import "reactflow/dist/style.css";
import { toPng } from "html-to-image";
import clsx from "clsx";

import { useFile, readSession } from "@digitorn/preview-sdk";
import { parseYamlToGraph, type NodeData } from "./lib/yaml-to-graph";
import { buildExtraNodes } from "./lib/extra-nodes";
import yaml from "js-yaml";
// Dev-only fixture: when running standalone (no live session) Vite injects this
// raw YAML so we can render the canvas without the daemon proxying a real app.
// In production builds the import is tree-shaken when not used.
// @ts-ignore vite ?raw import
import devFixtureYaml from "./fixtures/digitorn-code.yaml?raw";
import { autoLayout, type LayoutDir } from "./lib/auto-layout";
import { laneLayout } from "./lib/lane-layout";
import { enrichNodes, type EnrichedNodeData } from "./lib/enrich-graph";
import { useTheme } from "./lib/useTheme";
import { validateApp, worstSeverityByNode } from "./lib/validate";
import { beginnerLabelFor } from "./lib/glossary";
import { dimNodesForView, type ViewMode } from "./lib/view-modes";
import { buildStoryScript } from "./lib/story-script";
import { buildSequenceDiagram } from "./lib/sequence-diagram";
import {
  setAtPath, deleteAtPath, dumpYaml, getAtPath, pathForNodeId, checkRoundTrip,
  loadYamlDoc, stringifyYamlDoc, setAtPathDoc, deleteAtPathDoc,
} from "./lib/yaml-edit";
import { resolveConnect, isAllowedConnect } from "./lib/connect-resolver";
import { detectRename, rippleRename } from "./lib/rename-ripple";
import { validateSchema, blockingIssues } from "./lib/schema-validate";
import { useUndoStack } from "./lib/useUndoStack";
import { TEMPLATES, type NodeTemplate } from "./lib/templates";

import CustomNode from "./components/CustomNode";
import AgentGroupNode from "./components/AgentGroupNode";
import MCPGroupNode from "./components/MCPGroupNode";
import ChannelGroupNode from "./components/ChannelGroupNode";
import Inspector from "./components/Inspector";
import Toolbar, { type LayoutMode, type DensityMode } from "./components/Toolbar";
import EmptyState from "./components/EmptyState";
import StoryRunner from "./components/StoryRunner";
import SequenceDiagram from "./components/SequenceDiagram";
import PalettePanel from "./components/PalettePanel";
import TutorialOverlay from "./components/TutorialOverlay";
import TestPromptPanel from "./components/TestPromptPanel";
import YamlPane from "./components/YamlPane";
import EmptyCanvas from "./components/EmptyCanvas";
import SearchPalette from "./components/SearchPalette";
import PresetGallery from "./components/PresetGallery";
import OutlineTree from "./components/OutlineTree";
import NodeContextMenu from "./components/NodeContextMenu";
import ToolCallBubble from "./components/ToolCallBubble";
import { groupSubAgents, AGENT_GROUP_NODE_TYPE } from "./lib/group-agents";
import { useLiveStatus } from "./lib/useLiveStatus";

// Existing widgets (kept as-is, just composed into the new layout)
import CompileStatus from "./components/CompileStatus";
import ConnectionBadge from "./components/ConnectionBadge";
import WorkspaceMenu from "./components/WorkspaceMenu";
import FilesMenu from "./components/FilesMenu";
import EdgeLegend from "./components/EdgeLegend";
import SchemaReferencePanel from "./components/SchemaReferencePanel";
import AutoTestPanel from "./components/AutoTestPanel";

const session = readSession();
const nodeTypes = {
  customNode: CustomNode,
  [AGENT_GROUP_NODE_TYPE]: AgentGroupNode,
  mcpGroup: MCPGroupNode,
  channelGroup: ChannelGroupNode,
};

function CanvasInner() {
  const { theme, toggle } = useTheme();
  const liveYaml = useFile("app.yaml");
  // In `_dev_` standalone mode (Vite preview without a real session) the
  // file API never resolves — fall back to the bundled fixture so the
  // builder team can iterate on the canvas without the full daemon stack.
  const sourceYaml =
    liveYaml ?? (session.sessionId === "_dev_" ? (devFixtureYaml as string) : null);
  // Local override of the YAML — undo/redo enabled. `null` means
  // "no edits, use sourceYaml". Each onYamlEdit dispatches a commit.
  const undoStack = useUndoStack<string | null>(null, { limit: 200, coalesceMs: 600 });
  const editedYaml = undoStack.value;
  const setEditedYaml = (v: string | null) => undoStack.commit(v);
  const yamlContent = editedYaml ?? sourceYaml;
  // When the upstream YAML changes (preview SDK pushed a new file),
  // reset the local edits so we re-render the latest source AND
  // clear the undo history (those snapshots are now stale).
  useEffect(() => { undoStack.reset(null); }, [sourceYaml]);  // eslint-disable-line react-hooks/exhaustive-deps

  // ── Autosave: persist editedYaml to localStorage so a tab reload
  // doesn't wipe in-progress work. The key includes the session id +
  // the source yaml's hash so we don't restore stale edits across
  // different source apps.
  const autosaveKey = useMemo(() => {
    if (!sourceYaml) return null;
    let hash = 0;
    for (let i = 0; i < Math.min(sourceYaml.length, 1000); i++) {
      hash = ((hash << 5) - hash + sourceYaml.charCodeAt(i)) | 0;
    }
    return `digitorn-builder:edits:${session.sessionId}:${Math.abs(hash)}`;
  }, [sourceYaml]);
  // Persist on every edit (debounced via undoStack's coalesce).
  useEffect(() => {
    if (!autosaveKey) return;
    if (editedYaml == null) {
      try { localStorage.removeItem(autosaveKey); } catch { /* quota / disabled */ }
      return;
    }
    try { localStorage.setItem(autosaveKey, editedYaml); } catch { /* quota / disabled */ }
  }, [autosaveKey, editedYaml]);
  // Restore on first mount of a new source.
  const [restoredFromAutosave, setRestoredFromAutosave] = useState<string | null>(null);
  useEffect(() => {
    if (!autosaveKey) return;
    try {
      const saved = localStorage.getItem(autosaveKey);
      if (saved && saved !== sourceYaml && saved !== editedYaml) {
        undoStack.commit(saved);
        setRestoredFromAutosave(new Date().toLocaleTimeString());
      }
    } catch { /* ignore */ }
  }, [autosaveKey]);  // eslint-disable-line react-hooks/exhaustive-deps
  const rf = useReactFlow();

  // ── Parse + enrich + merge extra nodes + group sub-agents ────
  const { rawNodes, rawEdges, appName, error, parsedDoc } = useMemo(() => {
    const result = parseYamlToGraph(yamlContent ?? "");
    const name = result.parsed?.app?.name ?? result.parsed?.app?.app_id ?? "App";
    const enriched = enrichNodes(result.nodes);
    const { nodes: extraNodes, edges: extraEdges } = buildExtraNodes(result.parsed);
    const merged = [...enriched, ...(extraNodes as typeof enriched)];
    const mergedEdges = [...result.edges, ...extraEdges];

    // Decorate module nodes with `approveActions` so CustomNode renders
    // a "🔒 N needs approval" badge. The decoration happens after merge
    // because `module-*` nodes are emitted by the parser, not by
    // extra-nodes — `buildExtraNodes` doesn't see them.
    const approves = (result.parsed?.capabilities?.approve ?? []) as Array<{ module?: string; actions?: string[] }>;
    const approveByModule = new Map<string, string[]>();
    for (const a of approves) {
      if (a.module && Array.isArray(a.actions)) {
        approveByModule.set(a.module, a.actions);
      }
    }
    // direct_modules — the modules whose tools are exposed natively to
    // the LLM (not routed through context_builder). Surface as a badge.
    const directList = (result.parsed?.execution as { direct_modules?: string[] } | undefined)?.direct_modules ?? [];
    const directSet = new Set(directList);
    for (const n of merged) {
      if (n.id.startsWith("module-")) {
        const modName = n.id.replace(/^module-/, "");
        const acts = approveByModule.get(modName);
        if (acts && acts.length > 0) {
          (n.data as unknown as Record<string, unknown>).approveActions = acts;
        }
        if (directSet.has(modName)) {
          (n.data as unknown as Record<string, unknown>).isDirect = true;
        }
      }
    }

    // Generic helper: convert a parser-emitted node into a group container
    // when it has children (`parentNode` set on them). The container's
    // type drives which renderer ReactFlow uses. Children get a grid
    // layout inside the parent.
    //
    // When children count > 8, only the first 8 are positioned inside;
    // the rest get marked with `data.hidden = true` so ReactFlow skips
    // them in the rendered output (the user can still find them via
    // the Outline tree + cmd+K palette). The container subtitle
    // surfaces "+N more" so the truncation is visible.
    const CONTAINER_CHILD_CAP = 8;
    const flipToGroup = (
      parentId: string,
      groupType: "mcpGroup" | "channelGroup",
      labelOverride?: string,
      childPrefix?: string,
    ) => {
      const allChildren = merged.filter(
        (n) => n.parentNode === parentId && (!childPrefix || n.id.startsWith(childPrefix)),
      );
      if (allChildren.length === 0) return;
      const parent = merged.find((n) => n.id === parentId);
      if (!parent) return;
      parent.type = groupType;
      const visibleChildren = allChildren.slice(0, CONTAINER_CHILD_CAP);
      const hiddenChildren = allChildren.slice(CONTAINER_CHILD_CAP);
      // Hide the overflow children so ReactFlow doesn't paint them
      // outside the container bounds.
      for (const h of hiddenChildren) (h as RFNode & { hidden?: boolean }).hidden = true;
      const COL_W = 220;
      const ROW_H = 70;
      const PAD = 18;
      const COLS = visibleChildren.length > 4 ? 3 : 2;
      const rows = Math.ceil(visibleChildren.length / COLS);
      const containerW = PAD * 2 + COLS * COL_W + (COLS - 1) * 12;
      const containerH = PAD * 2 + 24 + rows * ROW_H + (rows - 1) * 12;
      parent.style = { ...(parent.style ?? {}), width: containerW, height: containerH };
      visibleChildren.forEach((child, i) => {
        const col = i % COLS;
        const row = Math.floor(i / COLS);
        child.position = { x: PAD + col * (COL_W + 12), y: PAD + 24 + row * (ROW_H + 12) };
      });
      const d = parent.data as unknown as Record<string, unknown>;
      if (labelOverride) d.label = labelOverride;
      const total = allChildren.length;
      d.subtitle = hiddenChildren.length > 0
        ? `${total} items · +${hiddenChildren.length} hidden (use Outline / ⌘K)`
        : `${total} ${total > 1 ? "items" : "item"}`;
    };

    // module-mcp → mcpGroup (servers as children)
    flipToGroup("module-mcp", "mcpGroup", "MCP", "mcp-");
    (merged.find((n) => n.id === "module-mcp")?.data as unknown as Record<string, unknown> | undefined)
      && (() => {
        const p = merged.find((n) => n.id === "module-mcp")!;
        const cnt = merged.filter((n) => n.parentNode === "module-mcp").length;
        if (cnt > 0) (p.data as unknown as Record<string, unknown>).subtitle = `${cnt} server${cnt > 1 ? "s" : ""}`;
      })();

    // channel-<name> → channelGroup (providers as children) — for every
    // declared channel that has providers under config.providers.
    const declaredChannels = Object.keys(result.parsed?.channels ?? {});
    for (const chName of declaredChannels) {
      flipToGroup(`channel-${chName}`, "channelGroup", undefined, `provider-${chName}-`);
      const p = merged.find((n) => n.id === `channel-${chName}`);
      if (p) {
        const cnt = merged.filter((n) => n.parentNode === `channel-${chName}`).length;
        if (cnt > 0) (p.data as unknown as Record<string, unknown>).subtitle = `${cnt} provider${cnt > 1 ? "s" : ""}`;
      }
    }

    // ── Wiring: agent.delegate_to → coordinator → specialist edges ─
    // When `agents[i].delegate_to: [agentB, agentC]`, draw explicit
    // "delegates" edges so the user SEES the spawn hierarchy on the
    // canvas (instead of having to read the YAML to find them).
    for (const agent of result.parsed?.agents ?? []) {
      const aid = (agent as { id?: string; delegate_to?: string[] }).id;
      const delegates = (agent as { delegate_to?: string[] }).delegate_to;
      if (!aid || !Array.isArray(delegates)) continue;
      for (const target of delegates) {
        if (typeof target !== "string") continue;
        const sourceId = `agent-${aid}`;
        const targetId = `agent-${target}`;
        if (!merged.some((n) => n.id === sourceId)) continue;
        if (!merged.some((n) => n.id === targetId)) continue;
        // Avoid duplicate edges with the agent_spawn parser-emitted ones.
        const exists = mergedEdges.some(
          (e) => e.source === sourceId && e.target === targetId && (e.label === "spawns" || e.label === "delegates"),
        );
        if (exists) continue;
        mergedEdges.push({
          id: `e-delegate-${sourceId}-${targetId}`,
          source: sourceId,
          target: targetId,
          label: "delegates",
          type: "smoothstep",
          animated: false,
          data: { edgeKind: "callback" },
          style: {
            stroke: "rgb(168, 85, 247)",
            strokeWidth: 1.5,
            strokeDasharray: "6 3",
            opacity: 0.85,
          },
          labelStyle: { fontSize: 10, fontWeight: 500, fill: "rgb(216, 180, 254)" },
          labelBgStyle: { fill: "rgb(14, 22, 36)", fillOpacity: 0.85 },
          labelBgPadding: [4, 2],
          labelBgBorderRadius: 4,
        });
      }
    }

    // ── Wiring: trigger.channel → channel-X edge ──────────────────
    // When execution.triggers[i].channel === "X", add a real edge
    // from the trigger node → channel-X so the user SEES the
    // channel-as-input wiring (background mode where a channel's
    // incoming events start sessions).
    //
    // The parser names trigger nodes `trigger-exec-<index>`, not
    // `trigger-<id>` — so we walk by index into the triggers array.
    const triggers = ((result.parsed?.execution as { triggers?: Array<{ id?: string; channel?: string }> } | undefined)?.triggers) ?? [];
    triggers.forEach((t, i) => {
      if (!t.channel) return;
      const sourceId = `trigger-exec-${i}`;
      const targetId = `channel-${t.channel}`;
      const hasSource = merged.some((n) => n.id === sourceId);
      const hasTarget = merged.some((n) => n.id === targetId);
      if (hasSource && hasTarget) {
        mergedEdges.push({
          id: `e-${sourceId}-${targetId}`,
          source: sourceId,
          target: targetId,
          label: "via",
          type: "smoothstep",
          animated: false,
          data: { edgeKind: "temporal" },
          style: { stroke: "rgb(56, 189, 248)", strokeWidth: 1.4, opacity: 0.85 },
          labelStyle: { fontSize: 10, fontWeight: 500, fill: "rgb(125, 211, 252)" },
          labelBgStyle: { fill: "rgb(14, 22, 36)", fillOpacity: 0.85 },
        });
      }
    });

    // ── Wiring: channel-as-input → entry agent (background mode) ──
    // In background apps, channels CAN be the input source: incoming
    // messages activate the entry agent. We can't know for sure at
    // YAML parse time, but if execution.mode === "background" AND a
    // channel is referenced by a trigger, draw the input edge.
    const execMode = (result.parsed?.execution as { mode?: string } | undefined)?.mode;
    const entryId = result.parsed?.execution?.entry_agent ?? result.parsed?.agents?.[0]?.id;
    if (execMode === "background" && entryId) {
      const triggerChannels = new Set(triggers.map((t) => t.channel).filter(Boolean));
      for (const chName of triggerChannels) {
        const chId = `channel-${chName}`;
        const targetId = `agent-${entryId}`;
        if (merged.some((n) => n.id === chId) && merged.some((n) => n.id === targetId)) {
          mergedEdges.push({
            id: `e-${chId}-${targetId}`,
            source: chId,
            target: targetId,
            label: "feeds turns",
            type: "smoothstep",
            animated: false,
            data: { edgeKind: "temporal" },
            style: { stroke: "rgb(56, 189, 248)", strokeWidth: 1.4, opacity: 0.85, strokeDasharray: "4 3" },
            labelStyle: { fontSize: 10, fontWeight: 500, fill: "rgb(125, 211, 252)" },
            labelBgStyle: { fill: "rgb(14, 22, 36)", fillOpacity: 0.85 },
          });
        }
      }
    }

    const grouped = groupSubAgents(result.parsed, merged, mergedEdges);
    return {
      rawNodes: grouped.nodes as typeof enriched,
      rawEdges: grouped.edges,
      appName: name,
      error: result.error,
      parsedDoc: result.parsed,
    };
  }, [yamlContent]);

  // ── Layout mode + direction ──────────────────────────────────
  // Lifecycle "lanes" is the default — it shows the WHOLE flow
  // (Inputs → Palette → Middleware → Behavior → Capabilities →
  // Agents → Tools → Hooks → Outputs) so the user never has to
  // hunt for the next stage.
  const [layoutMode, setLayoutMode] = useState<LayoutMode>("lanes");
  const [layoutDir, setLayoutDir] = useState<LayoutDir>("LR");
  const [paletteExpanded, setPaletteExpanded] = useState(false);
  const [viewMode, setViewMode] = useState<ViewMode>("architecture");
  const [beginnerMode, setBeginnerMode] = useState(false);
  const [density, setDensity] = useState<DensityMode>("comfortable");
  const [densityUserSet, setDensityUserSet] = useState(false);
  // Auto-adapt density when the graph gets large. The user can still
  // override (we track densityUserSet so we never fight a manual choice).
  const effectiveDensity: DensityMode = useMemo(() => {
    if (densityUserSet) return density;
    if (rawNodes.length > 80) return "list";
    if (rawNodes.length > 30) return "compact";
    return density;
  }, [density, densityUserSet, rawNodes.length]);
  const [storyOpen, setStoryOpen] = useState(false);
  const [storyPlaying, setStoryPlaying] = useState(false);
  const [storyActive, setStoryActive] = useState<{
    nodeIds: Set<string>;
    edgePairs: Array<[string, string]>;
  }>({ nodeIds: new Set(), edgePairs: [] });

  // Validation issues — recomputed from the parsed YAML.
  const validationIssues = useMemo(() => validateApp(parsedDoc), [parsedDoc]);
  const validationByNode = useMemo(
    () => worstSeverityByNode(validationIssues),
    [validationIssues],
  );

  // Story script tracks the current YAML.
  const storySteps = useMemo(() => buildStoryScript(parsedDoc), [parsedDoc]);

  // ── Round-trip integrity check ──────────────────────────────────
  // When user edits via Inspector, we dump JSON → YAML which can lose
  // comments / formatting. Surface a warning so the user knows.
  const roundTripStatus = useMemo(() => {
    if (!sourceYaml || !editedYaml || sourceYaml === editedYaml) return null;
    return checkRoundTrip(sourceYaml, editedYaml);
  }, [sourceYaml, editedYaml]);

  // Sequence diagram derived from the YAML — used by the Sequence view.
  const [sequenceScenario, setSequenceScenario] = useState<"classic" | "approval-denied" | "error-fallback" | "fan-out">("classic");
  const sequenceDiagram = useMemo(
    () => buildSequenceDiagram(parsedDoc, sequenceScenario),
    [parsedDoc, sequenceScenario],
  );

  // ── Editable Inspector wiring ────────────────────────────────────
  // When the user edits a field, we mutate the parsed YAML at the
  // dotted path and dump it back to text. The text re-flows through
  // `parsedDoc` on the next render — so the canvas updates without
  // the inspector having to call back specific node updaters.
  const onYamlEdit = useCallback((path: string, value: unknown) => {
    if (!parsedDoc) return;
    // Comment-preserving fast path: try mutating the AST in place
    // on the CURRENT yamlContent. Falls back to JSON dump only if
    // the path doesn't exist in the AST yet (fresh template).
    const rename = detectRename(path, parsedDoc as Record<string, unknown>, value);
    if (yamlContent && !rename) {
      const yd = loadYamlDoc(yamlContent);
      if (setAtPathDoc(yd, path, value)) {
        setEditedYaml(stringifyYamlDoc(yd));
        return;
      }
    }
    // Fallback: full JSON → js-yaml dump (loses comments).
    let next = setAtPath(parsedDoc, path, value);
    if (rename) {
      next = rippleRename(next as Record<string, unknown>, rename.kind, rename.oldName, rename.newName) as typeof next;
    }
    setEditedYaml(dumpYaml(next));
  }, [parsedDoc, yamlContent]);
  const onYamlDelete = useCallback((path: string) => {
    if (!parsedDoc) return;
    if (yamlContent) {
      const yd = loadYamlDoc(yamlContent);
      if (deleteAtPathDoc(yd, path)) {
        setEditedYaml(stringifyYamlDoc(yd));
        return;
      }
    }
    const next = deleteAtPath(parsedDoc, path);
    setEditedYaml(dumpYaml(next));
  }, [parsedDoc, yamlContent]);
  const onResetYaml = useCallback(() => setEditedYaml(null), []);
  const onConnectEdge = useCallback((conn: { source: string | null; target: string | null }) => {
    if (!parsedDoc || !conn.source || !conn.target) return;
    // Build a kind lookup from rawNodes (the merged result) — declared
    // earlier in this component so we don't have a temporal-dead-zone
    // issue with the `nodes` state.
    const kindOf = (id: string): string | null => {
      const n = rawNodes.find((x) => x.id === id);
      return (n?.data?.kind as string | undefined) ?? null;
    };
    const m = resolveConnect(conn.source, conn.target, parsedDoc, { kindOf });
    if (!m) return;
    const next = setAtPath(parsedDoc, m.path, m.value);
    setEditedYaml(dumpYaml(next));
  }, [parsedDoc, rawNodes]);
  const onAddTemplate = useCallback((tpl: NodeTemplate) => {
    if (!parsedDoc) return;
    const instance = tpl.template();
    let next = parsedDoc;
    if (tpl.parentPath === "" && tpl.defaultKey) {
      // Top-level singleton — only insert if it doesn't already exist.
      if (getAtPath(next, tpl.defaultKey) !== undefined) {
        // Already declared — focus it instead of overwriting.
        return;
      }
      next = setAtPath(next, tpl.defaultKey, instance);
    } else if (tpl.defaultKey) {
      // Map-like parent (modules / channels) — append under defaultKey.
      const parent = (getAtPath(next, tpl.parentPath) as Record<string, unknown> | undefined) ?? {};
      let key = tpl.defaultKey;
      let idx = 1;
      while (key in parent) {
        idx += 1;
        key = `${tpl.defaultKey}_${idx}`;
      }
      next = setAtPath(next, `${tpl.parentPath}.${key}`, instance);
    } else {
      // Array parent — append.
      const existing = (getAtPath(next, tpl.parentPath) as unknown[] | undefined) ?? [];
      next = setAtPath(next, tpl.parentPath, [...existing, instance]);
    }
    setEditedYaml(dumpYaml(next));
  }, [parsedDoc]);
  const [paletteCollapsed, setPaletteCollapsed] = useState(false);
  const [outlineCollapsed, setOutlineCollapsed] = useState(false);
  const [contextMenu, setContextMenu] = useState<{
    x: number; y: number; nodeId: string; yamlPath: string | null;
  } | null>(null);
  const [tutorialOpen, setTutorialOpen] = useState(false);
  const [presetsOpen, setPresetsOpen] = useState(false);
  const [testPanelOpen, setTestPanelOpen] = useState(false);
  const [yamlPaneOpen, setYamlPaneOpen] = useState(false);
  const templatesByKind = useMemo(() => {
    const m = new Map<string, NodeTemplate>();
    for (const t of TEMPLATES) m.set(t.kind, t);
    return m;
  }, []);
  const onDownloadYaml = useCallback(() => {
    if (!yamlContent) return;
    const blob = new Blob([yamlContent], { type: "text/yaml" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `${appName.replace(/\s+/g, "-").toLowerCase()}.yaml`;
    a.click();
    URL.revokeObjectURL(url);
  }, [yamlContent, appName]);
  // Pre-save schema validation — runs on every render so the user
  // sees blocking errors live (and the Deploy button can refuse).
  const schemaIssues = useMemo(() => validateSchema(parsedDoc), [parsedDoc]);
  const blockingSchema = useMemo(() => blockingIssues(schemaIssues), [schemaIssues]);

  const [deployStatus, setDeployStatus] = useState<{ kind: "idle" | "saving" | "ok" | "error"; msg?: string }>({ kind: "idle" });
  const onDeploy = useCallback(async () => {
    if (!yamlContent) return;
    const appId = parsedDoc?.app?.app_id;
    if (!appId) {
      setDeployStatus({ kind: "error", msg: "No app.app_id — cannot deploy without an id." });
      return;
    }
    if (blockingSchema.length > 0) {
      setDeployStatus({
        kind: "error",
        msg: `${blockingSchema.length} schema error${blockingSchema.length > 1 ? "s" : ""} block deploy. Fix them in the Inspector first.`,
      });
      return;
    }
    setDeployStatus({ kind: "saving" });
    try {
      const r = await fetch(`/api/apps/${appId}/yaml`, {
        method: "PUT",
        headers: { "Content-Type": "text/yaml" },
        body: yamlContent,
      });
      if (!r.ok) {
        const text = await r.text().catch(() => "");
        setDeployStatus({ kind: "error", msg: `HTTP ${r.status} ${text.slice(0, 80)}` });
        return;
      }
      setDeployStatus({ kind: "ok", msg: `Deployed ${appId}.` });
      setTimeout(() => setDeployStatus({ kind: "idle" }), 3000);
    } catch (e) {
      setDeployStatus({ kind: "error", msg: `Network: ${String(e).slice(0, 80)} — falling back to download.` });
      onDownloadYaml();
    }
  }, [yamlContent, parsedDoc, blockingSchema, onDownloadYaml]);

  // Track canvas width so lane wrapping reflects the viewport size.
  const flowRef = useRef<HTMLDivElement>(null);
  const [canvasWidth, setCanvasWidth] = useState(1500);
  useEffect(() => {
    const el = flowRef.current;
    if (!el) return;
    const ro = new ResizeObserver((entries) => {
      const w = entries[0]?.contentRect.width ?? 1500;
      setCanvasWidth(Math.max(800, Math.floor(w)));
    });
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  const layouted = useMemo(() => {
    if (!rawNodes.length) {
      return {
        nodes: [] as RFNode<EnrichedNodeData>[],
        edges: [] as RFEdge[],
      };
    }
    if (layoutMode === "lanes") {
      const r = laneLayout(rawNodes, rawEdges, {
        width: canvasWidth,
        expandPalette: paletteExpanded,
        density: effectiveDensity,
      });
      return {
        nodes: r.nodes as RFNode<EnrichedNodeData>[],
        edges: r.edges,
      };
    }
    const { nodes: ln, edges: le } = autoLayout(rawNodes, rawEdges, { direction: layoutDir });
    return { nodes: ln, edges: le };
  }, [rawNodes, rawEdges, layoutMode, layoutDir, canvasWidth, paletteExpanded, effectiveDensity]);

  // Post-process layouted nodes: attach validation severity, beginner
  // labels, and dim/un-dim per the active view-mode + story step.
  const decoratedLayouted = useMemo(() => {
    let ns = layouted.nodes.map((n) => {
      const data = n.data;
      const validation = validationByNode.get(n.id);
      const beginnerLabel = beginnerMode ? beginnerLabelFor(data.kind as string) : undefined;
      return { ...n, data: { ...data, validation, beginnerLabel, density: effectiveDensity } as EnrichedNodeData };
    });
    ns = dimNodesForView(ns, viewMode, validationIssues);
    // Story mode further focuses the highlighted set on top of dim.
    if (viewMode === "runtime" && storyActive.nodeIds.size > 0) {
      ns = ns.map((n) => ({
        ...n,
        data: { ...n.data, dimmed: !storyActive.nodeIds.has(n.id) },
      }));
    }
    return { nodes: ns, edges: layouted.edges };
  }, [layouted, validationByNode, validationIssues, viewMode, beginnerMode, storyActive, effectiveDensity]);

  const [nodes, setNodes, onNodesChange] = useNodesState<EnrichedNodeData>(decoratedLayouted.nodes);
  const [edges, setEdges, onEdgesChange] = useEdgesState(decoratedLayouted.edges);

  useEffect(() => {
    setNodes(decoratedLayouted.nodes);
    setEdges(decoratedLayouted.edges);
    // Fit a moment after layout updates
    setTimeout(() => rf.fitView({ padding: 0.2, duration: 300 }), 50);
  }, [decoratedLayouted, setNodes, setEdges, rf]);

  // Story mode: animate the edges that belong to the current step.
  useEffect(() => {
    const active = storyActive.edgePairs;
    setEdges((curr) =>
      curr.map((e) => {
        const isActive = active.some(([s, t]) => e.source === s && e.target === t);
        return { ...e, animated: isActive || e.animated === true };
      }),
    );
  }, [storyActive, setEdges]);

  // ── Live status overlay ─────────────────────────────────────
  const live = useLiveStatus();
  useEffect(() => {
    setNodes((curr) =>
      curr.map((n) => {
        const status = live.statuses.get(n.id) ?? "idle";
        if (n.data.status === status) return n;
        return { ...n, data: { ...n.data, status } };
      }),
    );
    setEdges((curr) =>
      curr.map((e) => {
        const isActive = live.activeEdges.has(e.id);
        if (e.animated === isActive) return e;
        return {
          ...e,
          animated: isActive,
          style: {
            ...(e.style ?? {}),
            stroke: isActive
              ? "rgb(var(--status-running))"
              : (e.style as { stroke?: string } | undefined)?.stroke,
            strokeWidth: isActive
              ? 2
              : (e.style as { strokeWidth?: number } | undefined)?.strokeWidth ?? 1.5,
          },
        };
      }),
    );
  }, [live, setNodes, setEdges]);

  // ── Selection (Inspector) ────────────────────────────────────
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const selectedData = useMemo(
    () => (selectedId ? nodes.find((n) => n.id === selectedId)?.data ?? null : null),
    [selectedId, nodes],
  );

  const onNodeClick = useCallback((_: React.MouseEvent, node: RFNode<EnrichedNodeData>) => {
    setSelectedId(node.id);
  }, []);

  const onPaneClick = useCallback(() => setSelectedId(null), []);

  // ── Deps panel : compute uses / usedBy from the edge list ────
  const deps = useMemo(() => {
    if (!selectedId) return undefined;
    const selectedNode = nodes.find((n) => n.id === selectedId);
    const labelOf = (id: string) =>
      nodes.find((n) => n.id === id)?.data.label ?? id;

    // For agents with explicit module restrictions, filter the "Uses"
    // list to only the modules they actually have access to. The graph
    // edges may suggest they can reach every module (because the parser
    // wires module->app generic edges); the Uses panel must reflect the
    // YAML truth, not the visualisation noise.
    const restricted = (selectedNode?.data as { restrictedModules?: Array<{ module: string; actions: string[] }> } | undefined)
      ?.restrictedModules;
    const allowedModuleIds: Set<string> | null = restricted && restricted.length > 0
      ? new Set(restricted.map((m) => `module-${m.module}`))
      : null;

    let uses = edges
      .filter((e) => e.source === selectedId)
      .map((e) => ({
        id: e.target,
        label: labelOf(e.target),
        via: typeof e.label === "string" ? e.label : undefined,
      }));
    if (allowedModuleIds) {
      uses = uses.filter((u) =>
        !u.id.startsWith("module-") || allowedModuleIds.has(u.id),
      );
      // Also surface modules from `restrictedModules` even if no edge exists
      // for them (normal case: the parser doesn't always emit per-agent edges).
      const seen = new Set(uses.map((u) => u.id));
      for (const m of restricted!) {
        const id = `module-${m.module}`;
        if (!seen.has(id)) {
          uses.push({
            id,
            label: labelOf(id),
            via: m.actions.length ? `[${m.actions.join(", ")}]` : "all actions",
          });
        }
      }
    }
    const usedBy = edges
      .filter((e) => e.target === selectedId)
      .map((e) => ({
        id: e.source,
        label: labelOf(e.source),
        via: typeof e.label === "string" ? e.label : undefined,
      }));
    return { uses, usedBy };
  }, [selectedId, edges, nodes]);

  // ── Search (highlight matching nodes) ────────────────────────
  const [searchQuery, setSearchQuery] = useState("");
  const searchHits = useMemo(() => {
    if (!searchQuery.trim()) return 0;
    const q = searchQuery.toLowerCase();
    return nodes.filter((n) => {
      const d = n.data;
      return (
        d.label.toLowerCase().includes(q) ||
        (d.subtitle ?? "").toLowerCase().includes(q) ||
        d.kind.includes(q)
      );
    }).length;
  }, [nodes, searchQuery]);

  // Apply highlight as a CSS class via node.className
  useEffect(() => {
    const q = searchQuery.trim().toLowerCase();
    setNodes((curr) =>
      curr.map((n) => {
        if (!q) return { ...n, className: undefined };
        const d = n.data;
        const matches =
          d.label.toLowerCase().includes(q) ||
          (d.subtitle ?? "").toLowerCase().includes(q) ||
          d.kind.includes(q);
        return { ...n, className: matches ? "search-hit" : "search-dim" };
      }),
    );
  }, [searchQuery, setNodes]);

  // ── Toolbar actions ──────────────────────────────────────────
  const onFit = useCallback(() => rf.fitView({ padding: 0.2, duration: 400 }), [rf]);
  const onReset = useCallback(() => {
    if (layoutMode === "lanes") {
      const r = laneLayout(rawNodes, rawEdges, {
        width: canvasWidth,
        expandPalette: paletteExpanded,
      });
      setNodes(r.nodes as RFNode<EnrichedNodeData>[]);
    } else {
      const { nodes: ln } = autoLayout(rawNodes, rawEdges, { direction: layoutDir });
      setNodes(ln);
    }
    setTimeout(onFit, 50);
  }, [rawNodes, rawEdges, layoutMode, layoutDir, canvasWidth, paletteExpanded, setNodes, onFit]);

  const onExport = useCallback(async () => {
    const el = flowRef.current?.querySelector(".react-flow__viewport") as HTMLElement | null;
    if (!el) return;
    try {
      const url = await toPng(el, {
        backgroundColor: theme === "dark" ? "#070c16" : "#f7f8fa",
        pixelRatio: 2,
      });
      const a = document.createElement("a");
      a.href = url;
      a.download = `${appName.replace(/\s+/g, "-")}.png`;
      a.click();
    } catch (e) {
      console.error("export failed", e);
    }
  }, [theme, appName]);

  // ── Keyboard shortcuts (cmd+0 fit, cmd+l layout, ? schema, cmd+Z undo, cmd+K search) ──
  const [schemaOpen, setSchemaOpen] = useState(false);
  const [searchPaletteOpen, setSearchPaletteOpen] = useState(false);
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const tag = (e.target as HTMLElement | null)?.tagName?.toLowerCase();
      const isInput = tag === "input" || tag === "textarea" || tag === "select";
      if ((e.metaKey || e.ctrlKey) && e.key === "0") {
        e.preventDefault();
        onFit();
      }
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "l" && !isInput) {
        e.preventDefault();
        onReset();
      }
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "z" && !e.shiftKey && !isInput) {
        e.preventDefault();
        undoStack.undo();
      }
      if ((e.metaKey || e.ctrlKey) && (e.key.toLowerCase() === "y" || (e.shiftKey && e.key.toLowerCase() === "z")) && !isInput) {
        e.preventDefault();
        undoStack.redo();
      }
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
        e.preventDefault();
        setSearchPaletteOpen(true);
      }
      if (e.key === "?" && !isInput) {
        e.preventDefault();
        setSchemaOpen((v) => !v);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onFit, onReset, undoStack]);

  // ── Render ───────────────────────────────────────────────────
  const empty = error && !nodes.length;

  return (
    <div className="flex flex-col h-screen overflow-hidden bg-surface-0 text-ink">
      <Toolbar
        appName={appName}
        theme={theme}
        onToggleTheme={toggle}
        layoutDir={layoutDir}
        onLayoutDir={setLayoutDir}
        layoutMode={layoutMode}
        onLayoutMode={setLayoutMode}
        viewMode={viewMode}
        onViewMode={(v) => {
          setViewMode(v);
          if (v !== "runtime") {
            setStoryOpen(false);
            setStoryPlaying(false);
            setStoryActive({ nodeIds: new Set(), edgePairs: [] });
          }
        }}
        beginnerMode={beginnerMode}
        onBeginnerMode={setBeginnerMode}
        density={effectiveDensity}
        onDensity={(d) => { setDensity(d); setDensityUserSet(true); }}
        onPlayStory={() => {
          setViewMode("runtime");
          setStoryOpen(true);
          setStoryPlaying(true);
        }}
        onFit={onFit}
        onResetLayout={onReset}
        onExport={onExport}
        searchQuery={searchQuery}
        onSearch={setSearchQuery}
        searchHits={searchHits}
        rightSlot={
          <div className="flex items-center gap-2">
            <button
              onClick={() => setPresetsOpen(true)}
              className="inline-flex items-center gap-1.5 h-8 px-2.5 rounded-lg text-xs text-ink-muted hover:text-ink hover:bg-surface-2"
              title="Load a starter app (chatbot / coding / research / multi-agent)"
            >
              ✨ Presets
            </button>
            <button
              onClick={() => setTutorialOpen(true)}
              className="inline-flex items-center gap-1.5 h-8 px-2.5 rounded-lg text-xs text-accent hover:bg-accent/15"
              title="Build a chatbot in 3 minutes"
            >
              📘 Tutorial
            </button>
            <button
              onClick={() => undoStack.undo()}
              disabled={!undoStack.canUndo}
              className={clsx(
                "h-8 w-8 inline-flex items-center justify-center rounded-lg text-xs",
                undoStack.canUndo ? "text-ink-muted hover:text-ink hover:bg-surface-2" : "text-ink-dim/40 cursor-not-allowed",
              )}
              title="Undo (⌘Z)"
            >
              ↶
            </button>
            <button
              onClick={() => undoStack.redo()}
              disabled={!undoStack.canRedo}
              className={clsx(
                "h-8 w-8 inline-flex items-center justify-center rounded-lg text-xs",
                undoStack.canRedo ? "text-ink-muted hover:text-ink hover:bg-surface-2" : "text-ink-dim/40 cursor-not-allowed",
              )}
              title="Redo (⌘⇧Z)"
            >
              ↷
            </button>
            {editedYaml && (
              <button
                onClick={onDeploy}
                disabled={deployStatus.kind === "saving"}
                className={clsx(
                  "inline-flex items-center gap-1.5 h-8 px-2.5 rounded-lg text-xs font-semibold",
                  deployStatus.kind === "ok" ? "bg-status-ok/15 text-status-ok"
                  : deployStatus.kind === "error" ? "bg-status-error/15 text-status-error"
                  : deployStatus.kind === "saving" ? "bg-surface-3 text-ink-dim"
                  : "bg-accent text-surface-0 hover:bg-accent/90",
                )}
                title={deployStatus.msg ?? "Save modified YAML to the daemon"}
              >
                {deployStatus.kind === "saving" ? "Saving…"
                  : deployStatus.kind === "ok" ? "✓ Deployed"
                  : deployStatus.kind === "error" ? "✗ Retry deploy"
                  : "💾 Deploy"}
              </button>
            )}
            <button
              onClick={() => setYamlPaneOpen((v) => !v)}
              className={clsx(
                "inline-flex items-center gap-1.5 h-8 px-2.5 rounded-lg text-xs",
                yamlPaneOpen ? "bg-accent/15 text-accent" : "text-ink-muted hover:text-ink hover:bg-surface-2",
              )}
              title="Toggle live YAML preview"
            >
              {} YAML
            </button>
            <button
              onClick={() => setTestPanelOpen(true)}
              className="inline-flex items-center gap-1.5 h-8 px-2.5 rounded-lg text-xs text-status-ok hover:bg-status-ok/15"
              title="Send a test prompt against this app"
            >
              ▶ Test
            </button>
            <EdgeLegend />
            <FilesMenu />
            <CompileStatus />
            <span className="hidden md:inline text-[10px] font-mono text-ink-dim px-1.5 py-0.5 rounded bg-surface-2 border border-border-subtle">
              {session.sessionId.slice(0, 8)}
            </span>
            <button
              onClick={() => setSchemaOpen(true)}
              className="hidden md:inline-flex items-center gap-1 h-8 px-2.5 rounded-lg text-xs text-ink-muted hover:text-ink hover:bg-surface-2"
              title="Schema reference (?)"
            >
              <span className="font-mono">?</span>
              <span>schema</span>
            </button>
            <WorkspaceMenu />
            <ConnectionBadge />
          </div>
        }
      />

      {/* Body: palette + canvas + inspector */}
      <div className="flex flex-1 min-h-0">
        {restoredFromAutosave && (
          <div className="absolute top-3 left-1/2 -translate-x-1/2 z-30 bg-status-warn/15 border border-status-warn/40 text-status-warn text-[11px] rounded-lg px-3 py-1.5 flex items-center gap-2 backdrop-blur-md pointer-events-auto">
            <span>↻ Restored unsaved edits from {restoredFromAutosave}.</span>
            <button
              onClick={() => { undoStack.reset(null); setRestoredFromAutosave(null); }}
              className="text-status-warn hover:text-ink underline text-[10px]"
            >
              discard
            </button>
            <button
              onClick={() => setRestoredFromAutosave(null)}
              className="text-status-warn hover:text-ink"
              title="Dismiss"
            >
              ×
            </button>
          </div>
        )}
        {roundTripStatus === "comments-lost" && (
          <div
            className="absolute top-3 right-3 z-30 bg-status-warn/15 border border-status-warn/40 text-status-warn text-[10px] rounded-lg px-2.5 py-1 flex items-center gap-1.5 backdrop-blur-md pointer-events-auto"
            title="Editing via Inspector dumps the YAML through js-yaml which doesn't preserve # comments. Switch to YAML pane → Edit raw to keep comments."
          >
            ⚠ Comments dropped on save
          </div>
        )}
        {roundTripStatus === "semantic-diff" && (
          <div
            className="absolute top-3 right-3 z-30 bg-status-error/15 border border-status-error/40 text-status-error text-[10px] rounded-lg px-2.5 py-1 flex items-center gap-1.5 backdrop-blur-md pointer-events-auto"
            title="The dumped YAML doesn't match the source structurally — this indicates a builder bug. Use Reset to revert."
          >
            ⛔ Structural diff detected
          </div>
        )}
        <PalettePanel
          collapsed={paletteCollapsed}
          onToggle={() => setPaletteCollapsed((c) => !c)}
          onAdd={onAddTemplate}
        />
        <OutlineTree
          nodes={nodes}
          selectedId={selectedId}
          onSelect={(id) => {
            setSelectedId(id);
            const n = nodes.find((x) => x.id === id);
            if (n) rf.setCenter(n.position.x + 100, n.position.y + 50, { duration: 400, zoom: 1.2 });
          }}
          collapsed={outlineCollapsed}
          onToggle={() => setOutlineCollapsed((c) => !c)}
        />
        <div
          ref={flowRef}
          className="relative flex-1 min-w-0"
          onDragOver={(e) => {
            if (e.dataTransfer.types.includes("application/x-digitorn-template")) {
              e.preventDefault();
              e.dataTransfer.dropEffect = "copy";
            }
          }}
          onDrop={(e) => {
            const kind = e.dataTransfer.getData("application/x-digitorn-template");
            if (!kind) return;
            const tpl = templatesByKind.get(kind);
            if (tpl) onAddTemplate(tpl);
          }}
        >
          {empty && <EmptyState />}
          {/* Empty-state coach: real graph but no meaningful nodes (only
              app-root/user/input skeletons exist). Show ghost starter
              cards in lifecycle order. */}
          {!empty && rawNodes.filter((n) => !["app-root", "user", "input"].includes(n.id)).length === 0 && viewMode !== "sequence" && (
            <EmptyCanvas
              templates={TEMPLATES}
              onAdd={(kind) => {
                const t = templatesByKind.get(kind);
                if (t) onAddTemplate(t);
              }}
            />
          )}
          {viewMode === "sequence" && (
            <SequenceDiagram
              diagram={sequenceDiagram}
              selectedNodeId={selectedId}
              onSelectNode={(id) => setSelectedId(id)}
              onClose={() => setViewMode("architecture")}
              scenario={sequenceScenario}
              onScenarioChange={setSequenceScenario}
            />
          )}
          {viewMode !== "sequence" && storyOpen && (
            <StoryRunner
              steps={storySteps}
              playing={storyPlaying}
              onPlay={() => setStoryPlaying(true)}
              onPause={() => setStoryPlaying(false)}
              onClose={() => {
                setStoryOpen(false);
                setStoryPlaying(false);
                setStoryActive({ nodeIds: new Set(), edgePairs: [] });
                setViewMode("architecture");
              }}
              onStep={setStoryActive}
            />
          )}
          {viewMode !== "sequence" && (
          <ReactFlow
            nodes={nodes}
            edges={edges}
            onNodesChange={onNodesChange}
            onEdgesChange={onEdgesChange}
            onNodeClick={onNodeClick}
            onPaneClick={onPaneClick}
            onConnect={onConnectEdge}
            onNodeContextMenu={(e, node) => {
              e.preventDefault();
              setContextMenu({
                x: e.clientX,
                y: e.clientY,
                nodeId: node.id,
                yamlPath: pathForNodeId(node.id, parsedDoc),
              });
            }}
            isValidConnection={(c) => {
              const srcK = (rawNodes.find((n) => n.id === c.source)?.data?.kind as string | undefined) ?? null;
              const tgtK = (rawNodes.find((n) => n.id === c.target)?.data?.kind as string | undefined) ?? null;
              return isAllowedConnect(srcK, tgtK);
            }}
            nodeTypes={nodeTypes}
            fitView
            fitViewOptions={{ padding: 0.2 }}
            minZoom={0.05}
            maxZoom={2.5}
            // VIEWPORT VIRTUALIZATION: only render nodes/edges within
            // the visible area. Critical for 1000+ node apps — without
            // this ReactFlow paints every node every frame, killing pan
            // performance. Trade-off: tiny offscreen pop-in on fast pan.
            onlyRenderVisibleElements={nodes.length > 50}
            // Selection mode "Partial" so a marquee select catches nodes
            // even if only their corner is in the box (faster bulk ops).
            selectionMode={"partial" as never}
            proOptions={{ hideAttribution: true }}
            defaultEdgeOptions={{
              type: "smoothstep",
              animated: false,
            }}
            className="!bg-transparent"
          >
            <Background
              variant={BackgroundVariant.Dots}
              color={theme === "dark" ? "rgb(31, 45, 67)" : "rgb(226, 232, 240)"}
              gap={22}
              size={1.5}
            />
            <Controls
              showInteractive={false}
              position="bottom-right"
            />
            <MiniMap
              position="bottom-left"
              pannable
              zoomable
              nodeColor={(n: RFNode<EnrichedNodeData>) => {
                const c = n.data?.color;
                return c ?? "rgb(100, 116, 139)";
              }}
              maskColor={theme === "dark" ? "rgba(7, 12, 22, 0.7)" : "rgba(247, 248, 250, 0.7)"}
            />
          </ReactFlow>
          )}
          {viewMode !== "sequence" && live.lastToolCall && (
            <ToolCallBubble
              agentId={live.lastToolCall.agentId}
              toolName={live.lastToolCall.toolName}
              args={live.lastToolCall.args}
            />
          )}
          {/* ReadyDashboard moved into the toolbar `<FilesMenu />` so the
              build-status chips (YAML / Deploy / Tests) don't take canvas
              space. The status is still visible at a glance via the dot
              on the Files button. */}
        </div>

        <YamlPane
          open={yamlPaneOpen}
          yaml={yamlContent ?? ""}
          sourceYaml={sourceYaml}
          onChange={(y) => setEditedYaml(y)}
          onClose={() => setYamlPaneOpen(false)}
        />
        {selectedData && (
          <Inspector
            data={selectedData}
            deps={deps}
            doc={parsedDoc}
            validationIssues={
              selectedId
                ? validationIssues
                    .filter((i) => i.nodeId === selectedId)
                    .map((i) => ({ severity: i.severity, message: i.message, hint: i.hint, fix: i.fix }))
                : []
            }
            yamlPath={selectedId ? pathForNodeId(selectedId, parsedDoc) : null}
            edited={editedYaml != null}
            onEditField={onYamlEdit}
            onDeleteField={onYamlDelete}
            onResetEdits={onResetYaml}
            onDownloadYaml={onDownloadYaml}
            onSelectNode={(id) => setSelectedId(id)}
            onClose={() => setSelectedId(null)}
          />
        )}
      </div>

      {/* Bottom strip — auto-tests panel only (PhaseStepper moved to top) */}
      <div className="border-t border-border-subtle bg-surface-1 flex-shrink-0 max-h-[40vh] overflow-hidden p-3">
        <AutoTestPanel />
      </div>

      <SchemaReferencePanel open={schemaOpen} onClose={() => setSchemaOpen(false)} />
      <TutorialOverlay open={tutorialOpen} onClose={() => setTutorialOpen(false)} />
      {contextMenu && (
        <NodeContextMenu
          x={contextMenu.x}
          y={contextMenu.y}
          nodeId={contextMenu.nodeId}
          yamlPath={contextMenu.yamlPath}
          canDelete={!!contextMenu.yamlPath && !["app", "capabilities", "behavior", "workspace", "preview", "widgets", "middleware"].includes(contextMenu.yamlPath)}
          canDuplicate={!!contextMenu.yamlPath && /\.\d+$/.test(contextMenu.yamlPath)}
          onInspect={() => setSelectedId(contextMenu.nodeId)}
          onCenter={() => {
            const n = nodes.find((x) => x.id === contextMenu.nodeId);
            if (n) rf.setCenter(n.position.x + 100, n.position.y + 50, { duration: 400, zoom: 1.4 });
          }}
          onDuplicate={() => {
            if (!contextMenu.yamlPath || !parsedDoc) return;
            const cur = getAtPath(parsedDoc, contextMenu.yamlPath);
            if (!cur) return;
            // Insert a deep clone right after, with a renamed id if it has one.
            const parts = contextMenu.yamlPath.split(".");
            const lastIdx = Number.parseInt(parts[parts.length - 1], 10);
            if (Number.isNaN(lastIdx)) return;
            const parentPath = parts.slice(0, -1).join(".");
            const arr = (getAtPath(parsedDoc, parentPath) as unknown[] | undefined) ?? [];
            const cloned: unknown = structuredClone(cur);
            if (cloned && typeof cloned === "object" && "id" in (cloned as object)) {
              const oldId = (cloned as { id: string }).id;
              (cloned as { id: string }).id = `${oldId}_copy`;
            }
            const next = setAtPath(
              parsedDoc,
              parentPath,
              [...arr.slice(0, lastIdx + 1), cloned, ...arr.slice(lastIdx + 1)],
            );
            setEditedYaml(dumpYaml(next));
          }}
          onCopyPath={() => {
            if (contextMenu.yamlPath) {
              navigator.clipboard.writeText(contextMenu.yamlPath).catch(() => {});
            }
          }}
          onDelete={() => {
            if (!contextMenu.yamlPath) return;
            onYamlDelete(contextMenu.yamlPath);
            if (selectedId === contextMenu.nodeId) setSelectedId(null);
          }}
          onClose={() => setContextMenu(null)}
        />
      )}
      <SearchPalette
        open={searchPaletteOpen}
        nodes={nodes}
        onClose={() => setSearchPaletteOpen(false)}
        onSelect={(id) => {
          setSelectedId(id);
          // Center the viewport on the picked node so the user sees it
          const node = nodes.find((n) => n.id === id);
          if (node) {
            rf.setCenter(node.position.x + 100, node.position.y + 50, { duration: 400, zoom: 1.2 });
          }
        }}
      />
      <PresetGallery
        open={presetsOpen}
        onClose={() => setPresetsOpen(false)}
        onLoad={(p) => setEditedYaml(p.build())}
      />
      <TestPromptPanel
        open={testPanelOpen}
        onClose={() => setTestPanelOpen(false)}
        appName={appName}
        yamlContent={yamlContent}
        sessionId={session.sessionId}
      />

      <style>{`
        .react-flow__node.search-hit {
          z-index: 10;
        }
        .react-flow__node.search-hit > div {
          box-shadow: 0 0 0 2px rgb(var(--accent)), 0 0 24px rgb(var(--accent) / 0.5);
        }
        .react-flow__node.search-dim {
          opacity: 0.3;
          transition: opacity 0.2s;
        }
      `}</style>
    </div>
  );
}

export default function App() {
  return (
    <ReactFlowProvider>
      <CanvasInner />
    </ReactFlowProvider>
  );
}
