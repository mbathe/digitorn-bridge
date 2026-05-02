/**
 * Dedicated structured editor for an agent's `brain` block (and the
 * nested `brain.fallback` block). Replaces the generic recursive
 * EditableConfig for the brain field, which rendered nested objects
 * as a JSON-like tree with collapsibles -- unreadable for users who
 * just want to tweak provider / model / api_key.
 *
 * Fields exposed (matches packages/digitorn/core/app/schema.py):
 *   - provider, model, backend
 *   - temperature, max_tokens
 *   - context.{max_tokens, strategy, keep_recent, auto_compact}
 *   - config.{base_url, api_key, num_ctx, ...} (free-form)
 *   - credential (string OR object {ref, scope, provider})
 *   - fallback   (recursive — same shape, no infinite nesting)
 *   - vision, image_generation, image_detail, max_images_per_turn
 *
 * Resilience: if the value or any nested fallback is a STRING that
 * parses as a JSON object, we parse it transparently and write back
 * a real object on save. Users frequently end up with stringified
 * sub-objects after copy/paste mishaps; auto-recovering keeps the
 * form usable instead of dropping to a textarea.
 */
import { useMemo, useState } from "react";
import { Brain, ChevronDown, ChevronRight, AlertTriangle, Plus, Trash2 } from "lucide-react";
import clsx from "clsx";

const BRAIN_PROVIDERS = [
  "anthropic", "openai", "deepseek", "google", "ollama",
  "groq", "azure_openai", "openrouter", "mistral", "togetherai",
];

// Per schema.py AgentBrain.backend = Literal["openai_compat", "anthropic", "github_copilot"].
// "native" and "anthropic_compat" are NOT valid backend values - the compiler rejects them.
const BACKENDS = ["openai_compat", "anthropic", "github_copilot"];

interface Props {
  /** Current value at brain (or brain.fallback). May be undefined,
   *  an object, or a stringified-JSON object (auto-recovered). */
  value: unknown;
  /** Absolute YAML path of THIS brain block, e.g.
   *  "agents.0.brain" or "agents.0.brain.fallback". */
  basePath: string;
  /** Title shown above the form. */
  title?: string;
  /** Subtitle / hint shown below the title. */
  hint?: string;
  /** Recursion guard: only the top-level brain renders the
   *  "Add fallback brain" affordance. */
  isFallback?: boolean;
  onEdit: (absolutePath: string, value: unknown) => void;
  onDelete: (absolutePath: string) => void;
}

/** Parse a stringified JSON object back into an object, or null. */
function tryParseJson(s: string): Record<string, unknown> | null {
  const t = s.trim();
  if (!t.startsWith("{")) return null;
  try {
    const parsed = JSON.parse(t);
    return parsed && typeof parsed === "object" && !Array.isArray(parsed) ? parsed : null;
  } catch { return null; }
}

