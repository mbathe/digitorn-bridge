import { useConnection } from "@digitorn/preview-sdk";

export default function ConnectionBadge() {
  const connected = useConnection();
  const color = connected ? "#10b981" : "#ef4444";
  const label = connected ? "live" : "reconnecting…";
  return (
    <span
      style={{
        display: "inline-flex",
        alignItems: "center",
        gap: 6,
        fontSize: 11,
        color: "#cbd5e1",
        fontFamily: "monospace",
      }}
    >
      <span
        style={{
          width: 8,
          height: 8,
          borderRadius: "50%",
          background: color,
          boxShadow: `0 0 6px ${color}`,
        }}
      />
      {label}
    </span>
  );
}
