/**
 * Visual color picker for the ``theme:`` block.
 *
 * Schema: ``AppDefinition.theme: dict[str, str]`` with two known keys:
 *
 *   - accent: hex like '#6EE7B7' - primary action color, overrides app.color
 *   - background: hex - reserved (client-side, not yet rendered)
 *
 * The Flutter client reads these values to recolor the chat surface.
 * We surface them here as a swatch grid + native HTML5 color input
 * with a hex text fallback - far more usable than the generic dict
 * editor that just shows a key/value table.
 */
import { Palette } from "lucide-react";
import clsx from "clsx";

interface Props {
  raw: Record<string, string>;
  basePath: string;
  onEdit: (absolutePath: string, value: unknown) => void;
}

interface ThemeKey {
  key: string;
  label: string;
  hint: string;
  defaultValue: string;
}

const THEME_KEYS: readonly ThemeKey[] = [
  {
    key: "accent",
    label: "Accent",
    hint: "Primary action color. Buttons, focus rings, selected states. Overrides app.color.",
    defaultValue: "#6EE7B7",
  },
  {
    key: "background",
    label: "Background",
    hint: "Reserved - client-side, not yet rendered by the Flutter client.",
    defaultValue: "#0B1220",
  },
] as const;

/** Curated swatch palette - matches the project's Tailwind palette. */
const SWATCHES: Array<{ value: string; name: string }> = [
  { value: "#6EE7B7", name: "Mint" },
  { value: "#34D399", name: "Emerald" },
  { value: "#10B981", name: "Green" },
  { value: "#22D3EE", name: "Cyan" },
  { value: "#0EA5E9", name: "Sky" },
  { value: "#3B82F6", name: "Blue" },
  { value: "#6366F1", name: "Indigo" },
  { value: "#8B5CF6", name: "Violet" },
  { value: "#A855F7", name: "Purple" },
  { value: "#EC4899", name: "Pink" },
  { value: "#F43F5E", name: "Rose" },
  { value: "#F97316", name: "Orange" },
  { value: "#F59E0B", name: "Amber" },
  { value: "#EAB308", name: "Yellow" },
  { value: "#84CC16", name: "Lime" },
  { value: "#94A3B8", name: "Slate" },
];

const BG_SWATCHES: Array<{ value: string; name: string }> = [
  { value: "#0B1220", name: "Midnight" },
  { value: "#0F172A", name: "Slate-950" },
  { value: "#111827", name: "Gray-900" },
  { value: "#18181B", name: "Zinc-900" },
  { value: "#1F2937", name: "Slate-800" },
  { value: "#FFFFFF", name: "White" },
  { value: "#F8FAFC", name: "Slate-50" },
  { value: "#F3F4F6", name: "Gray-100" },
];

export default function ThemeColorPicker({ raw, basePath, onEdit }: Props) {
  return (
    <div className="space-y-4">
      <div className="flex items-center gap-1.5">
        <Palette className="w-3.5 h-3.5 text-accent" />
        <span className="text-[11px] text-ink-muted">
          Override the client's accent + background. Hex codes only.
        </span>
      </div>
      {THEME_KEYS.map((tk) => (
        <ColorField
          key={tk.key}
          label={tk.label}
          hint={tk.hint}
          value={raw[tk.key] ?? ""}
          defaultValue={tk.defaultValue}
          swatches={tk.key === "background" ? BG_SWATCHES : SWATCHES}
          onChange={(v) => {
            if (!v) onEdit(`${basePath}.${tk.key}`, undefined);
            else onEdit(`${basePath}.${tk.key}`, v);
          }}
        />
      ))}
    </div>
  );
}

function ColorField({
  label, hint, value, defaultValue, swatches, onChange,
}: {
  label: string;
  hint: string;
  value: string;
  defaultValue: string;
  swatches: Array<{ value: string; name: string }>;
  onChange: (v: string) => void;
}) {
  const current = value || defaultValue;
  const isSet = !!value;

  return (
    <div className="space-y-1.5">
      <div className="flex items-center gap-2">
        <div className="text-[10px] uppercase tracking-wider text-ink-dim font-semibold flex-1">
          {label}
        </div>
        {!isSet && (
          <span className="text-[9px] uppercase tracking-wider text-ink-dim font-mono">
            using default
          </span>
        )}
        {isSet && (
          <button
            onClick={() => onChange("")}
            className="text-[10px] text-ink-dim hover:text-ink underline-offset-2 hover:underline"
            title="Reset to client default"
          >
            reset
          </button>
        )}
      </div>

      <div className="flex items-center gap-2">
        <div
          className="w-9 h-8 rounded-md border border-border-subtle flex-shrink-0"
          style={{ background: current }}
          title={current}
        />
        <input
          type="color"
          value={isHex(current) ? current : defaultValue}
          onChange={(e) => onChange(e.target.value.toUpperCase())}
          className="w-9 h-8 rounded border border-border-subtle cursor-pointer p-0.5 bg-surface-2"
          title="Pick a color"
        />
        <input
          type="text"
          value={value}
          onChange={(e) => onChange(e.target.value.toUpperCase())}
          placeholder={defaultValue}
          className="flex-1 h-8 px-2 rounded-md bg-surface-2 border border-border-subtle text-[11px] text-ink placeholder:text-ink-dim font-mono focus:outline-none focus:border-accent"
        />
      </div>

      <div className="grid grid-cols-8 gap-1 mt-1">
        {swatches.map((s) => {
          const selected = current.toUpperCase() === s.value.toUpperCase();
          return (
            <button
              key={s.value}
              onClick={() => onChange(s.value)}
              title={`${s.name} - ${s.value}`}
              className={clsx(
                "h-6 rounded-md border transition-all",
                selected
                  ? "border-accent ring-1 ring-accent/40 scale-110"
                  : "border-border-subtle hover:scale-105",
              )}
              style={{ background: s.value }}
            />
          );
        })}
      </div>

      <div className="text-[10px] text-ink-dim italic">{hint}</div>
    </div>
  );
}

function isHex(v: string): boolean {
  return /^#[0-9a-fA-F]{6}$/.test(v) || /^#[0-9a-fA-F]{3}$/.test(v);
}
