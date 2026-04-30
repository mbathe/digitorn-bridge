/**
 * Editable form for the selected node's YAML sub-tree.
 *
 * Renders inputs for primitive fields, recursive forms for nested
 * objects, and add/remove buttons for arrays. On every change, calls
 * `onEdit(absolutePath, value)` so the parent can dump the new YAML.
 *
 * `schemaHints` is a partial map from RELATIVE field path to a hint
 * describing the field's type / allowed values. The Inspector passes
 * the appropriate hint set per kind (agent, hook, brain, etc.)
 */
import { useState } from "react";
import { ChevronRight, ChevronDown, Plus, Trash2, AlertCircle } from "lucide-react";
import clsx from "clsx";

export interface SchemaHint {
  /** Field type — drives input choice */
  type?: "string" | "number" | "boolean" | "select" | "textarea";
  /** When type === "select", the allowed values */
  options?: string[];
  /** Plain-language explanation shown above the input. */
  hint?: string;
  /** Validator: returns null = valid, or a message. */
  validate?: (value: unknown) => string | null;
  /** Hide this field entirely (e.g. derived/internal). */
  hidden?: boolean;
}

interface Props {
  value: unknown;
  /** Absolute path inside the YAML, e.g. "agents.0". */
  basePath: string;
  /** Map RELATIVE field paths (e.g. "brain.temperature") to hints. */
  schemaHints?: Record<string, SchemaHint>;
  onEdit: (absolutePath: string, value: unknown) => void;
  onDelete: (absolutePath: string) => void;
  /** Internal: depth-first nesting level for indentation. */
  depth?: number;
}

export default function EditableConfig({ value, basePath, schemaHints = {}, onEdit, onDelete, depth = 0 }: Props) {
  return (
    <div className="space-y-1.5">
      <ObjectEditor
        value={value}
        basePath={basePath}
        relPath=""
        schemaHints={schemaHints}
        onEdit={onEdit}
        onDelete={onDelete}
        depth={depth}
      />
    </div>
  );
}

function ObjectEditor({
  value,
  basePath,
  relPath,
  schemaHints,
  onEdit,
  onDelete,
  depth,
}: {
  value: unknown;
  basePath: string;
  relPath: string;
  schemaHints: Record<string, SchemaHint>;
  onEdit: (absolutePath: string, value: unknown) => void;
  onDelete: (absolutePath: string) => void;
  depth: number;
}) {
  const [collapsed, setCollapsed] = useState(false);

  if (value == null) {
    return (
      <div className="text-[11px] text-ink-dim italic px-2">
        (not set — click + to add)
      </div>
    );
  }

  if (typeof value === "string" || typeof value === "number" || typeof value === "boolean") {
    return (
      <PrimitiveEditor
        value={value}
        path={basePath}
        hint={schemaHints[relPath]}
        onChange={(v) => onEdit(basePath, v)}
      />
    );
  }

  if (Array.isArray(value)) {
    return (
      <div className="space-y-1">
        {value.map((item, i) => {
          const itemPath = basePath ? `${basePath}.${i}` : String(i);
          const itemRel = relPath ? `${relPath}.${i}` : String(i);
          return (
            <div key={i} className="border-l-2 border-border-subtle pl-2 ml-1 my-1.5">
              <div className="flex items-center gap-2 mb-1">
                <span className="text-[10px] uppercase tracking-wider text-ink-dim font-mono">
                  [{i}]
                </span>
                <button
                  onClick={() => onDelete(itemPath)}
                  className="text-status-error/60 hover:text-status-error"
                  title="Remove"
                >
                  <Trash2 className="w-3 h-3" />
                </button>
              </div>
              <ObjectEditor
                value={item}
                basePath={itemPath}
                relPath={itemRel}
                schemaHints={schemaHints}
                onEdit={onEdit}
                onDelete={onDelete}
                depth={depth + 1}
              />
            </div>
          );
        })}
        <button
          onClick={() => onEdit(`${basePath}.${value.length}`, defaultForArray(value))}
          className="inline-flex items-center gap-1 text-[10px] text-accent/70 hover:text-accent ml-1"
        >
          <Plus className="w-3 h-3" />
          Add item
        </button>
      </div>
    );
  }

  // Object
  const keys = Object.keys(value as Record<string, unknown>);
  if (keys.length === 0) {
    return <div className="text-[11px] text-ink-dim italic">(empty)</div>;
  }

  return (
    <div className={clsx(depth > 0 && "border-l border-border-subtle/50 pl-2 ml-0.5")}>
      {depth > 0 && (
        <button
          onClick={() => setCollapsed((c) => !c)}
          className="inline-flex items-center gap-1 text-[10px] text-ink-dim hover:text-ink mb-1"
        >
          {collapsed ? <ChevronRight className="w-2.5 h-2.5" /> : <ChevronDown className="w-2.5 h-2.5" />}
          {collapsed ? "show" : "hide"} ({keys.length} fields)
        </button>
      )}
      {!collapsed && keys.map((k) => {
        const childPath = basePath ? `${basePath}.${k}` : k;
        const childRel = relPath ? `${relPath}.${k}` : k;
        const hint = schemaHints[childRel];
        if (hint?.hidden) return null;
        const child = (value as Record<string, unknown>)[k];
        const isPrimitive = child == null
          || typeof child === "string"
          || typeof child === "number"
          || typeof child === "boolean";
        return (
          <div key={k} className="mb-2.5">
            <div className="flex items-baseline gap-2 mb-1">
              <label className="text-[11px] text-ink font-mono font-medium">{k}</label>
              {hint?.hint && !isPrimitive && (
                <span className="text-[10px] text-ink-dim italic">{hint.hint}</span>
              )}
              <button
                onClick={() => onDelete(childPath)}
                className="text-status-error/40 hover:text-status-error"
                title="Remove"
              >
                <Trash2 className="w-2.5 h-2.5" />
              </button>
            </div>
            {hint?.hint && isPrimitive && (
              <div className="text-[10px] text-ink-dim italic mb-1">{hint.hint}</div>
            )}
            {isPrimitive ? (
              <PrimitiveEditor
                value={child as string | number | boolean | null}
                path={childPath}
                hint={hint}
                onChange={(v) => onEdit(childPath, v)}
              />
            ) : (
              <ObjectEditor
                value={child}
                basePath={childPath}
                relPath={childRel}
                schemaHints={schemaHints}
                onEdit={onEdit}
                onDelete={onDelete}
                depth={depth + 1}
              />
            )}
          </div>
        );
      })}
    </div>
  );
}

