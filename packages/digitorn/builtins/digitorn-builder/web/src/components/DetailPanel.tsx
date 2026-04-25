import { useMemo } from "react";
import type { NodeData } from "../lib/yaml-to-graph";
import yaml from "js-yaml";

interface Props {
  data: NodeData | null;
  onClose: () => void;
}

export default function DetailPanel({ data, onClose }: Props) {
  if (!data) return null;

  return (
    <div style={styles.root}>
      <div style={styles.header}>
        <KindBadge kind={data.kind} color={data.color} />
        <span style={styles.title}>{data.label}</span>
        <div style={{ flex: 1 }} />
        <button onClick={onClose} style={styles.closeBtn} title="Close">
          x
        </button>
      </div>

      {data.subtitle && <div style={styles.subtitle}>{data.subtitle}</div>}

      {data.kind === "agent" && <AgentDetails raw={data.raw} />}
      {data.kind === "module" && <ModuleDetails raw={data.raw} actions={data.grantedActions} />}
      {data.kind === "trigger" && <TriggerDetails raw={data.raw} />}
      {data.kind === "channel" && <ChannelDetails raw={data.raw} />}
      {data.kind === "hook" && <HookDetails raw={data.raw} />}
      {data.kind === "input" && <InputOutputDetails raw={data.raw} direction="input" />}
      {data.kind === "output" && <InputOutputDetails raw={data.raw} direction="output" />}
      {data.kind === "app" && <RawSection title="Config" raw={data.raw} />}
      {data.kind === "variable" && <RawSection title="Value" raw={data.raw} />}
    </div>
  );
}

/* ------------------------------------------------------------------ */
/*  Sub-sections                                                       */
/* ------------------------------------------------------------------ */

function AgentDetails({ raw }: { raw: unknown }) {
  const agent = raw as Record<string, unknown> | undefined;
  if (!agent) return null;

  const brain = agent.brain as Record<string, unknown> | undefined;
  const prompt = agent.system_prompt as string | undefined;

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
      {brain && (
        <Section title="Brain">
          <YamlBlock data={brain} />
        </Section>
      )}
      {prompt && (
        <Section title="System prompt">
          <div style={styles.promptBox}>
            {prompt.length > 300 ? prompt.slice(0, 300) + "..." : prompt}
          </div>
        </Section>
      )}
      {agent.role != null && (
        <Section title="Role">
          <div style={styles.text}>{String(agent.role)}</div>
        </Section>
      )}
    </div>
  );
}

function ModuleDetails({ raw, actions }: { raw: unknown; actions?: string[] }) {
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
      {actions && actions.length > 0 && (
        <Section title="Granted actions">
          <div style={{ display: "flex", flexWrap: "wrap", gap: 4 }}>
            {actions.map((a) => (
              <span key={a} style={styles.actionTag}>
                {a}
              </span>
            ))}
          </div>
        </Section>
      )}
      {raw != null && (
        <Section title="Config">
          <YamlBlock data={raw} />
        </Section>
      )}
    </div>
  );
}

function TriggerDetails({ raw }: { raw: unknown }) {
  const trigger = raw as Record<string, unknown> | undefined;
  if (!trigger) return null;

  const schedule = trigger.schedule as string | undefined;
  const path = trigger.path as string | undefined;
  const adapter = trigger.adapter as string | undefined;
  const activation = trigger.activation as Record<string, unknown> | undefined;

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
      {schedule && (
        <Section title="Schedule">
          <div style={styles.text}>{schedule}</div>
        </Section>
      )}
      {path && (
        <Section title="Path">
          <div style={styles.text}>{path}</div>
        </Section>
      )}
      {adapter && (
        <Section title="Adapter">
          <div style={styles.text}>{adapter}</div>
        </Section>
      )}
      {activation && (
        <Section title="Activation">
          <YamlBlock data={activation} />
        </Section>
      )}
      <RawSection title="Full config" raw={trigger} />
    </div>
  );
}

function ChannelDetails({ raw }: { raw: unknown }) {
  const channel = raw as Record<string, unknown> | undefined;
  if (!channel) return null;

  const type = channel.type as string | undefined;
  const config = channel.config as Record<string, unknown> | undefined;
  const userResolver = channel.user_resolver as string | undefined;

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
      {type && (
        <Section title="Type">
          <div style={styles.text}>{type}</div>
        </Section>
      )}
      {userResolver && (
        <Section title="User resolver">
          <div style={styles.text}>{userResolver}</div>
        </Section>
      )}
      {config && (
        <Section title="Config">
          <YamlBlock data={config} />
        </Section>
      )}
    </div>
  );
}

function HookDetails({ raw }: { raw: unknown }) {
  const hook = raw as Record<string, unknown> | undefined;
  if (!hook) return null;

  const event = hook.event as string | undefined;
  const condition = hook.condition;
  const action = hook.action;
  const cooldown = hook.cooldown as number | undefined;

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
      {event && (
        <Section title="Event">
          <div style={styles.text}>{event}</div>
        </Section>
      )}
      {condition != null && (
        <Section title="Condition">
          {typeof condition === "string"
            ? <div style={styles.text}>{condition}</div>
            : <YamlBlock data={condition} />}
        </Section>
      )}
      {action != null && (
        <Section title="Action">
          {typeof action === "string"
            ? <div style={styles.text}>{action}</div>
            : <YamlBlock data={action} />}
        </Section>
      )}
      {cooldown != null && (
        <Section title="Cooldown">
          <div style={styles.text}>{cooldown}s</div>
        </Section>
      )}
    </div>
  );
}