export default function BrainEditor({
  value,
  basePath,
  title = "Brain",
  hint,
  isFallback = false,
  onEdit,
  onDelete,
}: Props) {
  // ── Recover from stringified JSON ───────────────────────────────
  // Some YAML edits end up storing brain or brain.fallback as a
  // string (e.g. paste of a JSON object into a textarea). Transparently
  // recover so the user sees structured fields again.
  const [stringRecovered, setStringRecovered] = useState(false);
  const obj: Record<string, unknown> = useMemo(() => {
    if (value && typeof value === "object" && !Array.isArray(value)) {
      return value as Record<string, unknown>;
    }
    if (typeof value === "string") {
      const parsed = tryParseJson(value);
      if (parsed) return parsed;
    }
    return {};
  }, [value]);

  const isStringValue = typeof value === "string" && tryParseJson(value);

  const fallback = obj.fallback as Record<string, unknown> | string | undefined;
  const ctx = (obj.context && typeof obj.context === "object")
    ? (obj.context as Record<string, unknown>) : undefined;
  const cfg = (obj.config && typeof obj.config === "object")
    ? (obj.config as Record<string, unknown>) : undefined;
  const credential = obj.credential;

  const [showAdvanced, setShowAdvanced] = useState(false);
  const [showContext, setShowContext] = useState(!!ctx);
  const [showConfig, setShowConfig] = useState(!!cfg);

  // Helper: write a single field at obj.<key>.
  const set = (key: string, v: unknown) => onEdit(`${basePath}.${key}`, v);
  const del = (key: string) => onDelete(`${basePath}.${key}`);

  // When the user edits a stringified-JSON brain, the FIRST edit
  // should also write the parsed object back so subsequent edits use
  // the structured form. Wraps `set` / `del` to do both.
  const writeStructuredFirst = () => {
    if (isStringValue && !stringRecovered) {
      onEdit(basePath, obj);
      setStringRecovered(true);
    }
  };
  const setRecover = (key: string, v: unknown) => {
    writeStructuredFirst();
    set(key, v);
  };

  return (
    <div className="space-y-3">
      {/* Header */}
      <div className="flex items-center gap-2">
        <Brain className="w-3.5 h-3.5 text-kind-agent" />
        <span className="text-[12px] font-semibold text-ink">{title}</span>
        {isFallback && (
          <button
            onClick={() => onDelete(basePath)}
            className="ml-auto inline-flex items-center gap-1 text-[10px] text-status-error/70 hover:text-status-error"
            title="Remove this fallback brain"
          >
            <Trash2 className="w-2.5 h-2.5" />
            Remove fallback
          </button>
        )}
      </div>

      {hint && (
        <div className="text-[10px] text-ink-dim italic">{hint}</div>
      )}

      {/* Recovery banner — when value was a JSON string, signal it. */}
      {isStringValue && (
        <div className="flex items-start gap-2 px-2 py-1.5 rounded bg-status-warn/10 border border-status-warn/30 text-[10px] text-status-warn">
          <AlertTriangle className="w-3 h-3 flex-shrink-0 mt-0.5" />
          <div className="flex-1">
            This brain was stored as a JSON string. The fields below show the
            parsed view; the next edit converts it to a proper YAML object.
          </div>
        </div>
      )}

      {/* Core fields — always visible */}
      <div className="grid grid-cols-2 gap-2">
        <Field
          label="Provider"
          required
          value={obj.provider as string | undefined}
          options={BRAIN_PROVIDERS}
          onChange={(v) => setRecover("provider", v)}
          path={`${basePath}.provider`}
        />
        <Field
          label="Model"
          required
          value={obj.model as string | undefined}
          onChange={(v) => setRecover("model", v)}
          path={`${basePath}.model`}
          placeholder="claude-sonnet-4-6"
        />
      </div>

      <div className="grid grid-cols-2 gap-2">
        <Field
          label="Temperature"
          type="number"
          step={0.05}
          min={0}
          max={2}
          value={obj.temperature as number | undefined}
          onChange={(v) => setRecover("temperature", v === "" ? undefined : Number(v))}
          path={`${basePath}.temperature`}
          placeholder="0–2"
        />
        <Field
          label="Max tokens"
          type="number"
          value={obj.max_tokens as number | undefined}
          onChange={(v) => setRecover("max_tokens", v === "" ? undefined : Number(v))}
          path={`${basePath}.max_tokens`}
          placeholder="e.g. 16384"
        />
      </div>

      <Field
        label="Backend"
        value={obj.backend as string | undefined}
        options={BACKENDS}
        allowEmpty
        onChange={(v) => setRecover("backend", v || undefined)}
        path={`${basePath}.backend`}
        hint="openai_compat for OpenAI / DeepSeek / Groq / Ollama / vLLM / OpenRouter / Mistral. anthropic for native Claude SDK."
      />

      {/* Credential — string ref or {ref, scope, provider} */}
      <CredentialField
        value={credential}
        onChange={(v) => setRecover("credential", v)}
        path={`${basePath}.credential`}
      />

      {/* Context sub-block */}
      <Toggleable
        title="Context window"
        open={showContext}
        onToggle={() => setShowContext((v) => !v)}
        onAdd={() => { setRecover("context", { max_tokens: 200000, strategy: "summarize", keep_recent: 10, auto_compact: true }); setShowContext(true); }}
        onRemove={ctx ? () => { del("context"); setShowContext(false); } : undefined}
        present={!!ctx}
      >
        {ctx && (
          <div className="grid grid-cols-2 gap-2">
            <Field
              label="Window (tokens)"
              type="number"
              value={ctx.max_tokens as number | undefined}
              onChange={(v) => setRecover("context.max_tokens", v === "" ? undefined : Number(v))}
              path={`${basePath}.context.max_tokens`}
              placeholder="200000"
            />
            <Field
              label="Strategy"
              value={ctx.strategy as string | undefined}
              options={["summarize", "drop_oldest", "none"]}
              onChange={(v) => setRecover("context.strategy", v)}
              path={`${basePath}.context.strategy`}
            />
            <Field
              label="Keep recent"
              type="number"
              value={ctx.keep_recent as number | undefined}
              onChange={(v) => setRecover("context.keep_recent", v === "" ? undefined : Number(v))}
              path={`${basePath}.context.keep_recent`}
            />
            <Field
              label="Auto compact"
              type="boolean"
              value={ctx.auto_compact as boolean | undefined}
              onChange={(v) => setRecover("context.auto_compact", v)}
              path={`${basePath}.context.auto_compact`}
            />
          </div>
        )}
      </Toggleable>

      {/* Backend config — free-form k/v map (base_url, api_key, num_ctx, ...) */}
      <Toggleable
        title="Backend config"
        open={showConfig}
        onToggle={() => setShowConfig((v) => !v)}
        onAdd={() => { setRecover("config", { base_url: "", api_key: "" }); setShowConfig(true); }}
        onRemove={cfg ? () => { del("config"); setShowConfig(false); } : undefined}
        present={!!cfg}
      >
        {cfg && (
          <ConfigKvList
            cfg={cfg}
            basePath={`${basePath}.config`}
            onEdit={(key, v) => setRecover(`config.${key}`, v)}
            onDelete={(key) => del(`config.${key}`)}
          />
        )}
      </Toggleable>

      {/* Advanced (vision/image flags) — collapsed by default */}
      <button
        onClick={() => setShowAdvanced((v) => !v)}
        className="flex items-center gap-1.5 text-[10px] text-ink-dim hover:text-ink-muted"
      >
        {showAdvanced ? <ChevronDown className="w-2.5 h-2.5" /> : <ChevronRight className="w-2.5 h-2.5" />}
        Multimodal & advanced
      </button>
      {showAdvanced && (
        <div className="grid grid-cols-2 gap-2 pl-3">
          <Field
            label="Vision"
            type="boolean"
            value={obj.vision as boolean | undefined}
            onChange={(v) => setRecover("vision", v)}
            path={`${basePath}.vision`}
            hint="null=auto, true/false to override"
          />
          <Field
            label="Image generation"
            type="boolean"
            value={obj.image_generation as boolean | undefined}
            onChange={(v) => setRecover("image_generation", v)}
            path={`${basePath}.image_generation`}
          />
          <Field
            label="Image detail"
            value={obj.image_detail as string | undefined}
            options={["auto", "low", "high"]}
            onChange={(v) => setRecover("image_detail", v)}
            path={`${basePath}.image_detail`}
          />
          <Field
            label="Max images / turn"
            type="number"
            value={obj.max_images_per_turn as number | undefined}
            onChange={(v) => setRecover("max_images_per_turn", v === "" ? undefined : Number(v))}
            path={`${basePath}.max_images_per_turn`}
          />
        </div>
      )}

      {/* Fallback brain — recursive BrainEditor. Top-level only. */}
      {!isFallback && (
        <div className="pt-2 border-t border-border-subtle">
          {fallback ? (
            <BrainEditor
              value={fallback}
              basePath={`${basePath}.fallback`}
              title="Fallback brain"
              hint="Used when the primary brain returns a billing or rate-limit error (HTTP 402, 'Insufficient Balance'). Switches back to primary on the next turn."
              isFallback
              onEdit={onEdit}
              onDelete={onDelete}
            />
          ) : (
            <button
              onClick={() => setRecover("fallback", { provider: "anthropic", model: "claude-haiku-4-5" })}
              className="inline-flex items-center gap-1.5 text-[11px] text-accent/80 hover:text-accent px-2 py-1 rounded border border-dashed border-accent/40 hover:border-accent/60 hover:bg-accent/5"
            >
              <Plus className="w-3 h-3" />
              Add fallback brain
            </button>
          )}
        </div>
      )}
    </div>
  );
}

