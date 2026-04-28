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

import CustomNode from "./components/CustomNode";
import AgentGroupNode from "./components/AgentGroupNode";
import Inspector from "./components/Inspector";
import Toolbar, { type LayoutMode } from "./components/Toolbar";
import EmptyState from "./components/EmptyState";
import StoryRunner from "./components/StoryRunner";
import ToolCallBubble from "./components/ToolCallBubble";
import { groupSubAgents, AGENT_GROUP_NODE_TYPE } from "./lib/group-agents";
import { useLiveStatus } from "./lib/useLiveStatus";

// Existing widgets (kept as-is, just composed into the new layout)
import CompileStatus from "./components/CompileStatus";
import ConnectionBadge from "./components/ConnectionBadge";
import WorkspaceMenu from "./components/WorkspaceMenu";
import ReadyDashboard from "./components/ReadyDashboard";
import SchemaReferencePanel from "./components/SchemaReferencePanel";
import AutoTestPanel from "./components/AutoTestPanel";

const session = readSession();
const nodeTypes = {
  customNode: CustomNode,
  [AGENT_GROUP_NODE_TYPE]: AgentGroupNode,
};

function CanvasInner() {
  const { theme, toggle } = useTheme();
  const liveYaml = useFile("app.yaml");
  // In `_dev_` standalone mode (Vite preview without a real session) the
  // file API never resolves — fall back to the bundled fixture so the
  // builder team can iterate on the canvas without the full daemon stack.
  const yamlContent =
    liveYaml ?? (session.sessionId === "_dev_" ? (devFixtureYaml as string) : null);
  const rf = useReactFlow();

  // ── Parse + enrich + merge extra nodes + group sub-agents ────
  const { rawNodes, rawEdges, appName, error, parsedDoc } = useMemo(() => {
    const result = parseYamlToGraph(yamlContent ?? "");
    const name = result.parsed?.app?.name ?? result.parsed?.app?.app_id ?? "App";
    const enriched = enrichNodes(result.nodes);
    const { nodes: extraNodes, edges: extraEdges } = buildExtraNodes(result.parsed);
    const merged = [...enriched, ...(extraNodes as typeof enriched)];
    const mergedEdges = [...result.edges, ...extraEdges];
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
      });
      return {
        nodes: r.nodes as RFNode<EnrichedNodeData>[],
        edges: r.edges,
      };
    }
    const { nodes: ln, edges: le } = autoLayout(rawNodes, rawEdges, { direction: layoutDir });
    return { nodes: ln, edges: le };
  }, [rawNodes, rawEdges, layoutMode, layoutDir, canvasWidth, paletteExpanded]);

  // Post-process layouted nodes: attach validation severity, beginner
  // labels, and dim/un-dim per the active view-mode + story step.
  const decoratedLayouted = useMemo(() => {
    let ns = layouted.nodes.map((n) => {
      const data = n.data;
      const validation = validationByNode.get(n.id);
      const beginnerLabel = beginnerMode ? beginnerLabelFor(data.kind as string) : undefined;
      return { ...n, data: { ...data, validation, beginnerLabel } as EnrichedNodeData };
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
  }, [layouted, validationByNode, validationIssues, viewMode, beginnerMode, storyActive]);

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

  // ── Keyboard shortcuts (cmd+0 fit, cmd+l layout, ? schema) ──
  const [schemaOpen, setSchemaOpen] = useState(false);
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const tag = (e.target as HTMLElement | null)?.tagName?.toLowerCase();
      if (tag === "input" || tag === "textarea") return;
      if ((e.metaKey || e.ctrlKey) && e.key === "0") {
        e.preventDefault();
        onFit();
      }
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "l") {
        e.preventDefault();
        onReset();
      }
      if (e.key === "?") {
        e.preventDefault();
        setSchemaOpen((v) => !v);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onFit, onReset]);

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

      {/* Body: canvas + inspector */}
      <div className="flex flex-1 min-h-0">
        <div ref={flowRef} className="relative flex-1 min-w-0">
          {empty && <EmptyState />}
          {storyOpen && (
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
          <ReactFlow
            nodes={nodes}
            edges={edges}
            onNodesChange={onNodesChange}
            onEdgesChange={onEdgesChange}
            onNodeClick={onNodeClick}
            onPaneClick={onPaneClick}
            nodeTypes={nodeTypes}
            fitView
            fitViewOptions={{ padding: 0.2 }}
            minZoom={0.2}
            maxZoom={2.5}
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
          {live.lastToolCall && (
            <ToolCallBubble
              agentId={live.lastToolCall.agentId}
              toolName={live.lastToolCall.toolName}
              args={live.lastToolCall.args}
            />
          )}
          <ReadyDashboard />
        </div>

        {selectedData && (
          <Inspector
            data={selectedData}
            deps={deps}
            doc={parsedDoc}
            validationIssues={
              selectedId
                ? validationIssues
                    .filter((i) => i.nodeId === selectedId)
                    .map((i) => ({ severity: i.severity, message: i.message, hint: i.hint }))
                : []
            }
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