function InputOutputDetails({ raw, direction }: { raw: unknown; direction: "input" | "output" }) {
  const data = raw as Record<string, unknown> | undefined;
  if (!data) return null;

  const type = data.type as string | undefined;

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
      {type && (
        <Section title="Type">
          <div style={styles.text}>{type}</div>
        </Section>
      )}
      <Section title={direction === "input" ? "Schema" : "Format"}>
        <YamlBlock data={data} />
      </Section>
    </div>
  );
}

function RawSection({ title, raw }: { title: string; raw: unknown }) {
  if (!raw) return null;
  return (
    <Section title={title}>
      <YamlBlock data={raw} />
    </Section>
  );
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div>
      <div style={styles.sectionTitle}>{title}</div>
      {children}
    </div>
  );
}

function YamlBlock({ data }: { data: unknown }) {
  const text = useMemo(() => {
    try {
      return yaml.dump(data, { indent: 2, lineWidth: 60, noRefs: true });
    } catch {
      return JSON.stringify(data, null, 2);
    }
  }, [data]);

  // Highlight {{...}} template references
  const parts = text.split(/(\{\{[^}]+\}\})/g);
  return (
    <pre style={styles.codeBlock}>
      {parts.map((part, i) =>
        part.startsWith("{{") ? (
          <span key={i} style={styles.templateRef} title={describeTemplate(part)}>
            {part}
          </span>
        ) : (
          <span key={i}>{part}</span>
        ),
      )}
    </pre>
  );
}

function describeTemplate(ref: string): string {
  const inner = ref.slice(2, -2);
  if (inner.startsWith("prompt.")) return `File: prompts/${inner.slice(7)}.md`;
  if (inner.startsWith("skill.")) return `File: skills/${inner.slice(6)}.md`;
  if (inner.startsWith("secret.")) return `Secret: ${inner.slice(7)} (encrypted)`;
  if (inner.startsWith("env.")) return `Environment variable: ${inner.slice(4)}`;
  if (inner.startsWith("include:")) return `Include YAML: ${inner.slice(8)}`;
  if (inner.startsWith("asset.")) return `Asset file: assets/${inner.slice(6)}`;
  return `Variable: ${inner}`;
}

function KindBadge({ kind, color }: { kind: string; color: string }) {
  return (
    <span
      style={{
        fontSize: 9,
        fontWeight: 700,
        textTransform: "uppercase",
        letterSpacing: 1,
        color,
        background: `${color}22`,
        padding: "2px 8px",
        borderRadius: 4,
      }}
    >
      {kind}
    </span>
  );
}

/* ------------------------------------------------------------------ */
/*  Styles                                                             */
/* ------------------------------------------------------------------ */

const styles: Record<string, React.CSSProperties> = {
  root: {
    width: 320,
    height: "100%",
    background: "#0d1525",
    borderLeft: "1px solid #1e293b",
    display: "flex",
    flexDirection: "column",
    overflow: "hidden",
  },
  header: {
    display: "flex",
    alignItems: "center",
    gap: 8,
    padding: "12px 14px",
    borderBottom: "1px solid #1e293b",
  },
  title: {
    fontSize: 14,
    fontWeight: 600,
    color: "#e2e8f0",
  },
  subtitle: {
    fontSize: 11,
    color: "#94a3b8",
    padding: "6px 14px",
    fontFamily: "monospace",
  },
  closeBtn: {
    background: "none",
    border: "1px solid #334155",
    borderRadius: 6,
    color: "#94a3b8",
    cursor: "pointer",
    width: 24,
    height: 24,
    display: "flex",
    alignItems: "center",
    justifyContent: "center",
    fontSize: 12,
    fontFamily: "monospace",
  },
  sectionTitle: {
    fontSize: 10,
    fontWeight: 700,
    textTransform: "uppercase",
    letterSpacing: 1,
    color: "#64748b",
    padding: "0 14px",
    marginBottom: 4,
  },
  codeBlock: {
    margin: "0 14px",
    padding: "8px 10px",
    background: "#0a0f1a",
    border: "1px solid #1e293b",
    borderRadius: 6,
    fontSize: 11,
    fontFamily: "'JetBrains Mono', 'Fira Code', monospace",
    color: "#cbd5e1",
    overflow: "auto",
    maxHeight: 300,
    whiteSpace: "pre-wrap",
    lineHeight: 1.5,
  },
  promptBox: {
    margin: "0 14px",
    padding: "8px 10px",
    background: "#0a0f1a",
    border: "1px solid #1e293b",
    borderRadius: 6,
    fontSize: 11,
    color: "#94a3b8",
    lineHeight: 1.5,
    maxHeight: 200,
    overflow: "auto",
    whiteSpace: "pre-wrap",
  },
  text: {
    padding: "0 14px",
    fontSize: 12,
    color: "#cbd5e1",
  },
  actionTag: {
    fontSize: 10,
    fontFamily: "monospace",
    color: "#10b981",
    background: "#10b98122",
    padding: "2px 8px",
    borderRadius: 4,
    border: "1px solid #10b98133",
  },
  templateRef: {
    color: "#f59e0b",
    background: "#f59e0b18",
    padding: "1px 4px",
    borderRadius: 3,
    cursor: "help",
    borderBottom: "1px dashed #f59e0b55",
  },
};
