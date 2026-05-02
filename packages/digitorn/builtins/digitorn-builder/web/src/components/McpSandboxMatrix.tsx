/**
 * Visual matrix editor for MCP server OS-level sandbox permissions.
 *
 * Source schema: ``MCPServerSandbox`` in
 * ``packages/digitorn/core/app/schema.py``. The compiler enforces
 * deny-by-default: if a server doesn't declare a permission it cannot
 * use that capability. The matrix lets the user toggle each fine-
 * grained permission with a checkbox AND grant the wildcard
 * ``process.*`` / ``net.*`` / ``fs.*`` shortcuts, which the daemon
 * expands at runtime.
 *
 * Companion editors below the grid:
 *
 *   - allowed_hosts: list of FQDNs the server may reach when ``net.http``
 *     or ``net.socket`` is granted (otherwise inert).
 *   - paths.read / paths.write: filesystem paths beyond the workspace
 *     the server may access. Supports {{workspace}} and ~.
 */
import {
  ShieldCheck, Cpu, Globe, FolderOpen, Plus, Trash2, AlertTriangle,
} from "lucide-react";
import clsx from "clsx";

interface SandboxRaw {
  permissions?: string[];
  paths?: { read?: string[]; write?: string[] };
  allowed_hosts?: string[];
}

interface Props {
  /** The MCP server's full raw object (transport, command, sandbox, ...). */
  raw: Record<string, unknown>;
  /** Absolute YAML path to the MCP server entry, e.g.
   *  ``modules.mcp.config.servers.github``. */
  basePath: string;
  onEdit: (absolutePath: string, value: unknown) => void;
}

interface PermSpec {
  key: string;
  label: string;
  hint: string;
  /** When true, this entry represents the wildcard `*` for its category
   *  - selecting it implies all siblings are also granted. */
  wildcard?: boolean;
}

interface PermCategory {
  key: "process" | "net" | "fs";
  label: string;
  icon: typeof Cpu;
  color: string;
  perms: PermSpec[];
}

const PERM_CATEGORIES: readonly PermCategory[] = [
  {
    key: "process",
    label: "Process",
    icon: Cpu,
    color: "#f59e0b",
    perms: [
      { key: "process.exec", label: "exec", hint: "Spawn subprocesses (required for stdio transport)." },
      { key: "process.spawn_daemon", label: "spawn_daemon", hint: "Start long-running daemons that outlive the request." },
      { key: "process.*", label: "* (all process)", hint: "Wildcard - grants all process permissions.", wildcard: true },
    ],
  },
  {
    key: "net",
    label: "Network",
    icon: Globe,
    color: "#0ea5e9",
    perms: [
      { key: "net.http", label: "http", hint: "Outbound HTTP/HTTPS (required for SSE/HTTP transport)." },
      { key: "net.socket", label: "socket", hint: "Raw TCP/UDP socket access." },
      { key: "net.listen", label: "listen", hint: "Bind and listen on a port." },
      { key: "net.*", label: "* (all net)", hint: "Wildcard - grants all network permissions.", wildcard: true },
    ],
  },
  {
    key: "fs",
    label: "Filesystem",
    icon: FolderOpen,
    color: "#10b981",
    perms: [
      { key: "fs.read", label: "read", hint: "Read files beyond the workspace." },
      { key: "fs.write", label: "write", hint: "Write files beyond the workspace." },
      { key: "fs.delete", label: "delete", hint: "Delete files beyond the workspace." },
      { key: "fs.*", label: "* (all fs)", hint: "Wildcard - grants all filesystem permissions.", wildcard: true },
    ],
  },
] as const;