/* ─── Sub-components ──────────────────────────────────────────── */

function Field({
  label, value, onChange, path, options, type, placeholder,
  hint, required, allowEmpty, step, min, max,
}: {
  label: string;
  value: string | number | boolean | undefined;
  onChange: (v: string | boolean) => void;
  path: string;
  options?: string[];
  type?: "string" | "number" | "boolean";
  placeholder?: string;
  hint?: string;
  required?: boolean;
  allowEmpty?: boolean;
  step?: number;
  min?: number;
  max?: number;
}) {
  const isMissing = required && (value === undefined || value === "");

  if (type === "boolean") {
    return (
      <div className="flex flex-col gap-1">
        <span className="text-[10px] font-mono text-ink-muted">{label}</span>
        <select
          value={value === true ? "true" : value === false ? "false" : ""}
          onChange={(e) => {
            const v = e.target.value;
            if (v === "true") onChange(true);
            else if (v === "false") onChange(false);
            else onChange("");
          }}
          className="h-7 px-1.5 rounded bg-surface-2 border border-border-subtle text-[11px] text-ink"
          data-yaml-path={path}
        >
          <option value="">(unset)</option>
          <option value="true">true</option>
          <option value="false">false</option>
        </select>
        {hint && <span className="text-[9px] text-ink-dim italic">{hint}</span>}
      </div>
    );
  }

  if (options) {
    return (
      <div className="flex flex-col gap-1">
        <span className="text-[10px] font-mono text-ink-muted">
          {label}{required && <span className="text-status-error">*</span>}
        </span>
        <select
          value={(value as string | undefined) ?? ""}
          onChange={(e) => onChange(e.target.value)}
          className={clsx(
            "h-7 px-1.5 rounded bg-surface-2 border text-[11px] text-ink",
            isMissing ? "border-status-error/60" : "border-border-subtle",
          )}
          data-yaml-path={path}
        >
          {(allowEmpty || !required) && <option value="">(default)</option>}
          {options.map((o) => <option key={o} value={o}>{o}</option>)}
        </select>
        {hint && <span className="text-[9px] text-ink-dim italic">{hint}</span>}
      </div>
    );
  }

  // Mask credentials in the visible input.
  const isSecret = /key|token|secret|password/i.test(label);

  return (
    <div className="flex flex-col gap-1">
      <span className="text-[10px] font-mono text-ink-muted">
        {label}{required && <span className="text-status-error">*</span>}
      </span>
      <input
        type={type === "number" ? "number" : isSecret ? "password" : "text"}
        defaultValue={value === undefined || value === null ? "" : String(value)}
        onBlur={(e) => onChange(e.target.value)}
        onKeyDown={(e) => { if (e.key === "Enter") (e.target as HTMLInputElement).blur(); }}
        placeholder={placeholder}
        step={step}
        min={min}
        max={max}
        className={clsx(
          "h-7 px-1.5 rounded bg-surface-2 border text-[11px] text-ink font-mono",
          isMissing ? "border-status-error/60" : "border-border-subtle",
        )}
        data-yaml-path={path}
      />
      {hint && <span className="text-[9px] text-ink-dim italic">{hint}</span>}
    </div>
  );
}

