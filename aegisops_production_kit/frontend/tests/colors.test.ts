import { describe, expect, it } from "vitest";

import { cloudColor, modelColor } from "../lib/colors";

describe("cloudColor", () => {
  it("maps known clouds to their token colors", () => {
    expect(cloudColor("AWS")).toBe("var(--amber)");
    expect(cloudColor("Azure")).toBe("var(--cyan)");
    expect(cloudColor("GCP")).toBe("var(--red)");
    expect(cloudColor("Kubernetes")).toBe("var(--accent-2)");
    expect(cloudColor("VMware")).toBe("var(--green)");
  });

  it("falls back to accent-2 for unknown clouds", () => {
    expect(cloudColor("Oracle")).toBe("var(--accent-2)");
  });
});

describe("modelColor", () => {
  it("maps model families to their token colors", () => {
    // P0/D7: only served models get a brand color — the menu renders GET /models ids
    // (Google Gemini today); anything else is the neutral violet, honestly.
    expect(modelColor("gemini-3.5-flash")).toBe("var(--cyan)");
    expect(modelColor("Gemini 2.5 Pro")).toBe("var(--cyan)");
    expect(modelColor("Claude Sonnet 4.5")).toBe("var(--violet)");
    expect(modelColor("GPT-4o")).toBe("var(--violet)");
    expect(modelColor("Llama 3.1 70B")).toBe("var(--violet)");
  });
});
