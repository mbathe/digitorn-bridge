/**
 * Type-aware trigger editor for `runtime.triggers[]`.
 *
 * The TriggerConfig schema (core/app/schema.py:TriggerConfig) is a
 * union by `type` field with 3 known variants:
 *
 *   cron   -> needs `schedule` (cron expression)
 *   http   -> needs `path` + `method` + `port`
 *   watch  -> needs `paths[]` (glob patterns)
 *
 * Plus universal fields: id, message, routing, routing_key.
 *
 * Instead of rendering a flat form (where the user sees irrelevant
 * fields), this wizard switches its body based on the selected type
 * and surfaces presets / inline help for each variant.
 *
 * Path conventions: receives a `basePath` like
 * `runtime.triggers.0` and emits `onEdit(${basePath}.field, value)`
 * for every change, so the YAML round-trip is identical to the
 * generic EditableConfig flow.
 */
import { useState } from "react";
import {
  Clock, Globe, Eye, Plus, Trash2, Calendar, Zap,
} from "lucide-react";
import clsx from "clsx";

interface Props {
  raw: Record<string, unknown>;
  basePath: string;
  onEdit: (absolutePath: string, value: unknown) => void;
  onDelete: (absolutePath: string) => void;
}

type TriggerType = "cron" | "http" | "watch";

const HTTP_METHODS = ["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"] as const;

const ROUTINGS: Array<{ value: string; label: string; hint: string }> = [
  { value: "broadcast", label: "Broadcast", hint: "Hits every active session of this app." },
  { value: "user", label: "Per user", hint: "One session per identified user; routing_key picks which." },
  { value: "session", label: "Per session", hint: "One specific session; routing_key picks which." },
];

const CRON_PRESETS: Array<{ label: string; expr: string; hint: string }> = [
  { label: "Every minute", expr: "* * * * *", hint: "Mostly for testing." },
  { label: "Every 5 min", expr: "*/5 * * * *", hint: "Frequent polling." },
  { label: "Every hour", expr: "0 * * * *", hint: "Top of every hour." },
  { label: "Daily 9am", expr: "0 9 * * *", hint: "Morning report." },
  { label: "Weekly Monday 9am", expr: "0 9 * * 1", hint: "Weekly digest." },
  { label: "First of month 9am", expr: "0 9 1 * *", hint: "Monthly job." },
];

export default function TriggerWizard({ raw, basePath, onEdit }: Props) {
  const type = ((raw.type as string) ?? "cron") as TriggerType;

  return (
    <div className="space-y-4">
      <TypeSwitcher
        active={type}
        onChange={(t) => {
          onEdit(`${basePath}.type`, t);
          // Pre-fill type-specific defaults so the user never sees an
          // empty required field. Idempotent: never overwrites an
          // existing value.
          if (t === "cron" && !raw.schedule) onEdit(`${basePath}.schedule`, "0 9 * * *");
          if (t === "http" && !raw.method) onEdit(`${basePath}.method`, "POST");
          if (t === "http" && !raw.port) onEdit(`${basePath}.port`, 9100);
          if (t === "http" && !raw.path) onEdit(`${basePath}.path`, "/webhook");
          if (t === "watch" && !Array.isArray(raw.paths)) onEdit(`${basePath}.paths`, ["./inbox/*.csv"]);
        }}
      />

      <Field label="Trigger ID" hint="Unique identifier referenced in logs and routing.">
        <Input
          value={(raw.id as string) ?? ""}
          onChange={(v) => onEdit(`${basePath}.id`, v)}
          placeholder="my_trigger"
          mono
        />
      </Field>

      {type === "cron" && (
        <CronEditor
          schedule={(raw.schedule as string) ?? ""}
          onChange={(v) => onEdit(`${basePath}.schedule`, v)}
        />
      )}

      {type === "http" && (
        <HttpEditor
          path={(raw.path as string) ?? ""}
          method={(raw.method as string) ?? "POST"}
          port={typeof raw.port === "number" ? raw.port : 9100}
          onPath={(v) => onEdit(`${basePath}.path`, v)}
          onMethod={(v) => onEdit(`${basePath}.method`, v)}
          onPort={(v) => onEdit(`${basePath}.port`, v)}
        />
      )}

      {type === "watch" && (
        <WatchEditor
          paths={Array.isArray(raw.paths) ? (raw.paths as string[]) : []}
          basePath={basePath}
          onEdit={onEdit}
        />
      )}

      <Field
        label="Message template"
        hint="Sent to the agent when this trigger fires. Supports {{event.*}} placeholders."
      >
        <Textarea
          value={(raw.message as string) ?? ""}
          onChange={(v) => onEdit(`${basePath}.message`, v)}
          placeholder={
            type === "watch"
              ? "New file: {{event.path}}"
              : type === "http"
              ? "Webhook hit: {{event.body}}"
              : "Run scheduled job"
          }
        />
      </Field>

      <RoutingEditor
        routing={(raw.routing as string) ?? "broadcast"}
        routingKey={(raw.routing_key as string) ?? ""}
        onRouting={(v) => onEdit(`${basePath}.routing`, v)}
        onRoutingKey={(v) => onEdit(`${basePath}.routing_key`, v)}
      />
    </div>
  );
}

