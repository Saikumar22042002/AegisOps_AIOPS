// Icon helpers — ported from DESIGN_REFERENCE.logic.js (icon, navIcon, themeGlyph) plus
// the AegisOps shield logo reused across screens. SVGs are kept byte-identical to the source.

import type { ResolvedTheme } from "./types";

export function ThemeGlyph({ t }: { t: ResolvedTheme }) {
  if (t === "light") {
    return (
      <svg width={14} height={14} viewBox="0 0 24 24" fill="none">
        <circle cx={12} cy={12} r={4} style={{ stroke: "currentColor" }} strokeWidth={1.7} />
        <path
          d="M12 3v2M12 19v2M3 12h2M19 12h2M5.6 5.6l1.4 1.4M17 17l1.4 1.4M18.4 5.6 17 7M7 17l-1.4 1.4"
          style={{ stroke: "currentColor" }}
          strokeWidth={1.7}
          strokeLinecap="round"
        />
      </svg>
    );
  }
  return (
    <svg width={14} height={14} viewBox="0 0 24 24" fill="none">
      <path
        d="M20 14.5A8 8 0 0 1 9.5 4 7 7 0 1 0 20 14.5Z"
        style={{ stroke: "currentColor" }}
        strokeWidth={1.7}
        strokeLinejoin="round"
      />
    </svg>
  );
}

export function Icon({ kind, color }: { kind: "check" | "spin" | "x" | "dim"; color?: string }) {
  const c = color || "var(--text-3)";
  if (kind === "check")
    return (
      <svg width={12} height={12} viewBox="0 0 24 24" fill="none">
        <path d="m5 12 5 5 9-11" style={{ stroke: c }} strokeWidth={2.6} strokeLinecap="round" strokeLinejoin="round" />
      </svg>
    );
  if (kind === "spin")
    return (
      <svg width={13} height={13} viewBox="0 0 24 24" fill="none" style={{ animation: "ao-spin 1s linear infinite" }}>
        <path d="M12 3a9 9 0 1 0 9 9" style={{ stroke: c }} strokeWidth={2.4} strokeLinecap="round" />
      </svg>
    );
  if (kind === "x")
    return (
      <svg width={11} height={11} viewBox="0 0 24 24" fill="none">
        <path d="M6 6l12 12M18 6 6 18" style={{ stroke: c }} strokeWidth={2.4} strokeLinecap="round" />
      </svg>
    );
  return <span style={{ width: 6, height: 6, borderRadius: 99, background: c }} />;
}

export function NavIcon({ d, color, size = 15 }: { d: string; color?: string; size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none">
      <path d={d} style={{ stroke: color || "var(--text-3)" }} strokeWidth={1.6} strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
}

export function BrandShield({ size = 16, filled = true }: { size?: number; filled?: boolean }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none">
      <path
        d="M12 2.6 4.6 5.6v5.2c0 4.4 3 8.5 7.4 9.9 4.4-1.4 7.4-5.5 7.4-9.9V5.6L12 2.6Z"
        fill={filled ? "rgba(255,255,255,.2)" : "none"}
        stroke="#fff"
        strokeWidth={1.6}
        strokeLinejoin="round"
      />
      <path d="m8.7 12 2.2 2.3 4.2-4.6" stroke="#fff" strokeWidth={1.7} strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
}

export function Checkmark({ color = "var(--accent-2)", size = 14 }: { color?: string; size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none">
      <path d="m5 12 5 5 9-11" stroke={color} strokeWidth={2.4} strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
}

export function Chevron({ size = 11, color = "var(--text-4)" }: { size?: number; color?: string }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none">
      <path d="m7 10 5 5 5-5" stroke={color} strokeWidth={1.9} strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
}
