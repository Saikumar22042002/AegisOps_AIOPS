import type { CSSProperties } from "react";

// navStyle / tabStyle ported from DESIGN_REFERENCE.logic.js, returned as CSSProperties.

export function navStyle(active: boolean): CSSProperties {
  const base: CSSProperties = {
    display: "flex",
    alignItems: "center",
    gap: 11,
    width: "100%",
    padding: "10px 9px",
    borderRadius: 9,
    border: "none",
    fontSize: 13,
    fontWeight: 500,
    cursor: "pointer",
    textAlign: "left",
    transition: "background .12s",
  };
  if (active)
    return {
      ...base,
      background: "rgba(99,102,241,.12)",
      color: "var(--text-navactive)",
      boxShadow: "inset 2px 0 0 var(--accent-2)",
    };
  return { ...base, background: "transparent", color: "var(--text-3)" };
}

export function tabStyle(active: boolean): CSSProperties {
  const base: CSSProperties = {
    padding: "9px 13px",
    border: "none",
    background: "transparent",
    fontSize: 12.5,
    fontWeight: 500,
    cursor: "pointer",
    transition: "color .12s",
    whiteSpace: "nowrap",
  };
  if (active) return { ...base, color: "var(--text)", boxShadow: "inset 0 -2px 0 var(--accent-2)" };
  return { ...base, color: "var(--text-4)" };
}
