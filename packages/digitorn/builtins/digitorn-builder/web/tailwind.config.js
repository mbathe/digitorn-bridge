/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{ts,tsx,js,jsx}"],
  darkMode: "class",
  theme: {
    extend: {
      colors: {
        // Surface tokens — auto-mapped to CSS vars set by useTheme()
        surface: {
          0: "rgb(var(--surface-0) / <alpha-value>)",
          1: "rgb(var(--surface-1) / <alpha-value>)",
          2: "rgb(var(--surface-2) / <alpha-value>)",
          3: "rgb(var(--surface-3) / <alpha-value>)",
        },
        ink: {
          DEFAULT: "rgb(var(--ink) / <alpha-value>)",
          muted: "rgb(var(--ink-muted) / <alpha-value>)",
          dim: "rgb(var(--ink-dim) / <alpha-value>)",
        },
        accent: {
          DEFAULT: "rgb(var(--accent) / <alpha-value>)",
          fg: "rgb(var(--accent-fg) / <alpha-value>)",
        },
        border: {
          subtle: "rgb(var(--border-subtle) / <alpha-value>)",
          DEFAULT: "rgb(var(--border) / <alpha-value>)",
          strong: "rgb(var(--border-strong) / <alpha-value>)",
        },
        // Status palette (consistent across themes)
        status: {
          idle: "rgb(var(--status-idle) / <alpha-value>)",
          running: "rgb(var(--status-running) / <alpha-value>)",
          ok: "rgb(var(--status-ok) / <alpha-value>)",
          warn: "rgb(var(--status-warn) / <alpha-value>)",
          error: "rgb(var(--status-error) / <alpha-value>)",
        },
        // Node-kind palette
        kind: {
          app: "rgb(var(--kind-app) / <alpha-value>)",
          agent: "rgb(var(--kind-agent) / <alpha-value>)",
          subagent: "rgb(var(--kind-subagent) / <alpha-value>)",
          module: "rgb(var(--kind-module) / <alpha-value>)",
          skill: "rgb(var(--kind-skill) / <alpha-value>)",
          hook: "rgb(var(--kind-hook) / <alpha-value>)",
          trigger: "rgb(var(--kind-trigger) / <alpha-value>)",
          channel: "rgb(var(--kind-channel) / <alpha-value>)",
          memory: "rgb(var(--kind-memory) / <alpha-value>)",
          io: "rgb(var(--kind-io) / <alpha-value>)",
        },
      },
      boxShadow: {
        node: "0 1px 2px rgb(0 0 0 / 0.10), 0 4px 12px rgb(0 0 0 / 0.08)",
        "node-hover": "0 2px 4px rgb(0 0 0 / 0.15), 0 8px 24px rgb(0 0 0 / 0.18)",
        "node-active": "0 0 0 2px rgb(var(--accent) / 0.5), 0 0 24px rgb(var(--accent) / 0.4)",
        glass: "inset 0 1px 0 rgb(255 255 255 / 0.05), 0 8px 32px rgb(0 0 0 / 0.4)",
      },
      animation: {
        pulse: "pulse 2s cubic-bezier(0.4, 0, 0.6, 1) infinite",
        "ping-slow": "ping 3s cubic-bezier(0, 0, 0.2, 1) infinite",
        shimmer: "shimmer 2.5s linear infinite",
        "edge-flow": "edgeFlow 1.5s linear infinite",
      },
      keyframes: {
        shimmer: {
          "0%": { backgroundPosition: "-200% 0" },
          "100%": { backgroundPosition: "200% 0" },
        },
        edgeFlow: {
          "0%": { strokeDashoffset: 0 },
          "100%": { strokeDashoffset: -16 },
        },
      },
      fontFamily: {
        sans: ["Inter", "ui-sans-serif", "system-ui", "sans-serif"],
        mono: ["JetBrains Mono", "ui-monospace", "Menlo", "monospace"],
      },
    },
  },
  plugins: [],
};
