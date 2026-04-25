import { useCallback, useMemo, useState } from "react";
import ReactFlow, {
  Background,
  BackgroundVariant,
  Controls,
  MiniMap,
  useEdgesState,
  useNodesState,
  type Node as RFNode,
} from "reactflow";
import "reactflow/dist/style.css";

import { useFile, readSession, type SessionInfo } from "@digitorn/preview-sdk";
import { parseYamlToGraph, type NodeData } from "./lib/yaml-to-graph";
import CustomNode from "./components/CustomNode";
import DetailPanel from "./components/DetailPanel";
import CompileStatus from "./components/CompileStatus";
import ConnectionBadge from "./components/ConnectionBadge";
import StateTimeline from "./components/StateTimeline";
import WorkspaceMenu from "./components/WorkspaceMenu";
import ReadyDashboard from "./components/ReadyDashboard";
import SchemaReferencePanel from "./components/SchemaReferencePanel";
import AutoTestPanel from "./components/AutoTestPanel";
import { useEffect } from "react";

const session: SessionInfo = readSession();

const nodeTypes = { customNode: CustomNode };

export default function App() {
  const yamlContent = useFile("app.yaml");

  const { graphNodes, graphEdges, appName, error } = useMemo(() => {
    const result = parseYamlToGraph(yamlContent ?? "");
    const name = result.parsed?.app?.name ?? result.parsed?.app?.app_id ?? "App";
    return {
      graphNodes: result.nodes,
      graphEdges: result.edges,
      appName: name,
      error: result.error,
    };
  }, [yamlContent]);

  const [nodes, setNodes, onNodesChange] = useNodesState(graphNodes);
  const [edges, setEdges, onEdgesChange] = useEdgesState(graphEdges);

  // sync graph when YAML changes
  useEffect(() => {
    setNodes(graphNodes);
    setEdges(graphEdges);
  }, [graphNodes, graphEdges, setNodes, setEdges]);

  // detail panel
  const [selectedData, setSelectedData] = useState<NodeData | null>(null);

  const onNodeClick = useCallback((_: React.MouseEvent, node: RFNode<NodeData>) => {
    setSelectedData(node.data);
  }, []);

  const onPaneClick = useCallback(() => {
    setSelectedData(null);
  }, []);

  // Global keyboard shortcuts — ? to toggle the schema reference.
  const [schemaOpen, setSchemaOpen] = useState(false);
  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      // Ignore typing inside inputs/textareas
      const tag = (e.target as HTMLElement | null)?.tagName?.toLowerCase();
      if (tag === "input" || tag === "textarea") return;
      if (e.key === "?") {
        e.preventDefault();
        setSchemaOpen((v) => !v);
      }
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, []);

  return (
    <div style={styles.root}>
      {/* header */}
      <header style={styles.header}>
        <span style={styles.title}>{appName}</span>
        <CompileStatus />
        <span style={styles.sessionTag}>{session.sessionId.slice(0, 8)}</span>
        <div style={{ flex: 1 }} />
        <button
          onClick={() => setSchemaOpen(true)}
          style={styles.schemaBtn}
          title="Schema reference — press ?"
        >
          <span style={{ fontFamily: "monospace" }}>?</span>
          <span>schema</span>
        </button>
        <WorkspaceMenu />
        <ConnectionBadge />
      </header>

      {/* body */}
      <div style={styles.body}>
        <div style={styles.canvasWrap}>
          {error && !graphNodes.length ? (
            <div style={styles.emptyState}>
              <div style={styles.emptyIcon}>?</div>
              <div style={styles.emptyTitle}>Waiting for app.yaml</div>
              <div style={styles.emptyText}>
                The agent will write the YAML config. The graph will appear automatically.
              </div>
            </div>
          ) : (
            <ReactFlow
              nodes={nodes}
              edges={edges}
              onNodesChange={onNodesChange}
              onEdgesChange={onEdgesChange}
              onNodeClick={onNodeClick}
              onPaneClick={onPaneClick}
              nodeTypes={nodeTypes}
              fitView
              fitViewOptions={{ padding: 0.3 }}
              minZoom={0.3}
              maxZoom={2}
              proOptions={{ hideAttribution: true }}
              defaultEdgeOptions={{
                type: "smoothstep",
                style: { strokeWidth: 1.5 },
              }}
            >
              <Background
                variant={BackgroundVariant.Dots}
                color="#1e293b"
                gap={20}
                size={1}
              />
              <Controls
                style={{ background: "#1e293b", borderColor: "#334155" }}
                showInteractive={false}
              />
              <MiniMap
                nodeColor={(n: RFNode<NodeData>) => n.data?.color ?? "#475569"}
                maskColor="#0f172acc"
                style={{ background: "#0b1120", border: "1px solid #1e293b" }}
              />
            </ReactFlow>
          )}

          {/* floating readiness dashboard */}
          <ReadyDashboard />
        </div>

        {/* detail panel */}
        {selectedData && (
          <DetailPanel data={selectedData} onClose={() => setSelectedData(null)} />
        )}
      </div>

      {/* bottom strip: timeline + auto-test panel side by side */}
      <div style={styles.bottomStrip}>
        <div style={styles.timelineCell}>
          <StateTimeline />
        </div>
        <div style={styles.testsCell}>
          <AutoTestPanel />
        </div>
      </div>

      {/* full-screen overlays */}
      <SchemaReferencePanel
        open={schemaOpen}
        onClose={() => setSchemaOpen(false)}
      />
    </div>
  );
}