function PrimitiveEditor({
  value,
  path,
  hint,
  onChange,
}: {
  value: string | number | boolean | null;
  path: string;
  hint?: SchemaHint;
  onChange: (v: unknown) => void;
}) {
  const [local, setLocal] = useState<string>(value == null ? "" : String(value));
  const [error, setError] = useState<string | null>(null);
  const isBool = typeof value === "boolean" || hint?.type === "boolean";
  const isSelect = hint?.type === "select" && hint.options;
  const isTextarea = hint?.type === "textarea" || (typeof value === "string" && value.length > 80);
  const isNumber = typeof value === "number" || hint?.type === "number";

  const commit = (raw: string) => {
    let parsed: unknown = raw;
    if (isNumber) {
      const n = Number(raw);
      if (!Number.isNaN(n) && raw.trim() !== "") parsed = n;
    }
    if (hint?.validate) {
      const err = hint.validate(parsed);
      setError(err);
      if (err) return;
    } else {
      setError(null);
    }
    onChange(parsed);
  };

  if (isBool) {
    return (
      <div className="flex items-center gap-2">
        <input
          type="checkbox"
          checked={value === true}
          onChange={(e) => onChange(e.target.checked)}
          className="rounded"
          data-yaml-path={path}
        />
        <span className="text-[10px] text-ink-muted">{value ? "true" : "false"}</span>
      </div>
    );
  }

  if (isSelect && hint.options) {
    return (
      <div>
        <select
          value={local}
          onChange={(e) => { setLocal(e.target.value); commit(e.target.value); }}
          className="w-full h-8 px-2 rounded-md bg-surface-2 border border-border-subtle text-xs text-ink"
          data-yaml-path={path}
        >
          {hint.options.map((opt) => (
            <option key={opt} value={opt}>{opt}</option>
          ))}
        </select>
        {error && <ErrorMsg msg={error} />}
      </div>
    );
  }

  if (isTextarea) {
    return (
      <div>
        <textarea
          value={local}
          onChange={(e) => setLocal(e.target.value)}
          onBlur={() => commit(local)}
          rows={Math.min(6, Math.max(2, Math.ceil(local.length / 60)))}
          className="w-full px-2 py-1.5 rounded-md bg-surface-2 border border-border-subtle text-xs text-ink font-mono resize-y"
          data-yaml-path={path}
        />
        {error && <ErrorMsg msg={error} />}
      </div>
    );
  }

  return (
    <div>
      <input
        type={isNumber ? "number" : "text"}
        value={local}
        onChange={(e) => setLocal(e.target.value)}
        onBlur={() => commit(local)}
        onKeyDown={(e) => { if (e.key === "Enter") (e.target as HTMLInputElement).blur(); }}
        className={clsx(
          "w-full h-8 px-2 rounded-md bg-surface-2 border text-xs text-ink",
          error ? "border-status-error/60" : "border-border-subtle",
        )}
        data-yaml-path={path}
      />
      {error && <ErrorMsg msg={error} />}
    </div>
  );
}

function ErrorMsg({ msg }: { msg: string }) {
  return (
    <div className="mt-1 inline-flex items-center gap-1 text-[10px] text-status-error">
      <AlertCircle className="w-3 h-3" />
      {msg}
    </div>
  );
}

function defaultForArray(arr: unknown[]): unknown {
  if (arr.length === 0) return "";
  const first = arr[0];
  if (typeof first === "string") return "";
  if (typeof first === "number") return 0;
  if (typeof first === "boolean") return false;
  if (Array.isArray(first)) return [];
  return {};
}