export default function McpSandboxMatrix({ raw, basePath, onEdit }: Props) {
  const sandbox = (raw.sandbox as SandboxRaw | undefined) ?? {};
  const granted = new Set(sandbox.permissions ?? []);
  const sandboxPath = `${basePath}.sandbox`;

  const togglePerm = (key: string) => {
    const next = new Set(granted);
    if (next.has(key)) next.delete(key);
    else next.add(key);
    // If the user just enabled a wildcard, drop the more specific
    // entries (the wildcard supersedes them - keeps the YAML minimal).
    if (next.has(key) && key.endsWith(".*")) {
      const prefix = key.slice(0, -1);
      for (const p of [...next]) {
        if (p !== key && p.startsWith(prefix)) next.delete(p);
      }
    }
    onEdit(`${sandboxPath}.permissions`, [...next].sort());
  };

  const isImplicitlyGranted = (key: string): boolean => {
    if (granted.has(key)) return false; // explicit, not implicit
    const cat = key.split(".")[0];
    return granted.has(`${cat}.*`);
  };

  const transport = (raw.transport as string) ?? "stdio";
  const needsExec = transport === "stdio";
  const needsHttp = transport === "sse" || transport === "http";

  return (
    <div className="space-y-4">
      {/* Transport-driven hints surface the implicit requirements that
          the daemon enforces - prevents "I granted everything but it
          still won't connect" confusion. */}
      <div className="rounded-md bg-surface-2 border border-border-subtle p-2.5 text-[11px] space-y-1">
        <div className="flex items-center gap-1.5 text-ink-muted">
          <ShieldCheck className="w-3 h-3 text-accent" />
          <span className="font-medium">Transport: <span className="font-mono">{transport}</span></span>
        </div>
        {needsExec && !granted.has("process.exec") && !granted.has("process.*") && (
          <div className="flex items-start gap-1.5 text-status-warn">
            <AlertTriangle className="w-3 h-3 flex-shrink-0 mt-0.5" />
            <span>
              <span className="font-mono">stdio</span> transport requires
              <span className="font-mono"> process.exec</span>.
            </span>
          </div>
        )}
        {needsHttp && !granted.has("net.http") && !granted.has("net.*") && (
          <div className="flex items-start gap-1.5 text-status-warn">
            <AlertTriangle className="w-3 h-3 flex-shrink-0 mt-0.5" />
            <span>
              <span className="font-mono">{transport}</span> transport requires
              <span className="font-mono"> net.http</span>.
            </span>
          </div>
        )}
      </div>

      {/* Permission grid */}
      {PERM_CATEGORIES.map((cat) => {
        const Icon = cat.icon;
        const wildcardActive = granted.has(`${cat.key}.*`);
        return (
          <div key={cat.key} className="space-y-1.5">
            <div className="flex items-center gap-1.5">
              <Icon className="w-3.5 h-3.5" style={{ color: cat.color }} />
              <span className="text-[11px] font-medium text-ink">{cat.label}</span>
            </div>
            <div className="grid grid-cols-2 gap-1">
              {cat.perms.map((p) => {
                const explicit = granted.has(p.key);
                const implicit = isImplicitlyGranted(p.key);
                const active = explicit || implicit;
                const dimmed = wildcardActive && !p.wildcard;
                return (
                  <button
                    key={p.key}
                    onClick={() => togglePerm(p.key)}
                    title={p.hint}
                    disabled={dimmed}
                    className={clsx(
                      "flex items-center gap-1.5 px-2 py-1.5 rounded-md text-[11px] text-left transition-colors",
                      active && !dimmed && "bg-accent/15 text-accent ring-1 ring-accent/30",
                      !active && !dimmed && "bg-surface-2 text-ink-muted hover:text-ink hover:bg-surface-3",
                      dimmed && "bg-surface-2/60 text-ink-dim opacity-60 cursor-not-allowed",
                      p.wildcard && active && "ring-status-warn/40",
                    )}
                  >
                    <span
                      className={clsx(
                        "w-3 h-3 rounded border flex-shrink-0",
                        active ? "bg-accent border-accent" : "border-border-subtle",
                      )}
                    />
                    <span className="font-mono truncate">{p.label}</span>
                  </button>
                );
              })}
            </div>
            {wildcardActive && (
              <div className="text-[10px] text-status-warn italic px-1">
                Wildcard active - all <span className="font-mono">{cat.key}.*</span> permissions are implicitly granted.
              </div>
            )}
          </div>
        );
      })}

      {/* Allowed hosts */}
      <ListEditor
        label="Allowed hosts"
        hint="Outbound network destinations the server may reach. Only effective when net.http or net.socket is granted."
        items={sandbox.allowed_hosts ?? []}
        placeholder="api.github.com"
        basePath={`${sandboxPath}.allowed_hosts`}
        onEdit={onEdit}
      />

      {/* Paths read / write */}
      <ListEditor
        label="Paths · read"
        hint="Filesystem paths beyond the workspace the server may read. Supports {{workspace}} and ~ expansion."
        items={sandbox.paths?.read ?? []}
        placeholder="{{workspace}}/data"
        basePath={`${sandboxPath}.paths.read`}
        onEdit={onEdit}
        mono
      />
      <ListEditor
        label="Paths · write"
        hint="Filesystem paths beyond the workspace the server may write."
        items={sandbox.paths?.write ?? []}
        placeholder="~/.cache/mcp-server"
        basePath={`${sandboxPath}.paths.write`}
        onEdit={onEdit}
        mono
      />
    </div>
  );
}

/* ─── Reusable list-of-strings editor ───────────────────── */

function ListEditor({
  label, hint, items, placeholder, basePath, onEdit, mono,
}: {
  label: string;
  hint?: string;
  items: string[];
  placeholder: string;
  basePath: string;
  onEdit: (path: string, value: unknown) => void;
  mono?: boolean;
}) {
  return (
    <div className="space-y-1 pt-2 border-t border-border-subtle">
      <div className="text-[10px] uppercase tracking-wider text-ink-dim font-semibold">{label}</div>
      {hint && <div className="text-[10px] text-ink-dim italic">{hint}</div>}
      <div className="space-y-1 mt-1">
        {items.map((item, i) => (
          <div key={i} className="flex items-center gap-1">
            <input
              value={item}
              onChange={(e) => onEdit(`${basePath}.${i}`, e.target.value)}
              placeholder={placeholder}
              className={clsx(
                "flex-1 h-7 px-2 rounded-md bg-surface-2 border border-border-subtle text-[11px] text-ink placeholder:text-ink-dim focus:outline-none focus:border-accent",
                mono && "font-mono",
              )}
            />
            <button
              onClick={() => onEdit(basePath, items.filter((_, j) => j !== i))}
              className="p-1 text-status-error/60 hover:text-status-error rounded hover:bg-status-error/10"
              title="Remove"
            >
              <Trash2 className="w-3 h-3" />
            </button>
          </div>
        ))}
        <button
          onClick={() => onEdit(`${basePath}.${items.length}`, "")}
          className="inline-flex items-center gap-1 h-7 px-2 rounded-md text-[11px] bg-accent/15 text-accent hover:bg-accent/25"
        >
          <Plus className="w-3 h-3" /> Add
        </button>
      </div>
    </div>
  );
}