/* ─── Type switcher ─────────────────────────────────────── */

function TypeSwitcher({
  active, onChange,
}: {
  active: TriggerType;
  onChange: (t: TriggerType) => void;
}) {
  const tabs: Array<{ key: TriggerType; icon: typeof Clock; label: string; hint: string }> = [
    { key: "cron", icon: Clock, label: "Schedule", hint: "Fire on a cron schedule." },
    { key: "http", icon: Globe, label: "Webhook", hint: "Fire on an HTTP request." },
    { key: "watch", icon: Eye, label: "Watch", hint: "Fire when files match a glob." },
  ];
  return (
    <div className="grid grid-cols-3 gap-1 p-1 bg-surface-2 rounded-lg border border-border-subtle">
      {tabs.map((t) => {
        const Icon = t.icon;
        const selected = t.key === active;
        return (
          <button
            key={t.key}
            onClick={() => onChange(t.key)}
            title={t.hint}
            className={clsx(
              "inline-flex flex-col items-center justify-center gap-1 h-14 px-2 rounded-md text-[11px] transition-colors",
              selected
                ? "bg-accent/15 text-accent font-medium ring-1 ring-accent/30"
                : "text-ink-muted hover:text-ink hover:bg-surface-3",
            )}
          >
            <Icon className="w-3.5 h-3.5" />
            <span>{t.label}</span>
          </button>
        );
      })}
    </div>
  );
}

/* ─── Cron editor ───────────────────────────────────────── */

function CronEditor({
  schedule, onChange,
}: {
  schedule: string;
  onChange: (v: string) => void;
}) {
  return (
    <div className="space-y-2">
      <Field
        label="Cron schedule"
        hint="POSIX cron format: 'min hour day month weekday'. Use the presets below or type your own."
      >
        <Input
          value={schedule}
          onChange={onChange}
          placeholder="0 9 * * *"
          mono
        />
      </Field>
      <div className="text-[10px] uppercase tracking-wider text-ink-dim font-semibold">Presets</div>
      <div className="grid grid-cols-2 gap-1">
        {CRON_PRESETS.map((p) => (
          <button
            key={p.expr}
            onClick={() => onChange(p.expr)}
            title={`${p.expr} - ${p.hint}`}
            className={clsx(
              "text-left px-2 py-1.5 rounded-md text-[11px] transition-colors",
              schedule === p.expr
                ? "bg-accent/15 text-accent ring-1 ring-accent/30"
                : "bg-surface-2 text-ink-muted hover:text-ink hover:bg-surface-3",
            )}
          >
            <div className="flex items-center gap-1.5">
              <Calendar className="w-3 h-3 flex-shrink-0" />
              <span className="font-medium">{p.label}</span>
            </div>
            <div className="font-mono text-[10px] text-ink-dim mt-0.5">{p.expr}</div>
          </button>
        ))}
      </div>
    </div>
  );
}

/* ─── HTTP editor ───────────────────────────────────────── */

function HttpEditor({
  path, method, port, onPath, onMethod, onPort,
}: {
  path: string;
  method: string;
  port: number;
  onPath: (v: string) => void;
  onMethod: (v: string) => void;
  onPort: (v: number) => void;
}) {
  return (
    <div className="space-y-3">
      <div className="flex gap-2">
        <Field label="Method" className="w-28 flex-shrink-0">
          <select
            value={method}
            onChange={(e) => onMethod(e.target.value)}
            className="w-full h-8 px-2 rounded-md bg-surface-2 border border-border-subtle text-[11px] text-ink font-mono focus:outline-none focus:border-accent"
          >
            {HTTP_METHODS.map((m) => (
              <option key={m} value={m}>{m}</option>
            ))}
          </select>
        </Field>
        <Field label="Path" className="flex-1" hint="Path served on the trigger listener. Must start with /.">
          <Input
            value={path}
            onChange={(v) => {
              if (v && !v.startsWith("/")) onPath(`/${v}`);
              else onPath(v);
            }}
            placeholder="/webhook"
            mono
          />
        </Field>
      </div>
      <Field label="Port" hint="Port the listener binds to (1024-65535). Default 9100.">
        <Input
          type="number"
          value={String(port)}
          onChange={(v) => {
            const n = Number(v);
            if (Number.isFinite(n) && n >= 1024 && n <= 65535) onPort(n);
          }}
          placeholder="9100"
          mono
        />
      </Field>
      <div className="text-[10px] text-ink-dim italic px-1">
        <Zap className="inline w-3 h-3 mr-0.5" />
        Curl: <span className="font-mono">{`curl -X ${method} http://host:${port}${path || "/"}`}</span>
      </div>
    </div>
  );
}