function CredentialField({
  value, onChange, path,
}: {
  value: unknown;
  onChange: (v: unknown) => void;
  path: string;
}) {
  // string OR { ref, scope, provider }
  const isStr = typeof value === "string";
  const obj = (value && typeof value === "object" && !Array.isArray(value))
    ? (value as Record<string, unknown>) : null;

  const [structured, setStructured] = useState(!!obj);

  if (!structured && !obj) {
    return (
      <Field
        label="Credential"
        value={isStr ? value : undefined}
        onChange={(v) => onChange(v || undefined)}
        path={path}
        hint="Credential vault id (e.g. anthropic_main). Click ⚙ to add scope/provider."
      />
    );
  }

  const ref = obj?.ref ?? (isStr ? value : "");
  const scope = obj?.scope ?? "";
  const prov = obj?.provider ?? "";

  return (
    <div className="space-y-1">
      <div className="flex items-center justify-between">
        <span className="text-[10px] font-mono text-ink-muted">Credential</span>
        <button
          onClick={() => { setStructured(false); onChange(typeof ref === "string" ? ref : undefined); }}
          className="text-[9px] text-ink-dim hover:text-ink"
        >
          collapse to ref-only
        </button>
      </div>
      <div className="grid grid-cols-3 gap-1.5">
        <Field label="ref" value={ref as string} onChange={(v) => onChange({ ...(obj ?? {}), ref: v })} path={`${path}.ref`} />
        <Field label="scope" value={scope as string} onChange={(v) => onChange({ ...(obj ?? {}), scope: v || undefined })} path={`${path}.scope`} options={["system_wide", "per_app_shared", "per_user", "per_app_per_user"]} allowEmpty />
        <Field label="provider" value={prov as string} onChange={(v) => onChange({ ...(obj ?? {}), provider: v || undefined })} path={`${path}.provider`} />
      </div>
    </div>
  );
}

