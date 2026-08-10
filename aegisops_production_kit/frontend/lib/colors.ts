// Color-by-value helpers — ported EXACTLY from design-reference/DESIGN_REFERENCE.logic.js
// (§8 of 02_DESIGN_SPEC). Used by the cloud + model selectors and resource rows.

export function cloudColor(name: string): string {
  const map: Record<string, string> = {
    AWS: "var(--amber)",
    Azure: "var(--cyan)",
    GCP: "var(--red)",
    Kubernetes: "var(--accent-2)",
    VMware: "var(--green)",
  };
  return map[name] ?? "var(--accent-2)";
}

export function modelColor(name: string): string {
  // P0/D7: the Claude/GPT/Azure branches were dead — the menu now renders only ids the
  // backend actually serves (GET /models; Google Gemini today). New providers get a
  // color when the P1 provider layer makes them real.
  if (/Gemini/i.test(name)) return "var(--cyan)";
  return "var(--violet)";
}