/* ─── Watch editor ──────────────────────────────────────── */

function WatchEditor({
  paths, basePath, onEdit,
}: {
  paths: string[];
  basePath: string;
  onEdit: (path: string, value: unknown) => void;
}) {
  return (
    <Field
      label="Watch globs"
      hint="Glob patterns (relative to the app workspace). Files matching any pattern fire the trigger."
    >
      <div className="space-y-1">
        {paths.map((p, i) => (
          <div key={i} className="flex items-center gap-1">
            <Input
              value={p}
              onChange={(v) => onEdit(`${basePath}.paths.${i}`, v)}
              placeholder="./inbox/*.csv"
              mono
            />
            <button
              onClick={() => onEdit(`${basePath}.paths`, paths.filter((_, j) => j !== i))}
              className="p-1 text-status-error/60 hover:text-status-error rounded hover:bg-status-error/10"
              title="Remove glob"
            >
              <Trash2 className="w-3 h-3" />
            </button>
          </div>
        ))}
        <button
          onClick={() => onEdit(`${basePath}.paths.${paths.length}`, "")}
          className="inline-flex items-center gap-1 h-7 px-2 rounded-md text-[11px] bg-accent/15 text-accent hover:bg-accent/25"
        >
          <Plus className="w-3 h-3" /> Add glob
        </button>
      </div>
    </Field>
  );
}

/* ─── Routing editor ────────────────────────────────────── */

function RoutingEditor({
  routing, routingKey, onRouting, onRoutingKey,
}: {
  routing: string;
  routingKey: string;
  onRouting: (v: string) => void;
  onRoutingKey: (v: string) => void;
}) {
  const needsKey = routing === "user" || routing === "session";
  return (
    <div className="space-y-2 pt-2 border-t border-border-subtle">
      <div className="text-[10px] uppercase tracking-wider text-ink-dim font-semibold">Routing</div>
      <div className="grid grid-cols-3 gap-1">
        {ROUTINGS.map((r) => (
          <button
            key={r.value}
            onClick={() => onRouting(r.value)}
            title={r.hint}
            className={clsx(
              "px-2 py-1.5 rounded-md text-[11px] transition-colors",
              routing === r.value
                ? "bg-accent/15 text-accent ring-1 ring-accent/30 font-medium"
                : "bg-surface-2 text-ink-muted hover:text-ink hover:bg-surface-3",
            )}
          >
            {r.label}
          </button>
        ))}
      </div>
      {needsKey && (
        <Field
          label="Routing key"
          hint={
            routing === "user"
              ? "Template extracting the user id from the event payload."
              : "Template extracting the session id from the event payload."
          }
        >
          <Input
            value={routingKey}
            onChange={onRoutingKey}
            placeholder={
              routing === "user"
                ? "{{event.header.X-User-Id}}"
                : "{{event.header.X-Session-Id}}"
            }
            mono
          />
        </Field>
      )}
    </div>
  );
}

/* ─── Primitives ────────────────────────────────────────── */

function Field({
  label, hint, children, className,
}: {
  label: string;
  hint?: string;
  children: React.ReactNode;
  className?: string;
}) {
  return (
    <div className={clsx("space-y-1", className)}>
      <div className="text-[10px] uppercase tracking-wider text-ink-dim font-semibold">{label}</div>
      {children}
      {hint && <div className="text-[10px] text-ink-dim italic px-0.5">{hint}</div>}
    </div>
  );
}

function Input({
  value, onChange, placeholder, mono, type = "text",
}: {
  value: string;
  onChange: (v: string) => void;
  placeholder?: string;
  mono?: boolean;
  type?: "text" | "number";
}) {
  return (
    <input
      type={type}
      value={value}
      onChange={(e) => onChange(e.target.value)}
      placeholder={placeholder}
      className={clsx(
        "w-full h-8 px-2 rounded-md bg-surface-2 border border-border-subtle text-[11px] text-ink placeholder:text-ink-dim focus:outline-none focus:border-accent",
        mono && "font-mono",
      )}
    />
  );
}

function Textarea({
  value, onChange, placeholder,
}: {
  value: string;
  onChange: (v: string) => void;
  placeholder?: string;
}) {
  return (
    <textarea
      value={value}
      onChange={(e) => onChange(e.target.value)}
      placeholder={placeholder}
      rows={2}
      className="w-full min-h-[48px] px-2 py-1.5 rounded-md bg-surface-2 border border-border-subtle text-[11px] text-ink placeholder:text-ink-dim font-mono focus:outline-none focus:border-accent resize-y"
    />
  );
}