function Toggleable({
  title, open, onToggle, onAdd, onRemove, present, children,
}: {
  title: string;
  open: boolean;
  onToggle: () => void;
  onAdd?: () => void;
  onRemove?: () => void;
  present: boolean;
  children: React.ReactNode;
}) {
  return (
    <div className="border border-border-subtle/60 rounded p-2 bg-surface-1/40">
      <div className="flex items-center gap-2">
        <button onClick={onToggle} className="flex items-center gap-1 text-[11px] text-ink-muted hover:text-ink">
          {open ? <ChevronDown className="w-2.5 h-2.5" /> : <ChevronRight className="w-2.5 h-2.5" />}
          {title}
        </button>
        <div className="flex-1" />
        {!present && onAdd && (
          <button onClick={onAdd} className="text-[10px] text-accent/80 hover:text-accent inline-flex items-center gap-1">
            <Plus className="w-2.5 h-2.5" /> add
          </button>
        )}
        {present && onRemove && (
          <button onClick={onRemove} className="text-[10px] text-status-error/70 hover:text-status-error inline-flex items-center gap-1">
            <Trash2 className="w-2.5 h-2.5" /> remove
          </button>
        )}
      </div>
      {open && present && <div className="mt-2">{children}</div>}
    </div>
  );
}

function ConfigKvList({
  cfg, basePath, onEdit, onDelete,
}: {
  cfg: Record<string, unknown>;
  basePath: string;
  onEdit: (key: string, value: unknown) => void;
  onDelete: (key: string) => void;
}) {
  const [newKey, setNewKey] = useState("");

  return (
    <div className="space-y-1.5">
      {Object.entries(cfg).map(([k, v]) => (
        <div key={k} className="grid grid-cols-[100px_1fr_auto] gap-1.5 items-center">
          <span className="text-[10px] font-mono text-ink-muted truncate" title={k}>{k}</span>
          <input
            type={/key|token|secret|password/i.test(k) ? "password" : "text"}
            defaultValue={typeof v === "object" ? JSON.stringify(v) : String(v ?? "")}
            onBlur={(e) => {
              const raw = e.target.value;
              // Auto-coerce numbers; leave strings alone
              const n = Number(raw);
              const parsed = raw !== "" && !Number.isNaN(n) && /^-?\d+(\.\d+)?$/.test(raw) ? n : raw;
              onEdit(k, parsed);
            }}
            className="h-6 px-1.5 rounded bg-surface-2 border border-border-subtle text-[11px] text-ink font-mono"
            data-yaml-path={`${basePath}.${k}`}
          />
          <button
            onClick={() => onDelete(k)}
            className="text-status-error/50 hover:text-status-error"
            title="Remove key"
          >
            <Trash2 className="w-2.5 h-2.5" />
          </button>
        </div>
      ))}
      <div className="grid grid-cols-[100px_1fr_auto] gap-1.5 items-center pt-1">
        <input
          type="text"
          value={newKey}
          onChange={(e) => setNewKey(e.target.value)}
          placeholder="new_key"
          className="h-6 px-1.5 rounded bg-surface-2 border border-border-subtle/60 text-[11px] text-ink font-mono"
        />
        <span className="text-[9px] text-ink-dim italic">value set after key creation</span>
        <button
          onClick={() => { if (newKey.trim()) { onEdit(newKey.trim(), ""); setNewKey(""); } }}
          className="text-accent/70 hover:text-accent"
          title="Add key"
        >
          <Plus className="w-3 h-3" />
        </button>
      </div>
    </div>
  );
}
