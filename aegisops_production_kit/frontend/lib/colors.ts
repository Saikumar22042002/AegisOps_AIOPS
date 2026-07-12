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
  if (/Claude/i.test(name)) return "var(--amber)";
  if (/GPT|OpenAI/i.test(name)) return "var(--green)";
  if (/Gemini/i.test(name)) return "var(--cyan)";
  if (/Azure/i.test(name)) return "var(--accent-2)";
  return "var(--violet)";
}