/* ------------------------------------------------------------------ */
/*  Styles                                                             */
/* ------------------------------------------------------------------ */

const styles: Record<string, React.CSSProperties> = {
  root: {
    display: "flex",
    flexDirection: "column",
    height: "100vh",
    overflow: "hidden",
    background: "#0f172a",
    color: "#e2e8f0",
    fontFamily:
      "'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif",
  },
  header: {
    flex: "0 0 auto",
    padding: "10px 16px",
    borderBottom: "1px solid #1e293b",
    display: "flex",
    alignItems: "center",
    gap: 12,
    background: "#0b1120",
  },
  title: {
    fontWeight: 600,
    fontSize: 14,
    letterSpacing: 0.3,
  },
  sessionTag: {
    fontFamily: "monospace",
    fontSize: 10,
    color: "#64748b",
    padding: "2px 6px",
    background: "#1e293b",
    borderRadius: 4,
  },
  body: {
    flex: "1 1 auto",
    display: "flex",
    minHeight: 0,
    position: "relative",
  },
  canvasWrap: {
    flex: "1 1 0",
    minWidth: 0,
    minHeight: 0,
    position: "relative",
  },
  timeline: {
    flex: "0 0 auto",
    padding: "8px 16px",
    borderTop: "1px solid #1e293b",
    background: "#0b1120",
  },
  bottomStrip: {
    flex: "0 0 auto",
    display: "grid",
    gridTemplateColumns: "minmax(420px, 1fr) minmax(360px, 1fr)",
    gap: 16,
    padding: "10px 16px",
    borderTop: "1px solid #1e293b",
    background: "#0b1120",
  },
  timelineCell: {
    minWidth: 0,
  },
  testsCell: {
    minWidth: 0,
    paddingLeft: 16,
    borderLeft: "1px solid #1e293b",
  },
  schemaBtn: {
    display: "inline-flex",
    alignItems: "center",
    gap: 6,
    padding: "4px 10px",
    background: "#1e293b",
    border: "1px solid #334155",
    borderRadius: 4,
    fontSize: 10,
    fontWeight: 600,
    color: "#cbd5e1",
    letterSpacing: 0.3,
    cursor: "pointer",
  },
  emptyState: {
    display: "flex",
    flexDirection: "column",
    alignItems: "center",
    justifyContent: "center",
    height: "100%",
    gap: 8,
    color: "#475569",
  },
  emptyIcon: {
    width: 48,
    height: 48,
    borderRadius: "50%",
    background: "#1e293b",
    display: "flex",
    alignItems: "center",
    justifyContent: "center",
    fontSize: 24,
    color: "#334155",
    marginBottom: 8,
  },
  emptyTitle: {
    fontSize: 14,
    fontWeight: 600,
    color: "#64748b",
  },
  emptyText: {
    fontSize: 12,
    color: "#475569",
    maxWidth: 300,
    textAlign: "center",
    lineHeight: 1.5,
  },
};
